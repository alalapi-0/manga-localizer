from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageGeneration,
    PageLineageEvent,
    PageTranslationReview,
    Project,
    RegionTranslationCandidate,
    RegionTranslationReview,
    Revision,
    TextRegion,
    new_id,
)
from manga_localizer.security import validate_remote_base_url
from manga_localizer.services.clean_plates import require_current_clean_plate_acceptance
from manga_localizer.services.page_lineage import (
    JobMutationBinding,
    PageLineageConflict,
    _append_event,
    _safe_actor,
    ocr_source_review_required,
    require_current_ocr_trust,
    require_image_mutation_lineage,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
)

TRANSLATION_QC_CHECKS = (
    "target-chinese-checked",
    "forbidden-template-checked",
    "nonempty-checked",
    "source-copy-checked",
    "japanese-residual-checked",
    "generic-duplicate-checked",
    "source-consistency-checked",
    "context-consistency-checked",
    "tone-and-type-checked",
    "source-noise-checked",
)
TRANSLATION_QC_FLAGS = {
    "none",
    "empty-output",
    "non-chinese-output",
    "forbidden-template",
    "source-copy",
    "japanese-residual",
    "generic-duplicate",
    "source-inconsistent",
    "context-inconsistent",
    "source-noise-hallucination",
}
TRANSLATION_REJECT_REASONS = (TRANSLATION_QC_FLAGS - {"none"}) | {"multiple-qc-failures"}
G9_TRANSLATION_CONTRACT_VERSION = "g9-translation-v1"
_AUTOMATIC_PROVIDER_MODELS = {
    "mock": "mock-v1",
    "argos-ja-zh": "argos-ja-zh-local-v1",
}
_REMOTE_TRANSLATION_PROVIDERS = {"openai", "openai-compatible"}
_REVISION_ONLY_PROVIDERS = {"manual", "dictionary"}
_OPAQUE_VALUE_RE = re.compile(r"^[^/\\\x00\r\n]{1,128}$")
_CHECK_FAILURE_FLAG = {
    "target-chinese-checked": "non-chinese-output",
    "forbidden-template-checked": "forbidden-template",
    "nonempty-checked": "empty-output",
    "source-copy-checked": "source-copy",
    "japanese-residual-checked": "japanese-residual",
    "generic-duplicate-checked": "generic-duplicate",
    "source-consistency-checked": "source-inconsistent",
    "context-consistency-checked": "context-inconsistent",
    "tone-and-type-checked": "context-inconsistent",
    "source-noise-checked": "source-noise-hallucination",
}
_FORBIDDEN_RE = re.compile(
    r"(?:联系我们|联系人|客服|客户服务|免责声明|作为.{0,16}(?:AI|人工智能)|(?:AI|人工智能).{0,16}(?:模型|助手)|语言模型|无法.{0,12}(?:回答|提供))",
    re.IGNORECASE,
)
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff]")
_HAN_RE = re.compile(r"[\u3400-\u9fff]")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text_checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _active(session, image: ImageAsset) -> PageGeneration:
    generation = session.scalar(
        select(PageGeneration).where(
            PageGeneration.image_id == image.id,
            PageGeneration.project_id == image.project_id,
            PageGeneration.state == "active",
        )
    )
    if generation is None:
        raise PageLineageConflict(
            "G9 requires an active page generation",
            resource=f"image:{image.id}",
            reason="active-generation-missing",
        )
    return generation


def eligible_translation_regions(session, image_id: str) -> list[TextRegion]:
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image_id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    return [
        row
        for row in rows
        if ocr_source_review_required(row)
        and row.ocr_review is not None
        and row.ocr_generation_id is not None
        and _text_checksum(row.source_text) == row.ocr_review.get("sourceTextChecksum")
    ]


def _bounded_policy(settings: dict[str, Any]) -> dict[str, Any]:
    values = settings
    radius_value = values.get("contextRadius", values.get("contextPages", 1))
    try:
        radius = max(0, min(int(radius_value), 10))
    except (TypeError, ValueError):
        radius = 1

    def bounded_map(value: object) -> dict[str, str]:
        if not isinstance(value, dict) or len(value) > 200:
            return {}
        result: dict[str, str] = {}
        for key, entry in sorted(value.items(), key=lambda pair: str(pair[0])):
            if (
                isinstance(key, str)
                and isinstance(entry, str)
                and 0 < len(key) <= 80
                and len(entry) <= 200
            ):
                result[key] = entry
        return result

    target = values.get("targetLanguage") or settings.get("targetLanguage") or "zh-CN"
    if not isinstance(target, str) or len(target) > 40:
        target = "zh-CN"
    return {
        "targetLanguage": target,
        "contextRadius": radius,
        "glossary": bounded_map(values.get("glossary")),
        "characterNames": bounded_map(values.get("characterNames")),
        "policyVersion": "g9-context-v1",
    }


def _context_for(
    rows: list[TextRegion], index: int, policy: dict[str, Any]
) -> tuple[list[TextRegion], str]:
    radius = int(policy["contextRadius"])
    neighbors: list[TextRegion] = []
    for distance in range(1, radius + 1):
        for candidate_index in (index - distance, index + distance):
            if 0 <= candidate_index < len(rows):
                neighbors.append(rows[candidate_index])
    payload = {
        "target": {
            "regionId": rows[index].id,
            "sourceTextChecksum": _text_checksum(rows[index].source_text),
            "paragraphGroupId": rows[index].paragraph_group_id,
            "readingOrder": rows[index].reading_order,
            "regionType": rows[index].region_type,
            "direction": rows[index].direction,
        },
        "neighbors": [
            {
                "regionId": row.id,
                "sourceTextChecksum": _text_checksum(row.source_text),
                "paragraphGroupId": row.paragraph_group_id,
                "readingOrder": row.reading_order,
                "regionType": row.region_type,
                "direction": row.direction,
            }
            for row in neighbors
        ],
        "policy": policy,
    }
    return neighbors, _digest(payload)


def _parameter_hash(
    generation: PageGeneration,
    *,
    provider: str,
    model_version: str,
    policy: dict[str, Any],
    provider_config: dict[str, str] | None = None,
) -> str:
    return _digest(
        {
            "contractVersion": G9_TRANSLATION_CONTRACT_VERSION,
            "generationParameterSetHash": generation.parameter_set_hash,
            "provider": provider,
            "modelVersion": model_version,
            "providerConfig": provider_config or {},
            "policy": policy,
        }
    )


def _provider_runtime_contract(provider: Any) -> tuple[str, str, dict[str, str]]:
    canonical = getattr(provider, "name", None)
    if not isinstance(canonical, str) or len(canonical) > 80:
        raise PageLineageConflict(
            "Translation provider did not expose a valid canonical identity",
            resource="translation-provider",
            reason="g9-provider-invalid",
        )
    if canonical in _REVISION_ONLY_PROVIDERS:
        raise PageLineageConflict(
            "This translation provider is revision-only in strict G9",
            resource=f"translation-provider:{canonical}",
            reason=(
                "g9-manual-job-blocked" if canonical == "manual" else "g9-dictionary-job-blocked"
            ),
        )
    if canonical in _AUTOMATIC_PROVIDER_MODELS:
        return canonical, _AUTOMATIC_PROVIDER_MODELS[canonical], {}
    if canonical == "openai-compatible":
        model = getattr(provider, "model", None)
        base_url = getattr(provider, "base_url", None)
        if (
            not isinstance(model, str)
            or not _OPAQUE_VALUE_RE.fullmatch(model)
            or not isinstance(base_url, str)
        ):
            raise PageLineageConflict(
                "OpenAI-compatible translation metadata is invalid",
                resource="translation-provider:openai-compatible",
                reason="g9-provider-invalid",
            )
        try:
            canonical_base_url = validate_remote_base_url(base_url)
        except ValueError as error:
            raise PageLineageConflict(
                "OpenAI-compatible translation endpoint is invalid",
                resource="translation-provider:openai-compatible",
                reason="g9-provider-invalid",
            ) from error
        return canonical, model, {"baseUrl": canonical_base_url}
    raise PageLineageConflict(
        "Strict G9 does not support this automatic translation provider",
        resource=f"translation-provider:{canonical}",
        reason="g9-provider-invalid",
    )


def resolve_translation_job_options(
    project_settings: dict[str, Any],
    options: dict[str, Any],
    translator_factory: Callable[[str, dict[str, Any]], Any],
) -> dict[str, Any]:
    """Freeze server-derived provider metadata before a strict G9 job is inserted."""
    resolved = dict(options)
    requested = resolved.get("provider") or project_settings.get("translatorProvider") or "manual"
    if not isinstance(requested, str) or not requested or len(requested) > 80:
        raise PageLineageConflict(
            "Strict translation provider is invalid",
            resource="translation-provider",
            reason="g9-provider-invalid",
        )
    if requested in _REVISION_ONLY_PROVIDERS:
        raise PageLineageConflict(
            "This translation provider is revision-only in strict G9",
            resource=f"translation-provider:{requested}",
            reason=(
                "g9-manual-job-blocked" if requested == "manual" else "g9-dictionary-job-blocked"
            ),
        )
    if requested in _REMOTE_TRANSLATION_PROVIDERS and resolved.get("remoteAuthorized") is not True:
        raise PageLineageConflict(
            "Remote translation requires explicit authorization",
            resource=f"translation-provider:{requested}",
            reason="g9-remote-not-authorized",
        )
    try:
        runtime = translator_factory(requested, resolved)
    except (TypeError, ValueError) as error:
        raise PageLineageConflict(
            "Strict translation provider is invalid",
            resource=f"translation-provider:{requested}",
            reason="g9-provider-invalid",
        ) from error
    canonical, model_version, provider_config = _provider_runtime_contract(runtime)
    if canonical == "openai-compatible" and resolved.get("remoteAuthorized") is not True:
        raise PageLineageConflict(
            "Remote translation requires explicit authorization",
            resource="translation-provider:openai-compatible",
            reason="g9-remote-not-authorized",
        )
    resolved["provider"] = canonical
    resolved["modelVersion"] = model_version
    if canonical == "openai-compatible":
        resolved["model"] = model_version
        resolved["baseUrl"] = provider_config["baseUrl"]
    else:
        resolved.pop("model", None)
        resolved.pop("baseUrl", None)
    return resolved


def _require_chinese_target(policy: dict[str, Any]) -> None:
    if policy.get("targetLanguage") not in {"zh-CN", "zh", "zh-Hans"}:
        raise PageLineageConflict(
            "Strict G9 currently requires a Simplified Chinese target",
            resource="translation-policy",
            reason="g9-target-language-invalid",
        )


def _computed_flags(source: str, translation: str) -> list[str]:
    source_clean = "".join(source.split())
    translated = translation.strip()
    flags: list[str] = []
    if not translated:
        flags.append("empty-output")
    meaningful = [character for character in translated if character.isalnum()]
    if translated and (
        not meaningful
        or sum(bool(_HAN_RE.fullmatch(character)) for character in meaningful) / len(meaningful)
        < 0.25
    ):
        flags.append("non-chinese-output")
    if _FORBIDDEN_RE.search(translated):
        flags.append("forbidden-template")
    if source_clean and source_clean == "".join(translated.split()):
        flags.append("source-copy")
    if _JAPANESE_RE.search(translated):
        flags.append("japanese-residual")
    source_meaningful = [character for character in source_clean if character.isalnum()]
    if len(source_meaningful) <= 1 and len(meaningful) >= 4:
        flags.append("source-noise-hallucination")
    return sorted(set(flags)) or ["none"]


def _generic_duplicate_region_ids(
    rows: list[tuple[TextRegion, str]],
) -> set[str]:
    grouped: dict[str, list[TextRegion]] = {}
    for region, translation in rows:
        normalized = "".join(
            character.casefold()
            for character in unicodedata.normalize("NFKC", translation)
            if character.isalnum()
        )
        if normalized:
            grouped.setdefault(_text_checksum(normalized), []).append(region)
    duplicate_ids: set[str] = set()
    for regions in grouped.values():
        if (
            len(regions) > 1
            and len({_text_checksum(row.source_text) for row in regions}) > 1
            and max(row.reading_order for row in regions)
            - min(row.reading_order for row in regions)
            > 1
        ):
            duplicate_ids.update(row.id for row in regions)
    return duplicate_ids


def _candidate_checksum_payload(
    *,
    generation_id: str,
    region_id: str,
    revision_number: int,
    supersedes_candidate_id: str | None,
    origin_kind: str,
    g8_checksum: str,
    clean_plate_checksum: str,
    source_text_checksum: str,
    source_region_revision: int,
    context_checksum: str,
    provider: str,
    model_version: str,
    parameter_hash: str,
    target_language: str,
    translation_text: str,
    computed_qc_flags: list[str],
    job_id: str | None,
    job_item_id: str | None,
) -> dict[str, Any]:
    return {
        "generationId": generation_id,
        "regionId": region_id,
        "revisionNumber": revision_number,
        "supersedesCandidateId": supersedes_candidate_id,
        "originKind": origin_kind,
        "g8Checksum": g8_checksum,
        "cleanPlateChecksum": clean_plate_checksum,
        "sourceTextChecksum": source_text_checksum,
        "sourceRegionRevision": source_region_revision,
        "contextChecksum": context_checksum,
        "provider": provider,
        "modelVersion": model_version,
        "parameterHash": parameter_hash,
        "targetLanguage": target_language,
        "translationTextChecksum": _text_checksum(translation_text),
        "computedQcFlags": computed_qc_flags,
        "jobId": job_id,
        "jobItemId": job_item_id,
    }


def _rows(
    session, generation_id: str
) -> tuple[list[RegionTranslationCandidate], list[RegionTranslationReview]]:
    candidates = list(
        session.scalars(
            select(RegionTranslationCandidate)
            .where(RegionTranslationCandidate.generation_id == generation_id)
            .order_by(RegionTranslationCandidate.sequence, RegionTranslationCandidate.id)
        ).all()
    )
    reviews = list(
        session.scalars(
            select(RegionTranslationReview)
            .where(RegionTranslationReview.generation_id == generation_id)
            .order_by(RegionTranslationReview.sequence, RegionTranslationReview.id)
        ).all()
    )
    return candidates, reviews


def _translation_checksum(
    candidates: list[RegionTranslationCandidate],
    reviews: list[RegionTranslationReview],
    *,
    g8_checksum: str,
) -> str:
    if not candidates and not reviews:
        return g8_checksum
    return _digest(
        {
            "candidates": [
                [
                    row.id,
                    row.sequence,
                    row.region_id,
                    row.revision_number,
                    row.supersedes_candidate_id,
                    row.origin_kind,
                    row.g8_checksum,
                    row.clean_plate_checksum,
                    row.source_text_checksum,
                    row.source_region_revision,
                    row.context_checksum,
                    row.context_policy,
                    row.provider,
                    row.model_version,
                    row.parameter_hash,
                    row.target_language,
                    _text_checksum(row.translation_text),
                    row.candidate_checksum,
                    row.computed_qc_flags,
                    row.job_id,
                    row.job_item_id,
                    row.revision_id,
                ]
                for row in candidates
            ],
            "reviews": [
                [
                    row.id,
                    row.sequence,
                    row.candidate_id,
                    row.state,
                    row.reason,
                    row.candidate_checksum,
                    row.source_text_checksum,
                    row.context_checksum,
                    row.g8_checksum,
                    row.checks,
                    row.qc_flags,
                    row.reviewer,
                    row.revision_id,
                ]
                for row in reviews
            ],
        }
    )


def translation_state_checksum(session, generation_id: str, g8_checksum: str | None = None) -> str:
    if g8_checksum is None:
        g8_checksum = session.scalar(
            select(PageLineageEvent.output_checksum)
            .where(
                PageLineageEvent.generation_id == generation_id,
                PageLineageEvent.gate.in_(("G8_cleanPlate", "G8_cloudFullPage")),
                PageLineageEvent.state.in_(("accepted", "not-applicable")),
            )
            .order_by(PageLineageEvent.sequence.desc())
            .limit(1)
        )
    if not isinstance(g8_checksum, str):
        raise PageLineageConflict(
            "Accepted G8 checksum is missing",
            resource=f"page-generation:{generation_id}",
            reason="g9-g8-missing",
        )
    return _translation_checksum(*_rows(session, generation_id), g8_checksum=g8_checksum)


def _event_actor(event: PageLineageEvent) -> dict[str, str | None]:
    return _safe_actor(
        {
            "actorKind": event.actor_kind,
            "actorId": event.actor_id,
            "taskId": event.task_id,
            "threadId": event.thread_id,
            "sessionId": event.session_id,
            "operationSource": event.operation_source,
        }
    )


def _job_actor(job: Job) -> dict[str, str | None]:
    context = job.lineage_context
    if not isinstance(context, dict) or not isinstance(context.get("actor"), dict):
        raise PageLineageConflict(
            "Translation job actor evidence is invalid",
            resource=f"job:{job.id}",
            reason="g9-replay-invalid",
        )
    return _safe_actor(context["actor"])


def _job_page_sequence(session, job: Job, generation: PageGeneration) -> int:
    context = job.lineage_context
    pages = context.get("pages") if isinstance(context, dict) else None
    if (
        not isinstance(context, dict)
        or set(context) != {"version", "runId", "actor", "pages"}
        or context.get("version") != 1
        or context.get("runId") != generation.run_id
        or not isinstance(pages, list)
    ):
        raise PageLineageConflict(
            "Translation job page evidence is invalid",
            resource=f"job:{job.id}",
            reason="g9-replay-invalid",
        )
    items = list(job.items)
    page_image_ids = [page.get("imageId") for page in pages if isinstance(page, dict)]
    page_generation_ids = [page.get("pageGenerationId") for page in pages if isinstance(page, dict)]
    item_image_ids = [item.image_id for item in items]
    item_statuses = [item.status for item in items]
    aggregate_status_valid = (
        (job.status == "queued" and all(status == "queued" for status in item_statuses))
        or (
            job.status == "running"
            and any(status in {"queued", "running"} for status in item_statuses)
        )
        or (job.status == "completed" and all(status == "completed" for status in item_statuses))
        or (
            job.status == "failed"
            and all(status in {"completed", "failed"} for status in item_statuses)
            and any(status == "failed" for status in item_statuses)
        )
    )
    if (
        len(pages) != len(items)
        or any(
            not isinstance(page, dict)
            or set(page) != {"imageId", "pageGenerationId", "expectedSequence"}
            or not isinstance(page.get("imageId"), str)
            or not isinstance(page.get("pageGenerationId"), str)
            or type(page.get("expectedSequence")) is not int
            for page in pages
        )
        or any(item.image_id is None or item.region_id is not None for item in items)
        or len(set(page_image_ids)) != len(page_image_ids)
        or len(set(page_generation_ids)) != len(page_generation_ids)
        or set(page_image_ids) != set(item_image_ids)
        or sorted(item.position for item in items) != list(range(len(items)))
        or job.total != len(items)
        or job.completed != sum(item.status == "completed" for item in items)
        or not aggregate_status_valid
    ):
        raise PageLineageConflict(
            "Translation job targets do not match its lineage page map",
            resource=f"job:{job.id}",
            reason="g9-replay-invalid",
        )
    for page in pages:
        bound = session.get(PageGeneration, page["pageGenerationId"])
        if (
            bound is None
            or bound.project_id != job.project_id
            or bound.image_id != page["imageId"]
            or bound.run_id != context["runId"]
        ):
            raise PageLineageConflict(
                "Translation job contains a forged page-generation binding",
                resource=f"job:{job.id}",
                reason="g9-replay-invalid",
            )
    matches = [
        page
        for page in pages
        if isinstance(page, dict) and page.get("imageId") == generation.image_id
    ]
    if (
        len(matches) != 1
        or set(matches[0]) != {"imageId", "pageGenerationId", "expectedSequence"}
        or matches[0].get("pageGenerationId") != generation.id
        or type(matches[0].get("expectedSequence")) is not int
    ):
        raise PageLineageConflict(
            "Translation job page evidence is ambiguous",
            resource=f"job:{job.id}",
            reason="g9-replay-invalid",
        )
    return int(matches[0]["expectedSequence"])


def _translation_job_contract(
    project_settings: dict[str, Any], generation: PageGeneration, job: Job
) -> dict[str, Any]:
    provider = job.options.get("provider")
    model_version = job.options.get("modelVersion")
    if (
        not isinstance(provider, str)
        or not isinstance(model_version, str)
        or not provider
        or len(provider) > 80
        or not _OPAQUE_VALUE_RE.fullmatch(model_version)
    ):
        raise PageLineageConflict(
            "Strict translation job has no frozen provider contract",
            resource=f"job:{job.id}",
            reason="g9-provider-invalid",
        )
    provider_config: dict[str, str] = {}
    if provider in _AUTOMATIC_PROVIDER_MODELS:
        valid_pair = model_version == _AUTOMATIC_PROVIDER_MODELS[provider]
    elif provider == "openai-compatible":
        raw_base_url = job.options.get("baseUrl")
        try:
            base_url = (
                validate_remote_base_url(raw_base_url) if isinstance(raw_base_url, str) else None
            )
        except ValueError:
            base_url = None
        valid_pair = (
            job.options.get("remoteAuthorized") is True
            and job.options.get("model") == model_version
            and isinstance(base_url, str)
            and base_url == raw_base_url
        )
        if isinstance(base_url, str):
            provider_config = {"baseUrl": base_url}
    else:
        valid_pair = False
    if not valid_pair:
        raise PageLineageConflict(
            "Strict translation provider contract is not canonical",
            resource=f"job:{job.id}",
            reason="g9-provider-invalid",
        )
    policy = _bounded_policy(project_settings)
    _require_chinese_target(policy)
    return {
        "provider": provider,
        "modelVersion": model_version,
        "parameterHash": _parameter_hash(
            generation,
            provider=provider,
            model_version=model_version,
            policy=policy,
            provider_config=provider_config,
        ),
        "policy": policy,
        "providerConfig": provider_config,
    }


def _valid_checks(checks: object) -> bool:
    return (
        isinstance(checks, list)
        and len(checks) == len(TRANSLATION_QC_CHECKS)
        and all(
            isinstance(entry, dict)
            and set(entry) == {"check", "passed"}
            and entry["check"] in TRANSLATION_QC_CHECKS
            and type(entry["passed"]) is bool
            for entry in checks
        )
        and {entry["check"] for entry in checks} == set(TRANSLATION_QC_CHECKS)
    )


def _valid_qc_flags(flags: object) -> bool:
    return (
        isinstance(flags, list)
        and bool(flags)
        and len(flags) == len(set(flags))
        and set(flags).issubset(TRANSLATION_QC_FLAGS)
        and ("none" not in flags or flags == ["none"])
    )


def _translation_review_defects(
    computed_flags: list[str],
    checks: list[dict[str, Any]],
    qc_flags: list[str],
    *,
    dynamic_duplicate: bool,
) -> set[str]:
    defects = (set(computed_flags) | set(qc_flags)) - {"none"}
    defects.update(_CHECK_FAILURE_FLAG[entry["check"]] for entry in checks if not entry["passed"])
    if dynamic_duplicate:
        defects.add("generic-duplicate")
    return defects


def _valid_translation_review_verdict(
    *,
    state: str,
    reason: str,
    checks: list[dict[str, Any]],
    qc_flags: list[str],
    defects: set[str],
) -> bool:
    if state == "accepted":
        return (
            reason == "translation-reviewed"
            and not defects
            and qc_flags == ["none"]
            and all(entry["passed"] for entry in checks)
        )
    return (
        state == "rejected"
        and reason in TRANSLATION_REJECT_REASONS
        and bool(defects)
        and (
            (reason == "multiple-qc-failures" and len(defects) > 1)
            or (reason != "multiple-qc-failures" and reason in defects)
        )
    )


def _revision_matches(
    session,
    generation: PageGeneration,
    revision_id: str,
    *,
    entity_type: str,
    entity_id: str,
    operation: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    revision = session.get(Revision, revision_id)
    return bool(
        revision is not None
        and revision.project_id == generation.project_id
        and revision.entity_type == entity_type
        and revision.entity_id == entity_id
        and revision.operation == operation
        and revision.before == before
        and revision.after == after
    )


def validate_translation_replay(
    session,
    generation: PageGeneration,
    *,
    g8_checksum: str,
) -> tuple[str, PageTranslationReview | None]:
    def invalid(message: str, resource: str | None = None) -> None:
        raise PageLineageConflict(
            message,
            resource=resource or f"page-generation:{generation.id}",
            reason="g9-replay-invalid",
        )

    image = session.get(ImageAsset, generation.image_id)
    project = session.get(Project, generation.project_id)
    if image is None or project is None or image.project_id != project.id:
        invalid("G9 page ownership is invalid")
    assert image is not None and project is not None
    project_settings = dict(project.settings)
    policy = _bounded_policy(project_settings)
    _require_chinese_target(policy)
    eligible = eligible_translation_regions(session, image.id)
    eligible_by_id = {row.id: row for row in eligible}

    all_events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(PageLineageEvent.generation_id == generation.id)
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    if [event.sequence for event in all_events] != list(range(1, len(all_events) + 1)) or (
        generation.next_sequence != len(all_events) + 1
    ):
        invalid("Page generation event sequence is not contiguous")
    g8_terminal = next(
        (
            event
            for event in reversed(all_events)
            if (
                (event.gate == "G8_cleanPlate" and event.operation == "clean-plate-stage-review")
                or (
                    event.gate == "G8_cloudFullPage"
                    and event.operation == "cloud-full-page-stage-review"
                )
            )
            and event.state in {"accepted", "not-applicable"}
        ),
        None,
    )
    if g8_terminal is None or g8_terminal.output_checksum != g8_checksum:
        invalid("G9 has no exact accepted G8 parent")
    clean_plate_checksum = (g8_terminal.evidence or {}).get("candidateChecksum") or (
        g8_terminal.evidence or {}
    ).get("qualityChecksum")
    if not isinstance(clean_plate_checksum, str) or len(clean_plate_checksum) != 64:
        invalid("G9 clean-plate checksum evidence is missing")

    g9_events: list[PageLineageEvent] = []
    terminal_event_seen = False
    downstream_seen = False
    for event in all_events[g8_terminal.sequence :]:
        if event.gate == "G9_translation":
            if downstream_seen or terminal_event_seen:
                invalid("G9 evidence changed after its terminal boundary", f"event:{event.id}")
            g9_events.append(event)
            terminal_event_seen = event.operation == "translation-stage-review"
        else:
            if not terminal_event_seen:
                invalid("A downstream gate started before terminal G9", f"event:{event.id}")
            downstream_seen = True

    candidates, reviews = _rows(session, generation.id)
    terminal = session.scalar(
        select(PageTranslationReview).where(PageTranslationReview.generation_id == generation.id)
    )
    candidate_groups: dict[int, list[RegionTranslationCandidate]] = {}
    for row in candidates:
        candidate_groups.setdefault(row.sequence, []).append(row)
    review_groups: dict[int, list[RegionTranslationReview]] = {}
    for row in reviews:
        review_groups.setdefault(row.sequence, []).append(row)
    if any(sequence <= g8_terminal.sequence for sequence in candidate_groups | review_groups):
        invalid("G9 rows precede their accepted G8 parent")

    job_items: dict[str, tuple[JobItem, Job, int]] = {}
    for item, job in session.execute(
        select(JobItem, Job)
        .join(Job)
        .where(
            Job.kind == "translate",
            Job.lineage_context.is_not(None),
            JobItem.image_id == image.id,
        )
    ).all():
        if job.project_id != generation.project_id or item.region_id is not None:
            invalid("G9 job ownership is invalid", f"job:{job.id}")
        try:
            expected_sequence = _job_page_sequence(session, job, generation)
        except PageLineageConflict:
            context = job.lineage_context
            pages = context.get("pages") if isinstance(context, dict) else None
            belongs_to_generation = isinstance(pages, list) and any(
                isinstance(page, dict) and page.get("pageGenerationId") == generation.id
                for page in pages
            )
            if belongs_to_generation:
                invalid("G9 job has ambiguous lineage", f"job:{job.id}")
            continue
        job_items[item.id] = (item, job, expected_sequence)

    current_state = g8_checksum
    candidate_prefix: list[RegionTranslationCandidate] = []
    review_prefix: list[RegionTranslationReview] = []
    candidates_by_id: dict[str, RegionTranslationCandidate] = {}
    reviews_by_candidate: dict[str, RegionTranslationReview] = {}
    latest_by_region: dict[str, RegionTranslationCandidate] = {}
    source_revisions_by_region: dict[str, int] = {}
    enqueued_items: set[str] = set()
    completed_items: set[str] = set()
    enqueue_states: dict[str, str] = {}
    contracts: dict[str, dict[str, Any]] = {}
    produced_rows: dict[str, list[RegionTranslationCandidate]] = {}
    open_item: str | None = None
    replay_terminal: PageTranslationReview | None = None
    consumed_candidate_ids: set[str] = set()
    consumed_review_ids: set[str] = set()

    def candidate_checksum(row: RegionTranslationCandidate) -> str:
        return _digest(
            _candidate_checksum_payload(
                generation_id=row.generation_id,
                region_id=row.region_id,
                revision_number=row.revision_number,
                supersedes_candidate_id=row.supersedes_candidate_id,
                origin_kind=row.origin_kind,
                g8_checksum=row.g8_checksum,
                clean_plate_checksum=row.clean_plate_checksum,
                source_text_checksum=row.source_text_checksum,
                source_region_revision=row.source_region_revision,
                context_checksum=row.context_checksum,
                provider=row.provider,
                model_version=row.model_version,
                parameter_hash=row.parameter_hash,
                target_language=row.target_language,
                translation_text=row.translation_text,
                computed_qc_flags=row.computed_qc_flags,
                job_id=row.job_id,
                job_item_id=row.job_item_id,
            )
        )

    def validate_candidate(
        row: RegionTranslationCandidate,
        *,
        expected_origin: str,
        expected_provider: str,
        expected_model: str,
        expected_parameter_hash: str,
        expected_job_id: str | None,
        expected_job_item_id: str | None,
        expected_revision_number: int,
        expected_parent_id: str | None,
        expected_flags: list[str],
        expected_before: dict[str, Any] | None,
        expected_operation: str,
    ) -> None:
        region = eligible_by_id.get(row.region_id)
        if region is None:
            invalid("G9 candidate targets an ineligible region", f"translation-candidate:{row.id}")
        assert region is not None
        index = eligible.index(region)
        _neighbors, context_checksum = _context_for(eligible, index, policy)
        expected_after = {
            "candidateId": row.id,
            "regionId": row.region_id,
            "candidateChecksum": row.candidate_checksum,
        }
        if (
            row.generation_id != generation.id
            or row.image_id != image.id
            or row.origin_kind != expected_origin
            or row.revision_number != expected_revision_number
            or row.supersedes_candidate_id != expected_parent_id
            or row.g8_checksum != g8_checksum
            or row.clean_plate_checksum != clean_plate_checksum
            or row.source_text_checksum != _text_checksum(region.source_text)
            or row.context_checksum != context_checksum
            or row.context_policy != policy
            or row.provider != expected_provider
            or row.model_version != expected_model
            or row.parameter_hash != expected_parameter_hash
            or row.target_language != policy["targetLanguage"]
            or row.job_id != expected_job_id
            or row.job_item_id != expected_job_item_id
            or row.computed_qc_flags != expected_flags
            or row.candidate_checksum != candidate_checksum(row)
            or not _revision_matches(
                session,
                generation,
                row.revision_id,
                entity_type="translation-candidate",
                entity_id=row.id,
                operation=expected_operation,
                before=expected_before,
                after=expected_after,
            )
        ):
            invalid("G9 candidate evidence is inconsistent", f"translation-candidate:{row.id}")
        prior_source_revision = source_revisions_by_region.setdefault(
            row.region_id, row.source_region_revision
        )
        if row.source_region_revision != prior_source_revision or row.source_region_revision < 0:
            invalid(
                "G9 candidate source revision chain is inconsistent",
                f"translation-candidate:{row.id}",
            )

    for event in g9_events:
        try:
            event_actor = _event_actor(event)
        except PageLineageConflict:
            invalid("G9 event actor is invalid", f"event:{event.id}")
        if (
            event.gate != "G9_translation"
            or event.stage != "translation"
            or event.parent_checksum != g8_checksum
            or event.git_commit is not None
        ):
            invalid("G9 event common fields are invalid", f"event:{event.id}")
        evidence = event.evidence
        if not isinstance(evidence, dict):
            invalid("G9 event evidence is invalid", f"event:{event.id}")

        if event.operation == "translate-job-enqueued":
            item_id = event.job_item_id
            matched = job_items.get(str(item_id))
            if matched is None:
                invalid("G9 enqueue has no exact job item", f"event:{event.id}")
            item, job, expected_sequence = matched
            try:
                contract = _translation_job_contract(project_settings, generation, job)
                actor = _job_actor(job)
            except PageLineageConflict:
                invalid("G9 enqueue job contract is invalid", f"event:{event.id}")
            if (
                open_item is not None
                or item_id in enqueued_items
                or event.sequence != expected_sequence
                or event.job_id != job.id
                or event.state != "pending"
                or event.decision is not None
                or event.reason != "job-enqueued"
                or event.revision_id is not None
                or event.input_checksum != current_state
                or event.output_checksum != current_state
                or event.provider != contract["provider"]
                or event.model_version != contract["modelVersion"]
                or event.parameter_hash != contract["parameterHash"]
                or event_actor != actor
                or event.started_at != job.created_at
                or event.finished_at is not None
                or evidence
                != {
                    "eventType": "job-enqueued",
                    "qualityState": "pending-review",
                    "targetKind": "image",
                    "eligibleRegionCount": len(eligible),
                }
            ):
                invalid("G9 enqueue event matrix is invalid", f"event:{event.id}")
            enqueued_items.add(str(item_id))
            enqueue_states[str(item_id)] = current_state
            contracts[str(item_id)] = contract
            open_item = str(item_id)
            continue

        if event.operation == "translation-candidates-produced":
            item_id = str(event.job_item_id)
            matched = job_items.get(item_id)
            rows = candidate_groups.get(event.sequence, [])
            if matched is None or item_id != open_item:
                invalid("G9 publication is not the open job item", f"event:{event.id}")
            item, job, _expected_sequence = matched
            contract = contracts[item_id]
            actor = _job_actor(job)
            if (
                candidate_prefix
                or len(rows) != len(eligible)
                or {row.region_id for row in rows} != set(eligible_by_id)
                or event.job_id != job.id
                or event.state != "pending"
                or event.decision != "candidates-produced"
                or event.reason != "review-required"
                or event.revision_id is not None
                or event.input_checksum != current_state
                or event.provider != contract["provider"]
                or event.model_version != contract["modelVersion"]
                or event.parameter_hash != contract["parameterHash"]
                or event_actor != actor
                or event.started_at is not None
                or event.finished_at is not None
                or evidence
                != {
                    "eventType": "translation-candidates-produced",
                    "qualityState": "pending-review",
                    "targetKind": "region-set",
                    "eligibleRegionCount": len(eligible),
                    "candidateCount": len(rows),
                }
            ):
                invalid("G9 publication event matrix is invalid", f"event:{event.id}")
            duplicate_ids = _generic_duplicate_region_ids(
                [(eligible_by_id[row.region_id], row.translation_text) for row in rows]
            )
            for row in rows:
                flags = _computed_flags(
                    eligible_by_id[row.region_id].source_text, row.translation_text
                )
                if row.region_id in duplicate_ids:
                    flags = sorted((set(flags) - {"none"}) | {"generic-duplicate"})
                validate_candidate(
                    row,
                    expected_origin="model",
                    expected_provider=contract["provider"],
                    expected_model=contract["modelVersion"],
                    expected_parameter_hash=contract["parameterHash"],
                    expected_job_id=job.id,
                    expected_job_item_id=item.id,
                    expected_revision_number=1,
                    expected_parent_id=None,
                    expected_flags=flags,
                    expected_before=None,
                    expected_operation="create",
                )
                candidates_by_id[row.id] = row
                latest_by_region[row.region_id] = row
                consumed_candidate_ids.add(row.id)
            candidate_prefix.extend(rows)
            current_state = _translation_checksum(
                candidate_prefix, review_prefix, g8_checksum=g8_checksum
            )
            if event.output_checksum != current_state:
                invalid("G9 publication checksum is not its exact prefix", f"event:{event.id}")
            produced_rows[item_id] = rows
            continue

        if event.operation in {"translate-job-completed", "translate-job-failed"}:
            item_id = str(event.job_item_id)
            matched = job_items.get(item_id)
            if matched is None or item_id != open_item:
                invalid("G9 job terminal event has no open item", f"event:{event.id}")
            item, job, _expected_sequence = matched
            contract = contracts[item_id]
            actor = _job_actor(job)
            succeeded = event.operation == "translate-job-completed"
            rows = produced_rows.get(item_id, [])
            if (
                event.job_id != job.id
                or event.state != ("pending" if succeeded else "blocked")
                or event.decision is not None
                or event.reason != ("review-required" if succeeded else "job-execution-failed")
                or event.revision_id is not None
                or event.input_checksum != enqueue_states[item_id]
                or event.output_checksum != (current_state if succeeded else None)
                or event.provider != contract["provider"]
                or event.model_version != contract["modelVersion"]
                or event.parameter_hash != contract["parameterHash"]
                or event_actor != actor
                or item.status != ("completed" if succeeded else "failed")
                or event.started_at != item.started_at
                or event.finished_at != item.finished_at
                or bool(rows) is not succeeded
                or evidence
                != {
                    "eventType": "job-completed" if succeeded else "job-failed",
                    "qualityState": "pending-review" if succeeded else "blocked",
                    "targetKind": "image",
                    "candidateCount": len(rows),
                }
            ):
                invalid("G9 job terminal event matrix is invalid", f"event:{event.id}")
            if succeeded:
                completed_items.add(item_id)
            open_item = None
            continue

        if event.operation == "translation-candidate-revised":
            rows = candidate_groups.get(event.sequence, [])
            if open_item is not None or len(rows) != 1:
                invalid("G9 revision is interleaved or ambiguous", f"event:{event.id}")
            row = rows[0]
            previous = latest_by_region.get(row.region_id)
            previous_review = reviews_by_candidate.get(previous.id) if previous else None
            if previous is not None and (
                previous_review is None or previous_review.state != "rejected"
            ):
                invalid("G9 revision does not follow a rejected candidate", f"event:{event.id}")
            try:
                provider, model = _revision_provenance(event_actor, row.origin_kind)
            except PageLineageConflict:
                invalid("G9 revision actor/origin is invalid", f"event:{event.id}")
            parameter_hash = _parameter_hash(
                generation, provider=provider, model_version=model, policy=policy
            )
            flags = (
                _computed_flags(
                    eligible_by_id.get(row.region_id, row).source_text, row.translation_text
                )
                if row.region_id in eligible_by_id
                else []
            )
            validate_candidate(
                row,
                expected_origin=row.origin_kind,
                expected_provider=provider,
                expected_model=model,
                expected_parameter_hash=parameter_hash,
                expected_job_id=None,
                expected_job_item_id=None,
                expected_revision_number=1 if previous is None else previous.revision_number + 1,
                expected_parent_id=previous.id if previous else None,
                expected_flags=flags,
                expected_before={"supersedesCandidateId": previous.id} if previous else None,
                expected_operation="revise",
            )
            if (
                event.state != "pending"
                or event.decision != "candidate-revised"
                or event.reason != "review-required"
                or event.job_id is not None
                or event.job_item_id is not None
                or event.revision_id != row.revision_id
                or event.input_checksum != current_state
                or event.provider != provider
                or event.model_version != model
                or event.parameter_hash != parameter_hash
                or event.started_at is not None
                or event.finished_at is not None
                or evidence
                != {
                    "eventType": "translation-candidate-revised",
                    "qualityState": "pending-review",
                    "targetKind": "region",
                    "targetRegionId": row.region_id,
                    "candidateId": row.id,
                    "candidateChecksum": row.candidate_checksum,
                    "revisionNumber": row.revision_number,
                }
            ):
                invalid("G9 revision event matrix is invalid", f"event:{event.id}")
            candidate_prefix.append(row)
            candidates_by_id[row.id] = row
            latest_by_region[row.region_id] = row
            consumed_candidate_ids.add(row.id)
            current_state = _translation_checksum(
                candidate_prefix, review_prefix, g8_checksum=g8_checksum
            )
            if event.output_checksum != current_state:
                invalid("G9 revision checksum is not its exact prefix", f"event:{event.id}")
            continue

        if event.operation == "translation-candidate-reviewed":
            rows = review_groups.get(event.sequence, [])
            if open_item is not None or len(rows) != 1:
                invalid("G9 review is interleaved or ambiguous", f"event:{event.id}")
            review = rows[0]
            candidate = candidates_by_id.get(review.candidate_id)
            if (
                candidate is None
                or latest_by_region.get(review.region_id) is not candidate
                or review.candidate_id in reviews_by_candidate
                or (
                    candidate.job_item_id is not None
                    and candidate.job_item_id not in completed_items
                )
            ):
                invalid("G9 review target is not current and completed", f"event:{event.id}")
            actor = event_actor
            dynamic_duplicates = _generic_duplicate_region_ids(
                [
                    (eligible_by_id[region_id], row.translation_text)
                    for region_id, row in latest_by_region.items()
                    if region_id in eligible_by_id
                ]
            )
            if not _valid_checks(review.checks) or not _valid_qc_flags(review.qc_flags):
                invalid("G9 review QC structure is invalid", f"translation-review:{review.id}")
            defects = _translation_review_defects(
                candidate.computed_qc_flags,
                review.checks,
                review.qc_flags,
                dynamic_duplicate=candidate.region_id in dynamic_duplicates,
            )
            accepted = review.state == "accepted"
            verdict_valid = _valid_translation_review_verdict(
                state=review.state,
                reason=review.reason,
                checks=review.checks,
                qc_flags=review.qc_flags,
                defects=defects,
            )
            expected_after = {
                "candidateId": candidate.id,
                "state": review.state,
                "candidateChecksum": candidate.candidate_checksum,
                "compatibilityProjection": accepted,
            }
            if (
                not verdict_valid
                or review.generation_id != generation.id
                or review.image_id != image.id
                or review.region_id != candidate.region_id
                or review.candidate_checksum != candidate.candidate_checksum
                or review.source_text_checksum != candidate.source_text_checksum
                or review.context_checksum != candidate.context_checksum
                or review.g8_checksum != g8_checksum
                or review.reviewer != actor
                or not _revision_matches(
                    session,
                    generation,
                    review.revision_id,
                    entity_type="translation-review",
                    entity_id=review.id,
                    operation="review",
                    before=None,
                    after=expected_after,
                )
                or event.state != review.state
                or event.decision != f"candidate-{review.state}"
                or event.reason != review.reason
                or event.job_id is not None
                or event.job_item_id is not None
                or event.revision_id != review.revision_id
                or event.input_checksum != current_state
                or event.provider != candidate.provider
                or event.model_version != candidate.model_version
                or event.parameter_hash != candidate.parameter_hash
                or event.started_at is not None
                or event.finished_at is not None
                or evidence
                != {
                    "eventType": "translation-candidate-reviewed",
                    "qualityState": review.state,
                    "targetKind": "region",
                    "targetRegionId": candidate.region_id,
                    "candidateId": candidate.id,
                    "candidateChecksum": candidate.candidate_checksum,
                    "reviewedRegionCount": 1,
                }
            ):
                invalid("G9 review evidence is inconsistent", f"translation-review:{review.id}")
            review_prefix.append(review)
            reviews_by_candidate[candidate.id] = review
            consumed_review_ids.add(review.id)
            current_state = _translation_checksum(
                candidate_prefix, review_prefix, g8_checksum=g8_checksum
            )
            if event.output_checksum != current_state:
                invalid("G9 review checksum is not its exact prefix", f"event:{event.id}")
            continue

        if event.operation != "translation-stage-review":
            invalid("G9 contains an unsupported operation", f"event:{event.id}")
        if open_item is not None or terminal is None or terminal.sequence != event.sequence:
            invalid("G9 terminal event has no exact row", f"event:{event.id}")
        actor = event_actor
        all_candidates_reviewed = len(reviews_by_candidate) == len(candidate_prefix)
        accepted_ids = sorted(
            row.id
            for row in latest_by_region.values()
            if reviews_by_candidate.get(row.id) is not None
            and reviews_by_candidate[row.id].state == "accepted"
        )
        if terminal.state == "not-applicable":
            terminal_semantics = (
                not eligible
                and not candidate_prefix
                and not review_prefix
                and not enqueued_items
                and terminal.reason == "no-translatable-regions"
                and terminal.accepted_candidate_ids == []
            )
        else:
            terminal_semantics = (
                terminal.state == "accepted"
                and terminal.reason == "all-translations-reviewed"
                and bool(eligible)
                and all_candidates_reviewed
                and set(latest_by_region) == set(eligible_by_id)
                and len(accepted_ids) == len(eligible)
                and terminal.accepted_candidate_ids == accepted_ids
                and not _generic_duplicate_region_ids(
                    [
                        (eligible_by_id[region_id], row.translation_text)
                        for region_id, row in latest_by_region.items()
                    ]
                )
            )
        expected_terminal = _digest(
            {
                "terminalId": terminal.id,
                "generationId": generation.id,
                "g8Checksum": g8_checksum,
                "translationStateChecksum": current_state,
                "state": terminal.state,
                "reason": terminal.reason,
                "acceptedCandidateIds": terminal.accepted_candidate_ids,
                "reviewer": actor,
                "revisionId": terminal.revision_id,
            }
        )
        expected_after = {
            "state": terminal.state,
            "translationStateChecksum": current_state,
            "acceptedCandidateIds": terminal.accepted_candidate_ids,
        }
        if (
            not terminal_semantics
            or terminal.generation_id != generation.id
            or terminal.image_id != image.id
            or terminal.g8_checksum != g8_checksum
            or terminal.translation_state_checksum != current_state
            or terminal.reviewer != actor
            or terminal.terminal_checksum != expected_terminal
            or not _revision_matches(
                session,
                generation,
                terminal.revision_id,
                entity_type="translation-page-review",
                entity_id=terminal.id,
                operation="review",
                before=None,
                after=expected_after,
            )
            or event.state != terminal.state
            or event.decision
            != (
                "translations-accepted"
                if terminal.state == "accepted"
                else "translation-not-applicable"
            )
            or event.reason != terminal.reason
            or event.job_id is not None
            or event.job_item_id is not None
            or event.revision_id != terminal.revision_id
            or event.input_checksum != current_state
            or event.output_checksum != terminal.terminal_checksum
            or event.provider is not None
            or event.model_version is not None
            or event.parameter_hash != generation.parameter_set_hash
            or event.started_at is not None
            or event.finished_at is not None
            or evidence
            != {
                "eventType": "translation-stage-review",
                "qualityState": terminal.state,
                "targetKind": "region-set",
                "eligibleRegionCount": len(eligible),
                "candidateCount": len(candidate_prefix),
                "reviewedRegionCount": len(review_prefix),
            }
        ):
            invalid("G9 terminal evidence is inconsistent", f"event:{event.id}")
        replay_terminal = terminal

    if consumed_candidate_ids != {row.id for row in candidates} or consumed_review_ids != {
        row.id for row in reviews
    }:
        invalid("Stored G9 rows do not have one-to-one publication events")
    if (terminal is None) != (replay_terminal is None):
        invalid("Stored G9 terminal row/event cardinality is invalid")
    if terminal_event_seen != (replay_terminal is not None):
        invalid("G9 terminal boundary is inconsistent")
    if set(job_items) - enqueued_items:
        invalid("Current-generation translation job has no enqueue event")
    if open_item is not None:
        item, _job, _sequence = job_items[open_item]
        has_publication = open_item in produced_rows
        if item.status not in {"queued", "running"} or (
            has_publication and item.status != "running"
        ):
            invalid("Open G9 job item state is inconsistent", f"job-item:{open_item}")
        if review_prefix or replay_terminal is not None:
            invalid("G9 review began before its job completed", f"job-item:{open_item}")

    for region_id, region in eligible_by_id.items():
        latest = latest_by_region.get(region_id)
        if latest is None:
            if region.translation_text != "" or region.translation_provider is not None:
                invalid(
                    "G9 compatibility projection exists without a candidate", f"region:{region_id}"
                )
            continue
        latest_review = reviews_by_candidate.get(latest.id)
        accepted = latest_review is not None and latest_review.state == "accepted"
        base_revision = source_revisions_by_region[region_id]
        if region.revision != base_revision + int(accepted):
            invalid("G9 compatibility projection revision is stale", f"region:{region_id}")
        if accepted and (
            region.translation_text != latest.translation_text
            or region.translation_provider != latest.provider
        ):
            invalid("G9 compatibility projection is inconsistent", f"region:{region_id}")
        if not accepted and (
            region.translation_text != "" or region.translation_provider is not None
        ):
            invalid(
                "Unaccepted G9 candidate leaked into compatibility fields", f"region:{region_id}"
            )

    if downstream_seen:
        if replay_terminal is None:
            invalid("A downstream gate has no terminal G9 parent")
        from manga_localizer.services.typesets import validate_g10_prefix_after_g9

        validate_g10_prefix_after_g9(
            session,
            generation,
            g9_terminal_checksum=replay_terminal.terminal_checksum,
        )

    return current_state, replay_terminal


def _current_bindings(store: ProjectStore, session, image: ImageAsset, generation: PageGeneration):
    g6_checksum, _ = require_current_ocr_trust(store, session, image, generation)
    g8_checksum, clean_path, clean_candidate = require_current_clean_plate_acceptance(
        store, session, image, generation
    )
    clean_checksum = (
        clean_candidate.candidate_checksum
        if clean_candidate is not None
        else hashlib.sha256(clean_path.read_bytes()).hexdigest()
    )
    eligible = eligible_translation_regions(session, image.id)
    if any(row.ocr_generation_id != generation.id for row in eligible):
        raise PageLineageConflict(
            "Translation eligibility contains stale OCR trust",
            resource=f"image:{image.id}",
            reason="g9-eligibility-mismatch",
        )
    return g6_checksum, g8_checksum, clean_checksum, clean_candidate, eligible


def _public_review(review: RegionTranslationReview | None) -> dict[str, Any] | None:
    if review is None:
        return None
    return {
        "id": review.id,
        "state": review.state,
        "reason": review.reason,
        "checks": review.checks,
        "qcFlags": review.qc_flags,
        "reviewer": review.reviewer,
        "createdAt": review.created_at,
    }


def _public_candidate(
    row: RegionTranslationCandidate, review: RegionTranslationReview | None
) -> dict[str, Any]:
    return {
        "candidateId": row.id,
        "sequence": row.sequence,
        "regionId": row.region_id,
        "revisionNumber": row.revision_number,
        "supersedesCandidateId": row.supersedes_candidate_id,
        "originKind": row.origin_kind,
        "provider": row.provider,
        "modelVersion": row.model_version,
        "parameterHash": row.parameter_hash,
        "targetLanguage": row.target_language,
        "g8Checksum": row.g8_checksum,
        "cleanPlateChecksum": row.clean_plate_checksum,
        "sourceTextChecksum": row.source_text_checksum,
        "sourceRegionRevision": row.source_region_revision,
        "contextChecksum": row.context_checksum,
        "translationText": row.translation_text,
        "candidateChecksum": row.candidate_checksum,
        "computedQcFlags": row.computed_qc_flags,
        "jobId": row.job_id,
        "jobItemId": row.job_item_id,
        "revisionId": row.revision_id,
        "review": _public_review(review),
        "createdAt": row.created_at,
    }


def translation_gate_context(store: ProjectStore, image_id: str) -> dict[str, Any]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Translation image was not found")
        generation = _active(session, image)
        _g6, g8_checksum, clean_checksum, clean_candidate, eligible = _current_bindings(
            store, session, image, generation
        )
        replay_checksum, replay_terminal = validate_translation_replay(
            session, generation, g8_checksum=g8_checksum
        )
        project = store.project(session)
        policy = _bounded_policy(dict(project.settings))
        _require_chinese_target(policy)
        candidates, reviews = _rows(session, generation.id)
        reviews_by_candidate = {row.candidate_id: row for row in reviews}
        terminal = replay_terminal
        if (
            terminal is not None
            and terminal.translation_state_checksum
            != translation_state_checksum(session, generation.id)
        ):
            raise PageLineageConflict(
                "G9 evidence changed after terminal review",
                resource=f"page-generation:{generation.id}",
                reason="g9-accepted-state-mutated",
            )
        eligible_out = []
        for index, region in enumerate(eligible):
            neighbors, context_checksum = _context_for(eligible, index, policy)
            eligible_out.append(
                {
                    "regionId": region.id,
                    "readingOrder": region.reading_order,
                    "regionType": region.region_type,
                    "direction": region.direction,
                    "paragraphGroupId": region.paragraph_group_id,
                    "sourceText": region.source_text,
                    "sourceTextChecksum": _text_checksum(region.source_text),
                    "contextRegionIds": [row.id for row in neighbors],
                    "contextChecksum": context_checksum,
                    "rubyExcluded": True,
                }
            )
        latest: dict[str, RegionTranslationCandidate] = {}
        for candidate in candidates:
            latest[candidate.region_id] = candidate
        accepted = {
            region_id: candidate.id
            for region_id, candidate in latest.items()
            if (review := reviews_by_candidate.get(candidate.id)) is not None
            and review.state == "accepted"
        }
        return {
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "nextSequence": generation.next_sequence,
            "g8Checksum": g8_checksum,
            "cleanPlateCandidateId": clean_candidate.id if clean_candidate is not None else None,
            "cleanPlateChecksum": clean_checksum,
            "targetLanguage": policy["targetLanguage"],
            "translationStateChecksum": replay_checksum,
            "state": terminal.state if terminal is not None else "pending",
            "terminalChecksum": terminal.terminal_checksum if terminal is not None else None,
            "eligibleRegions": eligible_out,
            "candidates": [
                _public_candidate(row, reviews_by_candidate.get(row.id)) for row in candidates
            ],
            "acceptedCandidateIdsByRegion": accepted,
            "reviewedRegionCount": len(accepted),
        }


def prepare_translation_enqueue(
    store: ProjectStore,
    session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    job: Job,
    item: JobItem,
) -> dict[str, Any]:
    if item.region_id is not None:
        raise PageLineageConflict(
            "Strict G9 translate requires one whole-page job item",
            resource=f"job-item:{item.id}",
            reason="g9-whole-page-required",
        )
    _g6, g8_checksum, clean_checksum, _clean_candidate, eligible = _current_bindings(
        store, session, image, generation
    )
    if not eligible:
        raise PageLineageConflict(
            "A zero-eligible page must use G9 not-applicable",
            resource=f"image:{image.id}",
            reason="g9-no-translatable-regions",
        )
    if (
        session.scalar(
            select(PageTranslationReview).where(
                PageTranslationReview.generation_id == generation.id
            )
        )
        is not None
    ):
        raise PageLineageConflict(
            "Accepted G9 evidence is immutable",
            resource=f"image:{image.id}",
            reason="g9-translation-accepted",
        )
    active = session.scalar(
        select(JobItem.id)
        .join(Job)
        .where(
            Job.id != job.id,
            Job.kind == "translate",
            Job.lineage_context.is_not(None),
            JobItem.image_id == image.id,
            JobItem.status.in_(("queued", "running")),
        )
        .limit(1)
    )
    if active is not None:
        raise PageLineageConflict(
            "Another strict translation job is active",
            resource=f"image:{image.id}",
            reason="g9-translation-job-active",
        )
    if (
        session.scalar(
            select(RegionTranslationCandidate.id)
            .where(RegionTranslationCandidate.generation_id == generation.id)
            .limit(1)
        )
        is not None
    ):
        raise PageLineageConflict(
            "Automatic translation cannot overwrite existing candidate history",
            resource=f"image:{image.id}",
            reason="g9-candidate-history-exists",
        )
    project_settings = dict(store.project(session).settings)
    contract = _translation_job_contract(project_settings, generation, job)
    return {
        "g8Checksum": g8_checksum,
        "cleanPlateChecksum": clean_checksum,
        "stateChecksum": translation_state_checksum(session, generation.id),
        "eligibleRegionCount": len(eligible),
        "provider": contract["provider"],
        "modelVersion": contract["modelVersion"],
        "parameterHash": contract["parameterHash"],
        "policy": contract["policy"],
        "providerConfig": contract["providerConfig"],
    }


def publish_translation_candidates(
    store: ProjectStore,
    *,
    job: Job,
    item: JobItem,
    binding: JobMutationBinding,
    translator_factory: Callable[[str, dict[str, Any]], Any],
) -> dict[str, Any]:
    # Snapshot all provider inputs, then close the transaction before invoking
    # any provider. Publication reopens and revalidates the complete binding.
    with store.session() as session:
        image = session.get(ImageAsset, item.image_id)
        generation = session.get(PageGeneration, binding["generationId"])
        current_job = session.get(Job, job.id)
        current_item = session.get(JobItem, item.id)
        if image is None or generation is None or current_job is None or current_item is None:
            raise ProjectError("Translation job evidence disappeared")
        enqueued = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "translate-job-enqueued",
            )
        )
        existing = list(
            session.scalars(
                select(RegionTranslationCandidate).where(
                    RegionTranslationCandidate.job_item_id == item.id
                )
            ).all()
        )
        if existing:
            produced = session.scalar(
                select(PageLineageEvent).where(
                    PageLineageEvent.job_item_id == item.id,
                    PageLineageEvent.operation == "translation-candidates-produced",
                )
            )
            _g6, g8_checksum, _clean, _clean_candidate, eligible = _current_bindings(
                store, session, image, generation
            )
            validate_translation_replay(session, generation, g8_checksum=g8_checksum)
            if (
                enqueued is None
                or produced is None
                or produced.sequence != existing[0].sequence
                or produced.output_checksum
                != translation_state_checksum(session, generation.id, g8_checksum)
                or len(existing) != len(eligible)
                or {row.region_id for row in existing} != {row.id for row in eligible}
                or any(
                    row.g8_checksum != g8_checksum or row.revision_id is None for row in existing
                )
            ):
                raise PageLineageConflict(
                    "Recovered translation publication is incomplete",
                    resource=f"job-item:{item.id}",
                    reason="g9-publication-mismatch",
                )
            return {
                "provider": produced.provider,
                "modelVersion": produced.model_version,
                "parameterHash": produced.parameter_hash,
                "count": len(existing),
            }
        prepared = prepare_translation_enqueue(
            store, session, image=image, generation=generation, job=current_job, item=current_item
        )
        if enqueued is None or enqueued.input_checksum != prepared["stateChecksum"]:
            raise PageLineageConflict(
                "Translation enqueue evidence is stale",
                resource=f"job-item:{item.id}",
                reason="g9-lineage-mismatch",
            )
        eligible = eligible_translation_regions(session, image.id)
        snapshots = []
        for index, region in enumerate(eligible):
            neighbors, context_checksum = _context_for(eligible, index, prepared["policy"])
            snapshots.append(
                {
                    "regionId": region.id,
                    "regionRevision": region.revision,
                    "sourceText": region.source_text,
                    "sourceTextChecksum": _text_checksum(region.source_text),
                    "contextTexts": [row.source_text for row in neighbors],
                    "contextChecksum": context_checksum,
                }
            )
        provider_name = prepared["provider"]
        job_options = dict(current_job.options)
        frozen_prepared = dict(prepared)

    provider = translator_factory(provider_name, job_options)
    runtime_provider, runtime_model, runtime_config = _provider_runtime_contract(provider)
    if (
        runtime_provider != frozen_prepared["provider"]
        or runtime_model != frozen_prepared["modelVersion"]
        or runtime_config != frozen_prepared["providerConfig"]
    ):
        raise PageLineageConflict(
            "Translation provider runtime does not match the frozen job contract",
            resource=f"job-item:{item.id}",
            reason="g9-provider-runtime-mismatch",
        )
    translated = [
        provider.translate_text(
            row["sourceText"],
            row["contextTexts"],
            glossary=frozen_prepared["policy"]["glossary"],
            character_names=frozen_prepared["policy"]["characterNames"],
            target_language=frozen_prepared["policy"]["targetLanguage"],
        )
        for row in snapshots
    ]

    with store.session() as session:
        image = session.get(ImageAsset, item.image_id)
        generation = session.get(PageGeneration, binding["generationId"])
        current_job = session.get(Job, job.id)
        current_item = session.get(JobItem, item.id)
        if image is None or generation is None or current_job is None or current_item is None:
            raise ProjectError("Translation job evidence disappeared")
        prepared = prepare_translation_enqueue(
            store, session, image=image, generation=generation, job=current_job, item=current_item
        )
        if prepared != frozen_prepared:
            raise PageLineageConflict(
                "Translation inputs changed during provider execution",
                resource=f"job-item:{item.id}",
                reason="g9-provider-cas-conflict",
            )
        eligible = eligible_translation_regions(session, image.id)
        if len(eligible) != len(snapshots):
            raise PageLineageConflict(
                "Translation eligibility changed during provider execution",
                resource=f"job-item:{item.id}",
                reason="g9-provider-cas-conflict",
            )
        for index, (region, snapshot) in enumerate(zip(eligible, snapshots, strict=True)):
            _neighbors, context_checksum = _context_for(eligible, index, prepared["policy"])
            if (
                region.id != snapshot["regionId"]
                or region.revision != snapshot["regionRevision"]
                or _text_checksum(region.source_text) != snapshot["sourceTextChecksum"]
                or context_checksum != snapshot["contextChecksum"]
            ):
                raise PageLineageConflict(
                    "Translation source/context changed during provider execution",
                    resource=f"region:{region.id}",
                    reason="g9-provider-cas-conflict",
                )
        if (
            session.scalar(
                select(RegionTranslationCandidate.id)
                .where(RegionTranslationCandidate.job_item_id == item.id)
                .limit(1)
            )
            is not None
        ):
            raise PageLineageConflict(
                "Translation publication raced another writer",
                resource=f"job-item:{item.id}",
                reason="g9-provider-cas-conflict",
            )
        project = store.project(session)
        produced: list[RegionTranslationCandidate] = []
        before_state = prepared["stateChecksum"]
        duplicate_region_ids = _generic_duplicate_region_ids(
            [
                (region, value if isinstance(value, str) else "")
                for region, value in zip(eligible, translated, strict=True)
            ]
        )
        for region, snapshot, value in zip(eligible, snapshots, translated, strict=True):
            value = value if isinstance(value, str) else ""
            flags = _computed_flags(region.source_text, value)
            if region.id in duplicate_region_ids:
                flags = sorted((set(flags) - {"none"}) | {"generic-duplicate"})
            prior_count = session.scalar(
                select(RegionTranslationCandidate.id)
                .where(
                    RegionTranslationCandidate.generation_id == generation.id,
                    RegionTranslationCandidate.region_id == region.id,
                )
                .order_by(RegionTranslationCandidate.revision_number.desc())
                .limit(1)
            )
            if prior_count is not None:
                raise PageLineageConflict(
                    "Initial job cannot overwrite region candidate history",
                    resource=f"region:{region.id}",
                    reason="g9-candidate-history-exists",
                )
            payload = _candidate_checksum_payload(
                generation_id=generation.id,
                region_id=region.id,
                revision_number=1,
                supersedes_candidate_id=None,
                origin_kind="model",
                g8_checksum=prepared["g8Checksum"],
                clean_plate_checksum=prepared["cleanPlateChecksum"],
                source_text_checksum=_text_checksum(region.source_text),
                source_region_revision=region.revision,
                context_checksum=snapshot["contextChecksum"],
                provider=prepared["provider"],
                model_version=prepared["modelVersion"],
                parameter_hash=prepared["parameterHash"],
                translation_text=value,
                computed_qc_flags=flags,
                target_language=prepared["policy"]["targetLanguage"],
                job_id=current_job.id,
                job_item_id=current_item.id,
            )
            candidate_id = new_id()
            revision = add_revision(
                session,
                project,
                entity_type="translation-candidate",
                entity_id=candidate_id,
                operation="create",
                before=None,
                after={
                    "candidateId": candidate_id,
                    "regionId": region.id,
                    "candidateChecksum": _digest(payload),
                },
            )
            session.flush()
            candidate = RegionTranslationCandidate(
                id=candidate_id,
                generation_id=generation.id,
                image_id=image.id,
                region_id=region.id,
                sequence=generation.next_sequence,
                revision_number=1,
                supersedes_candidate_id=None,
                origin_kind="model",
                g8_checksum=prepared["g8Checksum"],
                clean_plate_checksum=prepared["cleanPlateChecksum"],
                source_text_checksum=_text_checksum(region.source_text),
                context_checksum=snapshot["contextChecksum"],
                source_region_revision=region.revision,
                context_policy=prepared["policy"],
                provider=prepared["provider"],
                model_version=prepared["modelVersion"],
                parameter_hash=prepared["parameterHash"],
                target_language=prepared["policy"]["targetLanguage"],
                translation_text=value,
                candidate_checksum=_digest(payload),
                computed_qc_flags=flags,
                job_id=current_job.id,
                job_item_id=current_item.id,
                revision_id=revision.id,
            )
            session.add(candidate)
            session.flush()
            produced.append(candidate)
        after_state = translation_state_checksum(session, generation.id)
        _append_event(
            session,
            generation,
            operation="translation-candidates-produced",
            gate="G9_translation",
            state="pending",
            actor=_safe_actor(current_job.lineage_context["actor"]),
            input_checksum=before_state,
            output_checksum=after_state,
            parent_checksum=prepared["g8Checksum"],
            stage="translation",
            provider=prepared["provider"],
            model_version=prepared["modelVersion"],
            parameter_hash=prepared["parameterHash"],
            job_id=current_job.id,
            job_item_id=current_item.id,
            decision="candidates-produced",
            reason="review-required",
            evidence={
                "eventType": "translation-candidates-produced",
                "qualityState": "pending-review",
                "targetKind": "region-set",
                "eligibleRegionCount": len(eligible),
                "candidateCount": len(produced),
            },
        )
    store.write_snapshot()
    return {
        "provider": prepared["provider"],
        "modelVersion": prepared["modelVersion"],
        "parameterHash": prepared["parameterHash"],
        "count": len(produced),
    }


def translation_completion_evidence(
    session, *, job: Job, item: JobItem, succeeded: bool
) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(RegionTranslationCandidate).where(
                RegionTranslationCandidate.job_item_id == item.id
            )
        ).all()
    )
    produced = session.scalar(
        select(PageLineageEvent).where(
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == "translation-candidates-produced",
        )
    )
    if succeeded and (
        produced is None
        or not rows
        or produced.output_checksum != translation_state_checksum(session, produced.generation_id)
    ):
        raise PageLineageConflict(
            "Strict translation cannot complete without atomic candidate publication",
            resource=f"job-item:{item.id}",
            reason="g9-publication-missing",
        )
    if not succeeded and rows:
        raise PageLineageConflict(
            "Published translation candidates must recover to completion",
            resource=f"job-item:{item.id}",
            reason="g9-published-job-failed",
        )
    return {
        "outputChecksum": produced.output_checksum if succeeded and produced is not None else None,
        "evidence": {"candidateCount": len(rows)},
    }


def _revision_provenance(actor: dict[str, str | None], origin_kind: str) -> tuple[str, str]:
    actor_kind = actor["actorKind"]
    if origin_kind == "manual" and actor_kind == "human":
        return "manual", "manual-review-v1"
    if origin_kind == "agent" and actor_kind in {"codex", "cursor"}:
        return str(actor_kind), "agent-revision-v1"
    if origin_kind == "dictionary" and actor_kind in {"human", "codex", "cursor", "system"}:
        return "dictionary", "dictionary-revision-v1"
    raise PageLineageConflict(
        "Revision origin does not match the reviewer actor",
        resource="translation-candidate",
        reason="g9-origin-actor-mismatch",
    )


def record_translation_revision(
    store: ProjectStore,
    image_id: str,
    *,
    region_id: str,
    translation_text: str,
    origin_kind: str,
    observed_g8_checksum: str,
    observed_source_text_checksum: str,
    observed_context_checksum: str,
    observed_translation_state_checksum: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> dict[str, Any]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Translation image was not found")
        if image.revision != expected_revision:
            raise RevisionConflict(
                "Image revision changed",
                expected_revision=expected_revision,
                actual_revision=image.revision,
                resource=f"image:{image.id}",
            )
        generation = _active(session, image)
        mutation = require_image_mutation_lineage(store, session, image, lineage)
        assert mutation is not None
        _bound_generation, actor, expected_sequence = mutation
        _g6, g8_checksum, clean_checksum, _clean_candidate, eligible = _current_bindings(
            store, session, image, generation
        )
        _replay_state, replay_terminal = validate_translation_replay(
            session, generation, g8_checksum=g8_checksum
        )
        if replay_terminal is not None:
            raise PageLineageConflict(
                "Accepted G9 evidence is immutable",
                resource=f"image:{image.id}",
                reason="g9-translation-accepted",
            )
        for active_item, active_job in session.execute(
            select(JobItem, Job)
            .join(Job)
            .where(
                Job.kind == "translate",
                Job.lineage_context.is_not(None),
                JobItem.image_id == image.id,
                JobItem.status.in_(("queued", "running")),
            )
        ).all():
            try:
                _job_page_sequence(session, active_job, generation)
            except PageLineageConflict:
                continue
            raise PageLineageConflict(
                "Manual translation revision must wait for the active automatic job",
                resource=f"job-item:{active_item.id}",
                reason="g9-translation-job-active",
            )
        if observed_g8_checksum != g8_checksum:
            raise PageLineageConflict(
                "Observed G8 checksum is stale",
                resource=f"image:{image.id}",
                reason="g9-observation-stale",
            )
        if (
            translation_state_checksum(session, generation.id)
            != observed_translation_state_checksum
        ):
            raise PageLineageConflict(
                "Translation draft changed", resource=f"image:{image.id}", reason="g9-state-stale"
            )
        region = next((row for row in eligible if row.id == region_id), None)
        if region is None:
            raise PageLineageConflict(
                "Region is not translation eligible",
                resource=f"region:{region_id}",
                reason="g9-region-ineligible",
            )
        project = store.project(session)
        policy = _bounded_policy(dict(project.settings))
        _require_chinese_target(policy)
        index = eligible.index(region)
        _neighbors, context_checksum = _context_for(eligible, index, policy)
        source_checksum = _text_checksum(region.source_text)
        if (
            observed_source_text_checksum != source_checksum
            or observed_context_checksum != context_checksum
        ):
            raise PageLineageConflict(
                "Source or context observation is stale",
                resource=f"region:{region.id}",
                reason="g9-observation-stale",
            )
        candidates, reviews = _rows(session, generation.id)
        latest = next((row for row in reversed(candidates) if row.region_id == region.id), None)
        reviews_by_id = {row.candidate_id: row for row in reviews}
        if latest is not None:
            review = reviews_by_id.get(latest.id)
            if review is None:
                raise PageLineageConflict(
                    "Latest candidate must be reviewed before revision",
                    resource=f"region:{region.id}",
                    reason="g9-candidate-unreviewed",
                )
            if review.state == "accepted":
                raise PageLineageConflict(
                    "Accepted translation candidates are immutable",
                    resource=f"region:{region.id}",
                    reason="g9-candidate-accepted",
                )
        provider, model = _revision_provenance(actor, origin_kind)
        parameter_hash = _parameter_hash(
            generation, provider=provider, model_version=model, policy=policy
        )
        revision_number = 1 if latest is None else latest.revision_number + 1
        flags = _computed_flags(region.source_text, translation_text)
        payload = _candidate_checksum_payload(
            generation_id=generation.id,
            region_id=region.id,
            revision_number=revision_number,
            supersedes_candidate_id=latest.id if latest else None,
            origin_kind=origin_kind,
            g8_checksum=g8_checksum,
            clean_plate_checksum=clean_checksum,
            source_text_checksum=source_checksum,
            source_region_revision=region.revision,
            context_checksum=context_checksum,
            provider=provider,
            model_version=model,
            parameter_hash=parameter_hash,
            target_language=policy["targetLanguage"],
            translation_text=translation_text,
            computed_qc_flags=flags,
            job_id=None,
            job_item_id=None,
        )
        candidate_id = new_id()
        revision = add_revision(
            session,
            project,
            entity_type="translation-candidate",
            entity_id=candidate_id,
            operation="revise",
            before={"supersedesCandidateId": latest.id} if latest else None,
            after={
                "candidateId": candidate_id,
                "regionId": region.id,
                "candidateChecksum": _digest(payload),
            },
        )
        session.flush()
        candidate = RegionTranslationCandidate(
            id=candidate_id,
            generation_id=generation.id,
            image_id=image.id,
            region_id=region.id,
            sequence=generation.next_sequence,
            revision_number=revision_number,
            supersedes_candidate_id=latest.id if latest else None,
            origin_kind=origin_kind,
            g8_checksum=g8_checksum,
            clean_plate_checksum=clean_checksum,
            source_text_checksum=source_checksum,
            source_region_revision=region.revision,
            context_checksum=context_checksum,
            context_policy=policy,
            provider=provider,
            model_version=model,
            parameter_hash=parameter_hash,
            target_language=policy["targetLanguage"],
            translation_text=translation_text,
            candidate_checksum=_digest(payload),
            computed_qc_flags=flags,
            revision_id=revision.id,
        )
        session.add(candidate)
        session.flush()
        after_state = translation_state_checksum(session, generation.id)
        _append_event(
            session,
            generation,
            operation="translation-candidate-revised",
            gate="G9_translation",
            state="pending",
            actor=actor,
            input_checksum=observed_translation_state_checksum,
            output_checksum=after_state,
            parent_checksum=g8_checksum,
            stage="translation",
            provider=provider,
            model_version=model,
            parameter_hash=parameter_hash,
            revision_id=revision.id,
            decision="candidate-revised",
            reason="review-required",
            evidence={
                "eventType": "translation-candidate-revised",
                "qualityState": "pending-review",
                "targetKind": "region",
                "targetRegionId": region.id,
                "candidateId": candidate.id,
                "candidateChecksum": candidate.candidate_checksum,
                "revisionNumber": revision_number,
            },
            expected_sequence=expected_sequence,
        )
        image.revision += 1
    store.write_snapshot()
    return translation_gate_context(store, image_id)


def record_translation_candidate_review(
    store: ProjectStore,
    image_id: str,
    candidate_id: str,
    *,
    decision: str,
    reason: str,
    observed_candidate_checksum: str,
    observed_source_text_checksum: str,
    observed_context_checksum: str,
    observed_g8_checksum: str,
    checks: list[dict[str, Any]],
    qc_flags: list[str],
    expected_revision: int,
    lineage: dict[str, Any],
):
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Translation image was not found")
        if image.revision != expected_revision:
            raise RevisionConflict(
                "Image revision changed",
                expected_revision=expected_revision,
                actual_revision=image.revision,
                resource=f"image:{image.id}",
            )
        generation = _active(session, image)
        mutation = require_image_mutation_lineage(store, session, image, lineage)
        assert mutation is not None
        _bound_generation, actor, expected_sequence = mutation
        _g6, g8_checksum, _clean, _clean_candidate, eligible = _current_bindings(
            store, session, image, generation
        )
        _replay_state, replay_terminal = validate_translation_replay(
            session, generation, g8_checksum=g8_checksum
        )
        if replay_terminal is not None:
            raise PageLineageConflict(
                "Accepted G9 evidence is immutable",
                resource=f"image:{image.id}",
                reason="g9-translation-accepted",
            )
        candidate = session.get(RegionTranslationCandidate, candidate_id)
        if (
            candidate is None
            or candidate.generation_id != generation.id
            or candidate.image_id != image.id
        ):
            raise ProjectError("Translation candidate was not found")
        if candidate.job_item_id is not None:
            completed = session.scalar(
                select(PageLineageEvent).where(
                    PageLineageEvent.job_item_id == candidate.job_item_id,
                    PageLineageEvent.operation == "translate-job-completed",
                )
            )
            completed_item = session.get(JobItem, candidate.job_item_id)
            if completed is None or completed_item is None or completed_item.status != "completed":
                raise PageLineageConflict(
                    "Model candidates cannot be reviewed before job completion",
                    resource=f"translation-candidate:{candidate.id}",
                    reason="g9-job-incomplete",
                )
        if (
            session.scalar(
                select(RegionTranslationReview).where(
                    RegionTranslationReview.candidate_id == candidate.id
                )
            )
            is not None
        ):
            raise PageLineageConflict(
                "Translation candidate already reviewed",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-candidate-reviewed",
            )
        if (
            observed_candidate_checksum != candidate.candidate_checksum
            or observed_source_text_checksum != candidate.source_text_checksum
            or observed_context_checksum != candidate.context_checksum
            or observed_g8_checksum != g8_checksum
        ):
            raise PageLineageConflict(
                "Translation review observation is stale",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-observation-stale",
            )
        region = next((row for row in eligible if row.id == candidate.region_id), None)
        project = store.project(session)
        _require_chinese_target(candidate.context_policy)
        if (
            candidate.target_language not in {"zh-CN", "zh", "zh-Hans"}
            or region is None
            or region.revision != candidate.source_region_revision
            or _text_checksum(region.source_text) != candidate.source_text_checksum
            or _context_for(eligible, eligible.index(region), candidate.context_policy)[1]
            != candidate.context_checksum
        ):
            raise PageLineageConflict(
                "Candidate source/context is stale",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-context-stale",
            )
        if not _valid_checks(checks):
            raise PageLineageConflict(
                "Every translation QC check is required exactly once",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-checks-invalid",
            )
        if not _valid_qc_flags(qc_flags):
            raise PageLineageConflict(
                "Translation QC flags are invalid",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-flags-invalid",
            )
        state = "accepted" if decision == "accept" else "rejected"
        candidates, _reviews = _rows(session, generation.id)
        latest: dict[str, RegionTranslationCandidate] = {}
        for row in candidates:
            latest[row.region_id] = row
        latest_regions = {row.id: row for row in eligible}
        dynamic_duplicate_ids = _generic_duplicate_region_ids(
            [
                (latest_regions[region_id], row.translation_text)
                for region_id, row in latest.items()
                if region_id in latest_regions
            ]
        )
        if state == "accepted" and candidate.region_id in dynamic_duplicate_ids:
            raise PageLineageConflict(
                "Generic duplicate translation must be rejected and revised",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-generic-duplicate",
            )
        defects = _translation_review_defects(
            candidate.computed_qc_flags,
            checks,
            qc_flags,
            dynamic_duplicate=candidate.region_id in dynamic_duplicate_ids,
        )
        verdict_valid = _valid_translation_review_verdict(
            state=state,
            reason=reason,
            checks=checks,
            qc_flags=qc_flags,
            defects=defects,
        )
        if state == "accepted" and not verdict_valid:
            raise PageLineageConflict(
                "Translation acceptance cannot bypass hard QC",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-hard-qc-failed",
            )
        if state == "rejected" and not verdict_valid:
            raise PageLineageConflict(
                "Translation rejection requires an exact QC defect",
                resource=f"translation-candidate:{candidate.id}",
                reason="g9-rejection-invalid",
            )
        before_state = translation_state_checksum(session, generation.id)
        review_id = new_id()
        revision = add_revision(
            session,
            project,
            entity_type="translation-review",
            entity_id=review_id,
            operation="review",
            before=None,
            after={
                "candidateId": candidate.id,
                "state": state,
                "candidateChecksum": candidate.candidate_checksum,
                "compatibilityProjection": state == "accepted",
            },
        )
        session.flush()
        review = RegionTranslationReview(
            id=review_id,
            generation_id=generation.id,
            image_id=image.id,
            region_id=candidate.region_id,
            candidate_id=candidate.id,
            sequence=generation.next_sequence,
            state=state,
            reason=reason,
            candidate_checksum=candidate.candidate_checksum,
            source_text_checksum=candidate.source_text_checksum,
            context_checksum=candidate.context_checksum,
            g8_checksum=g8_checksum,
            checks=checks,
            qc_flags=qc_flags,
            reviewer=actor,
            revision_id=revision.id,
        )
        session.add(review)
        if state == "accepted":
            region.translation_text = candidate.translation_text
            region.translation_provider = candidate.provider
            region.revision += 1
        session.flush()
        after_state = translation_state_checksum(session, generation.id)
        event = _append_event(
            session,
            generation,
            operation="translation-candidate-reviewed",
            gate="G9_translation",
            state=state,
            actor=actor,
            input_checksum=before_state,
            output_checksum=after_state,
            parent_checksum=g8_checksum,
            stage="translation",
            provider=candidate.provider,
            model_version=candidate.model_version,
            parameter_hash=candidate.parameter_hash,
            revision_id=revision.id,
            decision=f"candidate-{state}",
            reason=reason,
            evidence={
                "eventType": "translation-candidate-reviewed",
                "qualityState": state,
                "targetKind": "region",
                "targetRegionId": candidate.region_id,
                "candidateId": candidate.id,
                "candidateChecksum": candidate.candidate_checksum,
                "reviewedRegionCount": 1,
            },
            expected_sequence=expected_sequence,
        )
        image.revision += 1
    store.write_snapshot()
    return image, event


def record_translation_gate_review(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    observed_translation_state_checksum: str,
    expected_revision: int,
    lineage: dict[str, Any],
):
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Translation image was not found")
        if image.revision != expected_revision:
            raise RevisionConflict(
                "Image revision changed",
                expected_revision=expected_revision,
                actual_revision=image.revision,
                resource=f"image:{image.id}",
            )
        generation = _active(session, image)
        mutation = require_image_mutation_lineage(store, session, image, lineage)
        assert mutation is not None
        _bound_generation, actor, expected_sequence = mutation
        _g6, g8_checksum, _clean, _clean_candidate, eligible = _current_bindings(
            store, session, image, generation
        )
        validate_translation_replay(session, generation, g8_checksum=g8_checksum)
        if (
            session.scalar(
                select(PageTranslationReview).where(
                    PageTranslationReview.generation_id == generation.id
                )
            )
            is not None
        ):
            raise PageLineageConflict(
                "G9 terminal review is immutable",
                resource=f"image:{image.id}",
                reason="g9-translation-accepted",
            )
        current = translation_state_checksum(session, generation.id)
        if observed_translation_state_checksum != current:
            raise PageLineageConflict(
                "Translation state observation is stale",
                resource=f"image:{image.id}",
                reason="g9-state-stale",
            )
        candidates, reviews = _rows(session, generation.id)
        review_by = {row.candidate_id: row for row in reviews}
        jobs: list[JobItem] = []
        for job_item, job in session.execute(
            select(JobItem, Job)
            .join(Job)
            .where(
                Job.kind == "translate",
                Job.lineage_context.is_not(None),
                JobItem.image_id == image.id,
            )
        ).all():
            try:
                _job_page_sequence(session, job, generation)
            except PageLineageConflict:
                continue
            jobs.append(job_item)
        if decision == "not-applicable":
            if eligible or candidates or reviews or jobs:
                raise PageLineageConflict(
                    "G9 N/A is valid only for zero eligible evidence",
                    resource=f"image:{image.id}",
                    reason="g9-na-invalid",
                )
            state = "not-applicable"
            reason = "no-translatable-regions"
            accepted = []
        else:
            if any(candidate.id not in review_by for candidate in candidates):
                raise PageLineageConflict(
                    "Every translation candidate must be reviewed",
                    resource=f"image:{image.id}",
                    reason="g9-unreviewed-candidates",
                )
            latest: dict[str, RegionTranslationCandidate] = {}
            for candidate in candidates:
                latest[candidate.region_id] = candidate
            if set(latest) != {row.id for row in eligible}:
                raise PageLineageConflict(
                    "Every eligible region requires a current candidate",
                    resource=f"image:{image.id}",
                    reason="g9-regions-incomplete",
                )
            accepted = []
            for region in eligible:
                candidate = latest[region.id]
                review = review_by[candidate.id]
                if review.state != "accepted":
                    raise PageLineageConflict(
                        "Every latest candidate must be accepted",
                        resource=f"region:{region.id}",
                        reason="g9-regions-incomplete",
                    )
                accepted.append(candidate.id)
            eligible_by_id = {region.id: region for region in eligible}
            if _generic_duplicate_region_ids(
                [
                    (eligible_by_id[region_id], candidate.translation_text)
                    for region_id, candidate in latest.items()
                ]
            ):
                raise PageLineageConflict(
                    "Generic duplicate translation requires revision",
                    resource=f"image:{image.id}",
                    reason="g9-generic-duplicate",
                )
            state = "accepted"
            reason = "all-translations-reviewed"
        project = store.project(session)
        terminal_id = new_id()
        revision = add_revision(
            session,
            project,
            entity_type="translation-page-review",
            entity_id=terminal_id,
            operation="review",
            before=None,
            after={
                "state": state,
                "translationStateChecksum": current,
                "acceptedCandidateIds": sorted(accepted),
            },
        )
        session.flush()
        terminal_checksum = _digest(
            {
                "terminalId": terminal_id,
                "generationId": generation.id,
                "g8Checksum": g8_checksum,
                "translationStateChecksum": current,
                "state": state,
                "reason": reason,
                "acceptedCandidateIds": sorted(accepted),
                "reviewer": actor,
                "revisionId": revision.id,
            }
        )
        terminal = PageTranslationReview(
            id=terminal_id,
            generation_id=generation.id,
            image_id=image.id,
            sequence=generation.next_sequence,
            state=state,
            reason=reason,
            g8_checksum=g8_checksum,
            translation_state_checksum=current,
            terminal_checksum=terminal_checksum,
            accepted_candidate_ids=sorted(accepted),
            reviewer=actor,
            revision_id=revision.id,
        )
        session.add(terminal)
        session.flush()
        event = _append_event(
            session,
            generation,
            operation="translation-stage-review",
            gate="G9_translation",
            state=state,
            actor=actor,
            input_checksum=current,
            output_checksum=terminal_checksum,
            parent_checksum=g8_checksum,
            stage="translation",
            parameter_hash=generation.parameter_set_hash,
            revision_id=revision.id,
            decision="translations-accepted"
            if state == "accepted"
            else "translation-not-applicable",
            reason=reason,
            evidence={
                "eventType": "translation-stage-review",
                "qualityState": state,
                "targetKind": "region-set",
                "eligibleRegionCount": len(eligible),
                "candidateCount": len(candidates),
                "reviewedRegionCount": len(reviews),
            },
            expected_sequence=expected_sequence,
        )
        image.revision += 1
    store.write_snapshot()
    return image, event


def require_current_translation_acceptance(
    store: ProjectStore, session, image: ImageAsset, generation: PageGeneration
) -> tuple[str, PageTranslationReview]:
    _g6, g8_checksum, _clean, _clean_candidate, eligible = _current_bindings(
        store, session, image, generation
    )
    current, terminal = validate_translation_replay(session, generation, g8_checksum=g8_checksum)
    if (
        terminal is None
        or terminal.g8_checksum != g8_checksum
        or terminal.translation_state_checksum != current
        or terminal.state != ("accepted" if eligible else "not-applicable")
    ):
        raise PageLineageConflict(
            "G9 translation is not currently accepted",
            resource=f"image:{image.id}",
            reason="g9-translation-not-currently-accepted",
        )
    return terminal.terminal_checksum, terminal
