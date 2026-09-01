from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.datastructures import UploadFile as StarletteUploadFile

from manga_localizer import __version__
from manga_localizer.config import Settings, get_settings
from manga_localizer.database import ImageAsset, Job, Project, Revision, TextRegion
from manga_localizer.imaging import (
    font_capabilities,
    preprocess_suggestion_from_status,
    typeset_overflow_from_status,
)
from manga_localizer.logging_utils import configure_logging, redact, without_secrets
from manga_localizer.model_bundle import apply_model_bundle
from manga_localizer.providers.registry import ProviderRegistry
from manga_localizer.queue import JobConflict, PersistentJobQueue
from manga_localizer.schemas import (
    BackgroundClassificationRequest,
    BackgroundGateContextOut,
    BackgroundGateRequest,
    CleanPlateFallbackRequest,
    CleanPlateGateContextOut,
    CleanPlateGateRequest,
    CloudFullPageReviewRequest,
    ConfigOut,
    FinalReviewBatchCreate,
    FinalReviewBatchExport,
    FinalReviewBatchOpen,
    FinalReviewItemPatch,
    FinalReviewItemRefresh,
    FinalReviewItemRepair,
    HealthOut,
    ImageOut,
    ImageReviewRequest,
    InpaintAICandidateReviewRequest,
    InpaintClassicalFallbackRequest,
    JobOut,
    JobRequest,
    LocalImportRequest,
    MaskDraftRequest,
    MaskGateContextOut,
    MaskGateRequest,
    OCRGateContextOut,
    OCRGateRequest,
    OCRSourceReviewRequest,
    OpenAISessionConfig,
    PageGateResultOut,
    PageGenerationCreate,
    PageGenerationOut,
    PageLineageEventOut,
    ProjectCreate,
    ProjectOpen,
    ProjectOut,
    ProjectPatch,
    ReadingOrderRequest,
    ReconstructionGateRequest,
    RegionCreate,
    RegionDeleteRequest,
    RegionOut,
    RegionPatch,
    RegionsGateRequest,
    SelectInpaintCandidateRequest,
    StageReviewRequest,
    TextPresenceGateRequest,
    TranslationCandidateReviewRequest,
    TranslationCandidateRevisionRequest,
    TranslationGateContextOut,
    TranslationGateRequest,
    TypesetCandidateReviewRequest,
    TypesetGateContextOut,
)
from manga_localizer.security import (
    UnsafePathError,
    UnsafeRemoteEndpointError,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.clean_plates import (
    clean_plate_artifact_path,
    clean_plate_gate_context,
    record_clean_plate_fallback,
    record_clean_plate_review,
)
from manga_localizer.services.cloud_full_page_clean_plates import (
    MAX_METADATA_CHARS,
    MAX_NORMALIZED_BYTES,
    MAX_RAW_BYTES,
    cloud_full_page_artifact_path,
    cloud_full_page_context,
    cloud_full_page_raw_artifact_path,
    ingest_cloud_full_page_candidate,
    record_cloud_full_page_review,
)
from manga_localizer.services.final_reviews import (
    FINAL_REVIEW_NO_STORE_HEADERS,
    FinalReviewBatchConflict,
    FinalReviewConflict,
    FinalReviewRegistry,
)
from manga_localizer.services.images import (
    StagePrerequisiteConflict,
    StageReviewObservationConflict,
    image_path,
    import_local,
    ingest_bytes,
    invalidate_image_pipeline,
    list_images,
    public_inpaint_ai_rejected_candidate_ids,
    public_inpaint_fallback,
    review_image,
    review_image_stage,
    set_inpaint_ai_candidate_review,
    set_inpaint_classical_fallback,
    stage_reviews,
    thumbnail_path,
    validate_image_bytes,
)
from manga_localizer.services.inpaint_candidates import (
    candidate_image_path,
    public_candidates_from_status,
    select_inpaint_candidate,
    trusted_public_candidate_evidence,
)
from manga_localizer.services.masks import (
    mask_artifact_path,
    mask_gate_context,
    record_mask_review,
    update_mask_draft,
)
from manga_localizer.services.page_lineage import (
    PageLineageConflict,
    background_gate_context,
    create_page_generation,
    find_page_generation,
    list_page_generations,
    list_page_lineage_events,
    ocr_gate_context,
    public_page_generation,
    public_page_lineage_event,
    record_background_gate_acceptance,
    record_ocr_gate_acceptance,
    record_reconstruction_decision,
    record_regions_gate_acceptance,
    record_text_presence_decision,
    require_no_active_generations_for_project_settings,
    require_no_page_generations_for_project_ingest,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectNotFound,
    ProjectRegistry,
    ProjectStore,
    RevisionConflict,
    add_revision,
    public_root,
    region_payload,
    settings_with_defaults,
)
from manga_localizer.services.regions import (
    RegionNotFound,
    apply_reading_order,
    create_region,
    delete_region,
    list_regions,
    set_background_classification,
    set_ocr_source_review,
    update_region,
)
from manga_localizer.services.translations import (
    record_translation_candidate_review,
    record_translation_gate_review,
    record_translation_revision,
    translation_gate_context,
)
from manga_localizer.services.trust import (
    invalidate_trust,
    is_region_trusted,
    recognition_payload,
    recognition_uses_input_variant,
    region_disposition,
)
from manga_localizer.services.typesets import (
    record_typeset_candidate_review,
    typeset_artifact_path,
    typeset_gate_context,
)
from manga_localizer.workbench_static import (
    companion_url_for,
    cors_origins_for,
    resolve_frontend_dist,
)

_GENERATED_IMAGE_CACHE_HEADERS = {"Cache-Control": "private, no-store"}


def _generated_image_response(path: Path, media_type: str = "image/png") -> FileResponse:
    return FileResponse(path, media_type=media_type, headers=_GENERATED_IMAGE_CACHE_HEADERS)


async def _read_upload_with_limit(upload: StarletteUploadFile, maximum: int) -> bytes:
    if upload.size is not None and upload.size > maximum:
        raise HTTPException(status_code=413, detail="cloud image upload exceeds the byte limit")
    payload = await upload.read(maximum + 1)
    if len(payload) > maximum:
        raise HTTPException(status_code=413, detail="cloud image upload exceeds the byte limit")
    return payload


def _project_dict(project: Project, root: Path) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "rootPath": public_root(root),
        "outputRoot": public_root(root),
        "inputRoot": project.input_root,
        "schemaVersion": project.schema_version,
        "settings": redact(without_secrets(project.settings)),
        "revision": project.revision,
        "createdAt": project.created_at,
        "updatedAt": project.updated_at,
    }


def _settings_invalidation(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    stages: set[str] = set()
    if changed & {"preprocessorProvider", "preprocessing"}:
        stages.update(
            (
                "preprocess",
                "detection",
                "ocr",
                "translation",
                "inpaint",
                "typeset",
                "export",
            )
        )
    if changed & {
        "translatorProvider",
        "targetLanguage",
        "targetScript",
        "glossary",
        "characterNames",
        "fixedTranslations",
        "contextPages",
        "translateSoundEffects",
        "translateBackgroundText",
        "remoteEndpoint",
        "remoteModel",
    }:
        stages.update(("translation", "typeset", "export"))
    if changed & {"inpainterProvider"}:
        stages.update(("inpaint", "typeset", "export"))
    if changed & {"typesetterProvider", "defaultFont", "defaultTypography"}:
        stages.update(("typeset", "export"))
    return stages


_PUBLIC_STAGE_ERROR_MESSAGES = {
    "preprocess": "Image preprocessing failed; inspect the private project log",
    "detect": "Text detection failed; inspect the private project log",
    "ocr": "OCR failed; inspect the private project log",
    "translate": "Translation failed; inspect the private project log",
    "render": "Image rendering failed; inspect the private project log",
    "inpaint": "Image rendering failed; inspect the private project log",
    "typeset": "Image rendering failed; inspect the private project log",
    "export": "Export failed; inspect the private project log",
}


def _public_stage_error(stage: object) -> str:
    return _PUBLIC_STAGE_ERROR_MESSAGES.get(
        str(stage),
        "Processing failed; inspect the private project log",
    )


def _public_processing_errors(errors: object) -> list[dict[str, str]]:
    if not isinstance(errors, list):
        return []
    projected: list[dict[str, str]] = []
    for recorded in errors:
        if not isinstance(recorded, dict):
            continue
        stage = str(recorded.get("stage", "processing"))
        if stage not in _PUBLIC_STAGE_ERROR_MESSAGES:
            stage = "processing"
        projected.append({"stage": stage, "error": _public_stage_error(stage)})
    return projected


def _image_dict(image: ImageAsset, store: ProjectStore | None = None) -> dict[str, Any]:
    pipeline_status = {
        key: image.status.get(key, "pending")
        for key in (
            "preprocess",
            "detection",
            "ocr",
            "translation",
            "inpaint",
            "typeset",
            "export",
        )
    }
    pipeline_status["reviewState"] = image.status.get("reviewState", "pending")
    pipeline_status["reviewedAt"] = image.status.get("reviewedAt") or ""
    regions = image.__dict__.get("regions", [])
    processing_errors = _public_processing_errors(image.processing_errors)
    selected_inpaint_candidate, inpaint_candidate_records = public_candidates_from_status(
        image.status
    )
    candidate_generation_id: str | None = None
    if store is not None:
        candidate_generation_id, trusted_selected, trusted_records = (
            trusted_public_candidate_evidence(store, image)
        )
        if candidate_generation_id is not None:
            selected_inpaint_candidate = trusted_selected
            inpaint_candidate_records = trusted_records
        else:
            selected_inpaint_candidate = None
            inpaint_candidate_records = []
    overflow_count, overflow_ids = typeset_overflow_from_status(image.status)
    return {
        "id": image.id,
        "projectId": image.project_id,
        "name": image.name,
        "relativePath": image.relative_path,
        "sourceKind": image.source_kind,
        "width": image.width,
        "height": image.height,
        "mediaType": image.media_type,
        "status": pipeline_status,
        "stageReviews": stage_reviews(image),
        "regionCount": len(regions),
        "confirmedCount": sum(region.confirmed for region in regions),
        "trustedCount": sum(is_region_trusted(region) for region in regions),
        "trustReviewCount": sum(
            not region.ignored and region_disposition(region) == "review" for region in regions
        ),
        "ignoredCount": sum(region.ignored for region in regions),
        "processingErrors": processing_errors,
        "error": processing_errors[-1]["error"] if processing_errors else None,
        "revision": image.revision,
        "preprocessingProvider": image.status.get("preprocessingProvider") or None,
        "detectorProvider": image.status.get("detectorProvider") or None,
        "ocrProvider": image.status.get("ocrProvider") or None,
        "translatorProvider": image.status.get("translatorProvider") or None,
        "inpaintingProvider": image.status.get("inpaintingProvider") or None,
        "typesettingProvider": image.status.get("typesettingProvider") or None,
        "renderInputVariant": image.status.get("renderInputVariant") or None,
        "renderScale": image.status.get("renderScale") or None,
        "renderedSize": image.status.get("renderedSize") or None,
        "inpaintCandidate": selected_inpaint_candidate,
        "inpaintCandidates": inpaint_candidate_records,
        "inpaintCandidateGenerationId": candidate_generation_id,
        "inpaintAiRejectedCandidateIds": public_inpaint_ai_rejected_candidate_ids(store, image)
        if store is not None
        else [],
        "inpaintFallback": public_inpaint_fallback(store, image)
        if store is not None
        else {"state": "pending", "rejectedAiCandidateIds": []},
        "typesetOverflowCount": overflow_count,
        "typesetOverflowRegionIds": overflow_ids,
        "preprocessSuggestion": preprocess_suggestion_from_status(
            image.status,
            width=image.width,
            height=image.height,
        ),
        "thumbnailUrl": f"/api/images/{image.id}/thumbnail",
        "contentUrl": f"/api/images/{image.id}/content",
        "createdAt": image.created_at,
        "updatedAt": image.updated_at,
    }


def _region_dict(region: TextRegion) -> dict[str, Any]:
    return region_payload(region) | {
        "createdAt": region.created_at,
        "updatedAt": region.updated_at,
    }


_PUBLIC_JOB_OUTPUT_FIELDS = {
    "preprocess": {"provider", "profile", "originalSize", "processedSize", "scale"},
    "detect": {
        "provider",
        "inputVariant",
        "policyVersion",
        "count",
        "confidenceBuckets",
        "dispositionCounts",
        "reasonCounts",
    },
    "ocr": {
        "provider",
        "inputVariant",
        "policyVersion",
        "count",
        "attemptCount",
        "selectedInputVariantCounts",
        "confidenceBuckets",
        "dispositionCounts",
        "reasonCounts",
    },
    "translate": {
        "provider",
        "policyVersion",
        "count",
        "skippedUntrustedCount",
        "dispositionCounts",
        "reasonCounts",
    },
    "render": {
        "provider",
        "inpaintingProvider",
        "inpaintingProviders",
        "typesettingProvider",
        "repairPolicy",
        "inputVariant",
        "renderedSize",
        "scale",
        "eligibleRegionCount",
        "skippedRegionCount",
        "repairedRegionCount",
        "inpaintCandidate",
        "inpaintCandidateCount",
        "typesetEligibleRegionCount",
        "typesetSkippedRegionCount",
        "overflowCount",
        "overflowRegionIds",
        "partialTypeset",
        "overlayRegionCount",
        "overlayRegionIds",
    },
}


def _public_job_output(kind: str, output: dict[str, Any]) -> dict[str, Any]:
    if kind == "export":
        conflicts: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                conflict = value.get("conflict")
                if isinstance(conflict, str):
                    conflicts.append(conflict)
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(output)
        return {
            "writtenArtifactCount": sum(conflict != "skipped" for conflict in conflicts),
            "skippedArtifactCount": sum(conflict == "skipped" for conflict in conflicts),
        }
    normalized_kind = "render" if kind in {"render", "inpaint", "typeset"} else kind
    allowed = _PUBLIC_JOB_OUTPUT_FIELDS.get(normalized_kind, set())
    return {key: output[key] for key in allowed if key in output}


def _job_dict(job: Job) -> dict[str, Any]:
    public_error = _public_stage_error(job.kind)
    return {
        "id": job.id,
        "projectId": job.project_id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "total": job.total,
        "completed": job.completed,
        "error": public_error if job.error else None,
        "items": [
            {
                "id": item.id,
                "imageId": item.image_id,
                "position": item.position,
                "status": item.status,
                "progress": item.progress,
                "error": public_error if item.error else None,
                "output": _public_job_output(
                    job.kind,
                    redact(without_secrets(item.output)),
                ),
            }
            for item in job.items
        ],
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
    }


def _decode_relative_paths(form: Any, files: list[UploadFile]) -> list[str]:
    raw_values = list(form.getlist("relativePaths")) or list(form.getlist("relative_paths"))
    metadata = form.get("metadata")
    paths: list[str] = []
    if len(raw_values) == 1:
        try:
            decoded = json.loads(str(raw_values[0]))
            if isinstance(decoded, list):
                paths = [str(value) for value in decoded]
            elif isinstance(decoded, dict):
                paths = [str(value) for value in decoded.get("relativePaths", [])]
        except json.JSONDecodeError:
            paths = [str(raw_values[0])]
    elif raw_values:
        paths = [str(value) for value in raw_values]
    if not paths and metadata:
        try:
            decoded = json.loads(str(metadata))
            if isinstance(decoded, dict):
                paths = [str(value) for value in decoded.get("relativePaths", [])]
            elif isinstance(decoded, list):
                paths = [str(item.get("relativePath", "")) for item in decoded]
        except (json.JSONDecodeError, AttributeError):
            raise ProjectError("Upload metadata must be valid JSON") from None
    if not paths:
        paths = [file.filename or f"upload-{index}.png" for index, file in enumerate(files)]
    if len(paths) != len(files):
        raise ProjectError("relativePaths must contain exactly one path per uploaded file")
    # Validate the entire batch before writing the first project-owned source file.
    resolved_paths = [safe_relative_path(path) for path in paths]
    strip_common_root = str(
        form.get("stripCommonRoot") or form.get("strip_common_root") or ""
    ).lower() in {"1", "true", "yes"}
    if strip_common_root:
        if not resolved_paths or any(len(path.parts) < 2 for path in resolved_paths):
            raise ProjectError("Folder uploads must include a common selected root directory")
        roots = {path.parts[0] for path in resolved_paths}
        if len(roots) != 1:
            raise ProjectError("Folder uploads must share one selected root directory")
        resolved_paths = [Path(*path.parts[1:]) for path in resolved_paths]
    return [path.as_posix() for path in resolved_paths]


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    resolved_settings, bundled_models = apply_model_bundle(settings or get_settings())
    registry = ProjectRegistry(resolved_settings)
    final_reviews = FinalReviewRegistry(resolved_settings, registry)
    providers = ProviderRegistry(resolved_settings)
    queue = PersistentJobQueue(registry, providers, resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(resolved_settings.log_level)
        registry.load_catalog()
        final_reviews.load_catalog()
        if start_worker:
            await queue.start()
        app.state.ready = True
        try:
            yield
        finally:
            if start_worker:
                await queue.stop()

    app = FastAPI(
        title="Manga Localizer API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.registry = registry
    app.state.final_reviews = final_reviews
    app.state.providers = providers
    app.state.queue = queue
    app.state.bundled_models = bundled_models
    app.state.ready = False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins_for(resolved_settings),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "If-Match"],
    )

    @app.exception_handler(ProjectNotFound)
    @app.exception_handler(RegionNotFound)
    async def not_found_handler(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(RevisionConflict)
    @app.exception_handler(FinalReviewBatchConflict)
    @app.exception_handler(FinalReviewConflict)
    @app.exception_handler(StagePrerequisiteConflict)
    @app.exception_handler(StageReviewObservationConflict)
    @app.exception_handler(JobConflict)
    @app.exception_handler(PageLineageConflict)
    async def conflict_handler(_request: Request, error: Exception) -> JSONResponse:
        if isinstance(error, FinalReviewBatchConflict):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "message": str(error),
                        "expectedRevision": error.expected_revision,
                        "actualRevision": error.actual_revision,
                        "resource": f"final-review-batch:{error.batch_id}",
                    }
                },
            )
        if isinstance(error, FinalReviewConflict):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "message": str(error),
                        "expectedRevision": error.expected_revision,
                        "actualRevision": error.actual_revision,
                        "resource": f"final-review-item:{error.item['id']}",
                        "currentItem": error.item,
                    }
                },
            )
        if isinstance(error, StagePrerequisiteConflict):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "message": str(error),
                        "resource": error.resource,
                        "stage": error.stage,
                        "requiredState": error.required_state,
                        "reason": error.reason,
                        "mismatches": error.mismatches,
                    }
                },
            )
        if isinstance(error, StageReviewObservationConflict):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "message": str(error),
                        "resource": error.resource,
                        "stage": error.stage,
                        "mismatches": error.mismatches,
                    }
                },
            )
        if isinstance(error, RevisionConflict):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "message": str(error),
                        "expectedRevision": error.expected_revision,
                        "actualRevision": error.actual_revision,
                        "resource": error.resource,
                    }
                },
            )
        if isinstance(error, PageLineageConflict):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "message": str(error),
                        "resource": error.resource,
                        "reason": error.reason,
                        "expectedSequence": error.expected_sequence,
                        "actualSequence": error.actual_sequence,
                    }
                },
            )
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(UnsafePathError)
    @app.exception_handler(UnsafeRemoteEndpointError)
    @app.exception_handler(ProjectError)
    async def bad_request_handler(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(error)})

    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthOut)
    async def health() -> dict[str, Any]:
        companion = companion_url_for(resolved_settings)
        return {
            "status": "ok" if app.state.ready else "degraded",
            "version": __version__,
            "database": "ok",
            "queue": "running"
            if queue.running
            else ("disabled" if not start_worker else "stopped"),
            "lan_access": resolved_settings.lan_access,
            "companion_url": companion,
            "bundled_models": getattr(app.state, "bundled_models", None),
        }

    @app.get("/health", include_in_schema=False)
    async def root_health() -> dict[str, Any]:
        return await health()

    @router.get("/config", response_model=ConfigOut)
    async def config() -> dict[str, Any]:
        provider_capabilities = providers.capabilities()
        fonts = font_capabilities()
        return {
            "providers": provider_capabilities,
            "capabilities": {
                "portableProjects": True,
                "browserFolderUpload": True,
                "trustedLocalImport": True,
                "persistentQueue": True,
                "safeExport": True,
                "finalReviewBatches": True,
                "fonts": fonts,
                "ocr": provider_capabilities["ocr"],
                "translation": provider_capabilities["translation"],
                "lanAccess": resolved_settings.lan_access,
                "companionUrl": companion_url_for(resolved_settings) or "",
            },
        }

    @router.put("/config/translation/openai-session", response_model=ConfigOut)
    async def configure_openai(body: OpenAISessionConfig) -> dict[str, Any]:
        providers.configure_openai_session(
            api_key=body.api_key,
            base_url=body.base_url,
            model=body.model,
        )
        return await config()

    @router.get("/projects", response_model=list[ProjectOut])
    async def projects_list() -> list[dict[str, Any]]:
        output = []
        for project in registry.list():
            output.append(_project_dict(project, registry.get(project.id).root))
        return output

    @router.post("/projects", response_model=ProjectOut, status_code=201)
    async def project_create(body: ProjectCreate) -> dict[str, Any]:
        store, project = registry.create(body.name, body.output_path, body.settings)
        return _project_dict(project, store.root)

    @router.get("/final-review-batches")
    async def final_review_batches_list() -> list[dict[str, Any]]:
        return final_reviews.list()

    @router.post("/final-review-batches", status_code=201)
    async def final_review_batch_create(body: FinalReviewBatchCreate) -> dict[str, Any]:
        return final_reviews.create(
            name=body.name,
            output_path=body.output_path,
            source_project_ids=body.source_project_ids,
            expected_item_count=body.expected_item_count,
        )

    @router.post("/final-review-batches/open")
    async def final_review_batch_open(body: FinalReviewBatchOpen) -> dict[str, Any]:
        return final_reviews.open(body.manifest_path)

    @router.get("/final-review-batches/{batch_id}")
    async def final_review_batch_get(batch_id: str) -> dict[str, Any]:
        return final_reviews.get(batch_id).batch(include_items=True)

    @router.get("/final-review-batches/{batch_id}/items")
    async def final_review_batch_items(batch_id: str) -> list[dict[str, Any]]:
        return final_reviews.get(batch_id).items()

    @router.post("/final-review-batches/{batch_id}/export")
    async def final_review_batch_export(
        batch_id: str, body: FinalReviewBatchExport
    ) -> dict[str, Any]:
        return final_reviews.export(
            batch_id,
            body.output_path,
            conflict=body.conflict,
            preserve_tree=body.preserve_tree,
            expected_batch_revision=body.expected_batch_revision,
            actor=body.actor,
        )

    @router.patch("/final-review-items/{item_id}")
    async def final_review_item_patch(item_id: str, body: FinalReviewItemPatch) -> dict[str, Any]:
        store = final_reviews.find_item(item_id)
        return store.update_item(
            item_id,
            verdict=body.verdict,
            issue_codes=body.issue_codes,
            feedback=body.feedback,
            expected_revision=body.expected_revision,
            expected_batch_revision=body.expected_batch_revision,
            actor=body.actor,
        )

    @router.post("/final-review-items/{item_id}/refresh")
    async def final_review_item_refresh(
        item_id: str, body: FinalReviewItemRefresh
    ) -> dict[str, Any]:
        return final_reviews.find_item(item_id).refresh(
            item_id,
            expected_revision=body.expected_revision,
            expected_batch_revision=body.expected_batch_revision,
            actor=body.actor,
        )

    @router.post("/final-review-items/{item_id}/repair", status_code=201)
    async def final_review_item_repair(item_id: str, body: FinalReviewItemRepair) -> dict[str, Any]:
        return final_reviews.find_item(item_id).repair(
            item_id,
            expected_revision=body.expected_revision,
            expected_batch_revision=body.expected_batch_revision,
            actor=body.actor,
            parameter_set_id=body.parameter_set_id,
            parameter_set_hash=body.parameter_set_hash,
            retry_from_generation_id=(
                str(body.retry_from_generation_id)
                if body.retry_from_generation_id is not None
                else None
            ),
        )

    @router.get("/final-review-items/{item_id}/revisions")
    async def final_review_item_revisions(item_id: str) -> list[dict[str, Any]]:
        return final_reviews.find_item(item_id).revisions(item_id)

    @router.get("/final-review-items/{item_id}/content")
    async def final_review_item_content(
        item_id: str,
        artifact_revision: Annotated[int | None, Query(alias="artifactRevision", ge=1)] = None,
    ) -> FileResponse:
        path = final_reviews.find_item(item_id).artifact_path(
            item_id, artifact_revision=artifact_revision
        )
        return FileResponse(path, media_type="image/png", headers=FINAL_REVIEW_NO_STORE_HEADERS)

    @router.get("/final-review-items/{item_id}/thumbnail")
    async def final_review_item_thumbnail(
        item_id: str,
        artifact_revision: Annotated[int | None, Query(alias="artifactRevision", ge=1)] = None,
    ) -> FileResponse:
        path = final_reviews.find_item(item_id).artifact_path(
            item_id, thumbnail=True, artifact_revision=artifact_revision
        )
        return FileResponse(path, media_type="image/jpeg", headers=FINAL_REVIEW_NO_STORE_HEADERS)

    @router.get("/final-review-items/{item_id}/artifacts/{kind}")
    async def final_review_item_artifact(
        item_id: str,
        kind: str,
        artifact_revision: Annotated[int, Query(alias="artifactRevision", ge=1)],
    ) -> FileResponse:
        path = final_reviews.find_item(item_id).artifact_path(
            item_id, kind=kind, artifact_revision=artifact_revision
        )
        return FileResponse(path, headers=FINAL_REVIEW_NO_STORE_HEADERS)

    @router.post("/projects/open", response_model=ProjectOut)
    async def project_open(body: ProjectOpen) -> dict[str, Any]:
        store, project = registry.open(body.manifest_path)
        store.recover_jobs()
        return _project_dict(project, store.root)

    @router.get("/projects/{project_id}", response_model=ProjectOut)
    async def project_get(project_id: str) -> dict[str, Any]:
        store = registry.get(project_id)
        return _project_dict(store.project(), store.root)

    @router.patch("/projects/{project_id}", response_model=ProjectOut)
    async def project_patch(project_id: str, body: ProjectPatch) -> dict[str, Any]:
        store = registry.get(project_id)
        with store.session() as session:
            project = store.project(session)
            if body.expected_revision is not None and project.revision != body.expected_revision:
                raise RevisionConflict(
                    f"Project revision is {project.revision}, expected {body.expected_revision}",
                    expected_revision=body.expected_revision,
                    actual_revision=project.revision,
                    resource=f"project:{project.id}",
                )
            before = {"name": project.name, "settings": project.settings}
            if body.name is not None:
                project.name = body.name.strip()
            if body.settings is not None:
                previous_settings = dict(project.settings)
                proposed_settings = settings_with_defaults(body.settings, base=project.settings)
                changed_settings = {
                    key
                    for key in previous_settings.keys() | proposed_settings.keys()
                    if previous_settings.get(key) != proposed_settings.get(key)
                }
                if changed_settings:
                    require_no_active_generations_for_project_settings(session, project.id)
                project.settings = proposed_settings
                invalidated_stages = _settings_invalidation(
                    previous_settings,
                    project.settings,
                )
                if invalidated_stages:
                    images = list(
                        session.scalars(
                            select(ImageAsset).where(ImageAsset.project_id == project.id)
                        ).all()
                    )
                    for image in images:
                        if changed_settings & {
                            "preprocessorProvider",
                            "preprocessing",
                        }:
                            for region in image.regions:
                                if region.ignored:
                                    continue
                                evidence = recognition_payload(region)
                                if not recognition_uses_input_variant(evidence, "preprocessed"):
                                    continue
                                if not (is_region_trusted(region) or region.confirmed):
                                    continue
                                region.recognition = invalidate_trust(evidence)
                                region.confirmed = False
                                region.revision += 1
                        invalidate_image_pipeline(store, image, invalidated_stages)
                        image.revision += 1
            add_revision(
                session,
                project,
                entity_type="project",
                entity_id=project.id,
                operation="update",
                before=before,
                after={"name": project.name, "settings": project.settings},
            )
        store.write_snapshot()
        return _project_dict(project, store.root)

    @router.post(
        "/projects/{project_id}/images/upload",
        response_model=list[ImageOut],
        status_code=201,
    )
    async def images_upload(project_id: str, request: Request) -> list[dict[str, Any]]:
        store = registry.get(project_id)
        form = await request.form()
        files = [item for item in form.getlist("files") if isinstance(item, StarletteUploadFile)]
        if not files:
            raise ProjectError("Multipart upload requires at least one files field")
        paths = _decode_relative_paths(form, files)
        buffered: list[bytes] = []
        for file in files:
            data = await file.read(resolved_settings.max_upload_bytes + 1)
            if len(data) > resolved_settings.max_upload_bytes:
                raise ProjectError("An uploaded file exceeds the configured size limit")
            buffered.append(data)
        for data in buffered:
            validate_image_bytes(data, resolved_settings)
        with store.lock:
            with store.session() as session:
                project = store.project(session)
                require_no_page_generations_for_project_ingest(session, project.id)
            imported = [
                ingest_bytes(store, resolved_settings, data=data, relative_path=path)
                for data, path in zip(buffered, paths, strict=True)
            ]
        return [_image_dict(image, store) for image in imported]

    @router.post(
        "/projects/{project_id}/images/import-local",
        response_model=list[ImageOut],
        status_code=201,
    )
    async def images_import_local(
        project_id: str, body: LocalImportRequest, response: Response
    ) -> list[dict[str, Any]]:
        store = registry.get(project_id)
        with store.lock:
            with store.session() as session:
                project = store.project(session)
                require_no_page_generations_for_project_ingest(session, project.id)
            imported, failures = import_local(store, resolved_settings, body.paths)
        if failures:
            response.headers["X-Manga-Localizer-Import-Failures"] = str(len(failures))
        if failures and not imported:
            raise ProjectError(f"No images imported; first failure: {failures[0]['error']}")
        return [_image_dict(image, store) for image in imported]

    @router.get("/projects/{project_id}/images", response_model=list[ImageOut])
    async def images_list(project_id: str) -> list[dict[str, Any]]:
        store = registry.get(project_id)
        return [_image_dict(image, store) for image in list_images(store)]

    @router.post(
        "/images/{image_id}/page-generations",
        response_model=PageGenerationOut,
        status_code=201,
    )
    async def page_generation_create(image_id: str, body: PageGenerationCreate) -> dict[str, Any]:
        store, _image = registry.find_image(image_id)
        generation = create_page_generation(
            registry,
            store,
            image_id,
            run_id=body.run_id,
            page_generation_id=str(body.page_generation_id),
            parameter_set_id=body.parameter_set_id,
            parameter_set_hash=body.parameter_set_hash,
            restart_from_source=body.restart_from_source,
            source_project_id=str(body.source_project_id),
            source_image_id=str(body.source_image_id),
            expected_source_checksum=body.expected_source_checksum,
            expected_revision=body.expected_revision,
            actor=body.actor.model_dump(mode="json", by_alias=True),
        )
        return public_page_generation(generation)

    @router.get(
        "/images/{image_id}/page-generations",
        response_model=list[PageGenerationOut],
    )
    async def page_generations_list(image_id: str) -> list[dict[str, Any]]:
        store, _image = registry.find_image(image_id)
        return [
            public_page_generation(generation)
            for generation in list_page_generations(store, image_id)
        ]

    @router.get(
        "/page-generations/{generation_id}/events",
        response_model=list[PageLineageEventOut],
    )
    async def page_lineage_events_list(generation_id: str) -> list[dict[str, Any]]:
        store, _generation = find_page_generation(registry, generation_id)
        return [
            public_page_lineage_event(event)
            for event in list_page_lineage_events(store, generation_id)
        ]

    @router.patch(
        "/images/{image_id}/page-gates/reconstruction",
        response_model=PageGateResultOut,
    )
    async def page_reconstruction_gate(
        image_id: str,
        body: ReconstructionGateRequest,
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_reconstruction_decision(
            store,
            image.id,
            decision=body.decision,
            reason=body.reason,
            observed_quality_checksum=body.observed_quality_checksum,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.patch(
        "/images/{image_id}/page-gates/text-presence",
        response_model=PageGateResultOut,
    )
    async def page_text_presence_gate(
        image_id: str,
        body: TextPresenceGateRequest,
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_text_presence_decision(
            store,
            image.id,
            decision=body.decision,
            reason=body.reason,
            evidence=body.evidence,
            observed_original_checksum=body.observed_original_checksum,
            observed_quality_checksum=body.observed_quality_checksum,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.patch(
        "/images/{image_id}/page-gates/regions",
        response_model=PageGateResultOut,
    )
    async def page_regions_gate(
        image_id: str,
        body: RegionsGateRequest,
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_regions_gate_acceptance(
            store,
            image.id,
            decision=body.decision,
            reason=body.reason,
            observed_region_checksum=body.observed_region_checksum,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.get(
        "/images/{image_id}/page-gates/background",
        response_model=BackgroundGateContextOut,
    )
    async def page_background_gate_context(image_id: str) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return background_gate_context(store, image.id)

    @router.patch(
        "/images/{image_id}/page-gates/background",
        response_model=PageGateResultOut,
    )
    async def page_background_gate(
        image_id: str,
        body: BackgroundGateRequest,
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_background_gate_acceptance(
            store,
            image.id,
            decision=body.decision,
            reason=body.reason,
            observed_background_checksum=body.observed_background_checksum,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.get(
        "/images/{image_id}/page-gates/ocr",
        response_model=OCRGateContextOut,
    )
    async def page_ocr_gate_context(image_id: str) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return ocr_gate_context(store, image.id)

    @router.patch(
        "/images/{image_id}/page-gates/ocr",
        response_model=PageGateResultOut,
    )
    async def page_ocr_gate(image_id: str, body: OCRGateRequest) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_ocr_gate_acceptance(
            store,
            image.id,
            decision=body.decision,
            reason=body.reason,
            observed_ocr_checksum=body.observed_ocr_checksum,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.get(
        "/images/{image_id}/page-gates/mask",
        response_model=MaskGateContextOut,
    )
    async def page_mask_gate_context(image_id: str) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return mask_gate_context(store, image.id)

    @router.patch(
        "/images/{image_id}/page-gates/mask/draft",
        response_model=MaskGateContextOut,
    )
    async def page_mask_draft(image_id: str, body: MaskDraftRequest) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return update_mask_draft(
            store,
            image.id,
            regions=[region.model_dump(mode="json", by_alias=True) for region in body.regions],
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )

    @router.patch(
        "/images/{image_id}/page-gates/mask",
        response_model=PageGateResultOut,
    )
    async def page_mask_gate(image_id: str, body: MaskGateRequest) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_mask_review(
            store,
            image.id,
            decision=body.decision,
            reason=body.reason,
            selected_artifact_id=body.selected_artifact_id,
            observed_mask_checksum=body.observed_mask_checksum,
            coverage_checks=[
                entry.model_dump(mode="json", by_alias=True) for entry in body.coverage_checks
            ],
            collateral_checks=[
                entry.model_dump(mode="json", by_alias=True) for entry in body.collateral_checks
            ],
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.get("/images/{image_id}/page-gates/mask/artifacts/{artifact_id}")
    async def page_mask_artifact(image_id: str, artifact_id: str) -> FileResponse:
        store, image = registry.find_image(image_id)
        return _generated_image_response(mask_artifact_path(store, image.id, artifact_id))

    @router.get(
        "/images/{image_id}/page-gates/clean-plate",
        response_model=CleanPlateGateContextOut,
    )
    async def page_clean_plate_gate_context(image_id: str) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return clean_plate_gate_context(store, image.id)

    @router.patch(
        "/images/{image_id}/page-gates/clean-plate/fallback",
        response_model=CleanPlateGateContextOut,
    )
    async def page_clean_plate_fallback(
        image_id: str, body: CleanPlateFallbackRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return record_clean_plate_fallback(
            store,
            image.id,
            enabled=body.enabled,
            reason=body.reason,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )

    @router.patch(
        "/images/{image_id}/page-gates/clean-plate",
        response_model=PageGateResultOut,
    )
    async def page_clean_plate_gate(image_id: str, body: CleanPlateGateRequest) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_clean_plate_review(
            store,
            image.id,
            decision=body.decision,
            reason=body.reason,
            candidate_id=body.candidate_id,
            observed_candidate_checksum=body.observed_candidate_checksum,
            observed_width=body.observed_width,
            observed_height=body.observed_height,
            checks=[entry.model_dump(mode="json", by_alias=True) for entry in body.checks],
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.get("/images/{image_id}/page-gates/clean-plate/candidates/{candidate_id}")
    async def page_clean_plate_candidate(image_id: str, candidate_id: str) -> FileResponse:
        store, image = registry.find_image(image_id)
        return _generated_image_response(clean_plate_artifact_path(store, image.id, candidate_id))

    @router.get("/images/{image_id}/page-gates/cloud-full-page")
    async def page_cloud_full_page_context(image_id: str) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return cloud_full_page_context(store, image.id)

    @router.post("/images/{image_id}/page-gates/cloud-full-page/candidates")
    async def page_cloud_full_page_candidate(image_id: str, request: Request) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        form = await request.form()
        parts = list(form.multi_items())
        if len(parts) != 3 or {name for name, _value in parts} != {
            "raw",
            "normalized",
            "metadata",
        }:
            raise HTTPException(
                status_code=422,
                detail="multipart fields must be exactly raw, normalized, and metadata",
            )
        raw = form.get("raw")
        normalized = form.get("normalized")
        metadata_value = form.get("metadata")
        if (
            not isinstance(raw, StarletteUploadFile)
            or not isinstance(normalized, StarletteUploadFile)
            or not isinstance(metadata_value, str)
        ):
            raise HTTPException(
                status_code=422,
                detail="raw, normalized, and JSON metadata multipart fields are required",
            )
        if len(metadata_value) > MAX_METADATA_CHARS:
            raise HTTPException(status_code=413, detail="cloud metadata exceeds the size limit")
        try:
            metadata = json.loads(metadata_value)
        except (TypeError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=422, detail="metadata must be valid JSON") from error
        if not isinstance(metadata, dict):
            raise HTTPException(status_code=422, detail="metadata must be a JSON object")
        return ingest_cloud_full_page_candidate(
            store,
            image.id,
            raw_bytes=await _read_upload_with_limit(raw, MAX_RAW_BYTES),
            normalized_bytes=await _read_upload_with_limit(normalized, MAX_NORMALIZED_BYTES),
            metadata=metadata,
        )

    @router.patch("/images/{image_id}/page-gates/cloud-full-page")
    async def page_cloud_full_page_review(
        image_id: str, body: CloudFullPageReviewRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return record_cloud_full_page_review(
            store,
            image.id,
            candidate_id=body.candidate_id,
            observed_checksum=body.observed_checksum,
            checks=[entry.model_dump(mode="json", by_alias=True) for entry in body.checks],
            decision=body.decision,
            reason=body.reason,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )

    @router.get("/images/{image_id}/page-gates/cloud-full-page/candidates/{candidate_id}")
    async def page_cloud_full_page_artifact(image_id: str, candidate_id: str) -> FileResponse:
        store, image = registry.find_image(image_id)
        return _generated_image_response(
            cloud_full_page_artifact_path(store, image.id, candidate_id)
        )

    @router.get("/images/{image_id}/page-gates/cloud-full-page/candidates/{candidate_id}/raw")
    async def page_cloud_full_page_raw_artifact(image_id: str, candidate_id: str) -> FileResponse:
        store, image = registry.find_image(image_id)
        path, media_type = cloud_full_page_raw_artifact_path(store, image.id, candidate_id)
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.get(
        "/images/{image_id}/page-gates/translation",
        response_model=TranslationGateContextOut,
    )
    async def page_translation_gate_context(image_id: str) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return translation_gate_context(store, image.id)

    @router.post(
        "/images/{image_id}/page-gates/translation/candidates",
        response_model=TranslationGateContextOut,
    )
    async def page_translation_candidate_revision(
        image_id: str, body: TranslationCandidateRevisionRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return record_translation_revision(
            store,
            image.id,
            region_id=body.region_id,
            translation_text=body.translation_text,
            origin_kind=body.origin_kind,
            observed_g8_checksum=body.observed_g8_checksum,
            observed_source_text_checksum=body.observed_source_text_checksum,
            observed_context_checksum=body.observed_context_checksum,
            observed_translation_state_checksum=body.observed_translation_state_checksum,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )

    @router.patch(
        "/images/{image_id}/page-gates/translation/candidates/{candidate_id}",
        response_model=PageGateResultOut,
    )
    async def page_translation_candidate_review(
        image_id: str, candidate_id: str, body: TranslationCandidateReviewRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_translation_candidate_review(
            store,
            image.id,
            candidate_id,
            decision=body.decision,
            reason=body.reason,
            observed_candidate_checksum=body.observed_candidate_checksum,
            observed_source_text_checksum=body.observed_source_text_checksum,
            observed_context_checksum=body.observed_context_checksum,
            observed_g8_checksum=body.observed_g8_checksum,
            checks=[entry.model_dump(mode="json", by_alias=True) for entry in body.checks],
            qc_flags=list(body.qc_flags),
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.patch(
        "/images/{image_id}/page-gates/translation",
        response_model=PageGateResultOut,
    )
    async def page_translation_gate(image_id: str, body: TranslationGateRequest) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_translation_gate_review(
            store,
            image.id,
            decision=body.decision,
            observed_translation_state_checksum=body.observed_translation_state_checksum,
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.get(
        "/images/{image_id}/page-gates/typeset",
        response_model=TypesetGateContextOut,
    )
    async def page_typeset_gate_context(image_id: str) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        return typeset_gate_context(store, image.id)

    @router.get("/images/{image_id}/page-gates/typeset/candidates/{candidate_id}")
    async def page_typeset_candidate(image_id: str, candidate_id: str) -> FileResponse:
        store, image = registry.find_image(image_id)
        return _generated_image_response(typeset_artifact_path(store, image.id, candidate_id))

    @router.patch(
        "/images/{image_id}/page-gates/typeset/candidates/{candidate_id}",
        response_model=PageGateResultOut,
    )
    async def page_typeset_candidate_review(
        image_id: str,
        candidate_id: str,
        body: TypesetCandidateReviewRequest,
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated, event = record_typeset_candidate_review(
            store,
            image.id,
            candidate_id,
            decision=body.decision,
            reason=body.reason,
            observed_candidate_checksum=body.observed_candidate_checksum,
            observed_route_checksum=body.observed_route_checksum,
            observed_style_checksum=body.observed_style_checksum,
            observed_layout_checksum=body.observed_layout_checksum,
            observed_translation_terminal_checksum=(body.observed_translation_terminal_checksum),
            observed_clean_plate_checksum=body.observed_clean_plate_checksum,
            observed_width=body.observed_width,
            observed_height=body.observed_height,
            observed_render_scale=body.observed_render_scale,
            checks=[entry.model_dump(mode="json", by_alias=True) for entry in body.checks],
            expected_revision=body.expected_revision,
            lineage=body.lineage.model_dump(mode="json", by_alias=True),
        )
        return {
            "imageId": updated.id,
            "imageRevision": updated.revision,
            "generationId": event.generation_id,
            "nextSequence": event.sequence + 1,
            "event": public_page_lineage_event(event),
        }

    @router.patch("/images/{image_id}/review", response_model=ImageOut)
    async def image_review(image_id: str, body: ImageReviewRequest) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        reviewed = review_image(
            store,
            image.id,
            review_state=body.review_state,
            expected_revision=body.expected_revision,
        )
        return _image_dict(reviewed, store)

    @router.patch("/images/{image_id}/stage-reviews/{stage}", response_model=ImageOut)
    async def image_stage_review(
        image_id: str, stage: str, body: StageReviewRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        reviewed = review_image_stage(
            store,
            image.id,
            stage=stage,
            state=body.state,
            expected_revision=body.expected_revision,
            observed_artifact_checksum=body.observed_artifact_checksum,
            observed_mask_checksum=body.observed_mask_checksum,
            lineage=(
                body.lineage.model_dump(mode="json", by_alias=True)
                if body.lineage is not None
                else None
            ),
        )
        return _image_dict(reviewed, store)

    @router.patch("/images/{image_id}/inpaint-candidate", response_model=ImageOut)
    async def image_inpaint_candidate(
        image_id: str, body: SelectInpaintCandidateRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        selected = select_inpaint_candidate(
            store,
            image.id,
            candidate_id=body.candidate_id,
            expected_revision=body.expected_revision,
        )
        return _image_dict(selected, store)

    @router.patch("/images/{image_id}/inpaint-classical-fallback", response_model=ImageOut)
    async def image_inpaint_classical_fallback(
        image_id: str, body: InpaintClassicalFallbackRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated = set_inpaint_classical_fallback(
            store,
            image.id,
            state=body.state,
            reason=body.reason,
            expected_revision=body.expected_revision,
        )
        return _image_dict(updated, store)

    @router.patch("/images/{image_id}/inpaint-ai-candidate-review", response_model=ImageOut)
    async def image_inpaint_ai_candidate_review(
        image_id: str, body: InpaintAICandidateReviewRequest
    ) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        updated = set_inpaint_ai_candidate_review(
            store,
            image.id,
            state=body.state,
            expected_revision=body.expected_revision,
        )
        return _image_dict(updated, store)

    @router.get("/images/{image_id}/content")
    async def image_content(
        image_id: str,
        variant: Annotated[str, Query()] = "original",
    ) -> FileResponse:
        store, image = registry.find_image(image_id)
        if variant in {"preprocessed", "erased", "inpainted", "typeset"}:
            stage = (
                "preprocessed"
                if variant == "preprocessed"
                else "inpainted"
                if variant in {"erased", "inpainted"}
                else "typeset"
            )
            status_stage = (
                "preprocess"
                if stage == "preprocessed"
                else "inpaint"
                if stage == "inpainted"
                else "typeset"
            )
            if image.status.get(status_stage) != "done":
                raise HTTPException(
                    status_code=404,
                    detail=f"Generated {variant} image is stale or not available",
                )
            relative = safe_relative_path(image.relative_path).with_suffix(".png")
            target = resolve_write_target(
                store.root,
                Path("generated") / stage / relative,
                protected_roots=(store.source_root,),
            )
            if not target.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=f"Generated {variant} image is not available",
                )
            return _generated_image_response(target)
        if variant != "original":
            raise HTTPException(status_code=400, detail="Unknown image content variant")
        return FileResponse(
            image_path(store, image), media_type=image.media_type, filename=image.name
        )

    @router.get("/images/{image_id}/thumbnail")
    async def image_thumbnail(image_id: str) -> FileResponse:
        store, image = registry.find_image(image_id)
        return FileResponse(
            thumbnail_path(store, image, resolved_settings.thumbnail_size),
            media_type="image/jpeg",
        )

    @router.get("/images/{image_id}/generated/inpaint-candidates/{candidate_id}")
    async def image_inpaint_candidate_file(image_id: str, candidate_id: str) -> FileResponse:
        store, image = registry.find_image(image_id)
        if image.status.get("inpaint") != "done":
            raise HTTPException(
                status_code=404,
                detail="Generated inpainting candidate is stale or not available",
            )
        _selected, records = public_candidates_from_status(image.status)
        if candidate_id not in {item["id"] for item in records}:
            raise HTTPException(status_code=404, detail="Unknown inpainting candidate")
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        try:
            target = candidate_image_path(store, relative, candidate_id)
        except ProjectError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if not target.is_file():
            raise HTTPException(
                status_code=404,
                detail="Generated inpainting candidate is not available",
            )
        return _generated_image_response(target)

    @router.get("/images/{image_id}/generated/{stage}")
    async def image_generated(image_id: str, stage: str) -> FileResponse:
        stage_directory = {
            "preprocessed": "preprocessed",
            "inpainted": "inpainted",
            "typeset": "typeset",
            "mask": "masks",
        }
        if stage not in stage_directory:
            raise HTTPException(status_code=404, detail="Unknown generated image stage")
        store, image = registry.find_image(image_id)
        status_stage = (
            "preprocess"
            if stage == "preprocessed"
            else "inpaint"
            if stage in {"inpainted", "mask"}
            else "typeset"
        )
        if image.status.get(status_stage) != "done":
            raise HTTPException(
                status_code=404,
                detail="Generated image is stale or not available",
            )
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        target = resolve_write_target(
            store.root,
            Path("generated") / stage_directory[stage] / relative,
            protected_roots=(store.source_root,),
        )
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Generated image is not available")
        return _generated_image_response(target)

    @router.get("/images/{image_id}/regions", response_model=list[RegionOut])
    async def regions_list(image_id: str) -> list[dict[str, Any]]:
        store, image = registry.find_image(image_id)
        return [_region_dict(region) for region in list_regions(store, image.id)]

    @router.post("/images/{image_id}/regions", response_model=RegionOut, status_code=201)
    async def region_create(image_id: str, body: RegionCreate) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        values = body.model_dump(mode="json")
        if body.lineage is not None:
            values["lineage"] = body.lineage.model_dump(mode="json", by_alias=True)
        region = create_region(store, image.id, values)
        return _region_dict(region)

    @router.patch("/regions/{region_id}", response_model=RegionOut)
    async def region_patch(region_id: str, body: RegionPatch) -> dict[str, Any]:
        store, region = registry.find_region(region_id)
        values = body.model_dump(mode="json", exclude_unset=True)
        if body.lineage is not None:
            values["lineage"] = body.lineage.model_dump(mode="json", by_alias=True)
        updated = update_region(
            store,
            region.id,
            values,
        )
        return _region_dict(updated)

    @router.patch(
        "/regions/{region_id}/background-classification",
        response_model=RegionOut,
    )
    async def region_background_classification(
        region_id: str,
        body: BackgroundClassificationRequest,
    ) -> dict[str, Any]:
        store, region = registry.find_region(region_id)
        values = body.model_dump(mode="json")
        values["lineage"] = body.lineage.model_dump(mode="json", by_alias=True)
        updated = set_background_classification(store, region.id, values)
        return _region_dict(updated)

    @router.patch(
        "/regions/{region_id}/ocr-source-review",
        response_model=RegionOut,
    )
    async def region_ocr_source_review(
        region_id: str,
        body: OCRSourceReviewRequest,
    ) -> dict[str, Any]:
        store, region = registry.find_region(region_id)
        values = body.model_dump(mode="json")
        values["lineage"] = body.lineage.model_dump(mode="json", by_alias=True)
        updated = set_ocr_source_review(store, region.id, values)
        return _region_dict(updated)

    @router.delete("/regions/{region_id}", status_code=204)
    async def region_delete(
        region_id: str,
        body: Annotated[RegionDeleteRequest | None, Body()] = None,
        expected_revision: Annotated[int | None, Query(alias="expectedRevision", ge=0)] = None,
    ) -> Response:
        store, region = registry.find_region(region_id)
        delete_region(
            store,
            region.id,
            body.expected_revision if body is not None else expected_revision,
            expected_image_revision=(body.expected_image_revision if body is not None else None),
            lineage=(
                body.lineage.model_dump(mode="json", by_alias=True)
                if body is not None and body.lineage is not None
                else None
            ),
        )
        return Response(status_code=204)

    @router.post("/images/{image_id}/reading-order", response_model=list[RegionOut])
    @router.post("/images/{image_id}/regions/reorder", response_model=list[RegionOut])
    async def reading_order(image_id: str, body: ReadingOrderRequest) -> list[dict[str, Any]]:
        store, image = registry.find_image(image_id)
        ordered = apply_reading_order(
            store,
            image.id,
            region_ids=body.region_ids,
            mode=body.mode,
            expected_image_revision=body.expected_image_revision,
            lineage=(
                body.lineage.model_dump(mode="json", by_alias=True)
                if body.lineage is not None
                else None
            ),
        )
        return [_region_dict(region) for region in ordered]

    @router.get("/projects/{project_id}/revisions")
    async def revisions_list(
        project_id: str, limit: int = Query(default=100, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        store = registry.get(project_id)
        with store.session() as session:
            revisions = list(
                session.scalars(
                    select(Revision).order_by(Revision.created_at.desc()).limit(limit)
                ).all()
            )
        return [
            {
                "id": revision.id,
                "entityType": revision.entity_type,
                "entityId": revision.entity_id,
                "operation": revision.operation,
                "before": redact(without_secrets(revision.before)),
                "after": redact(without_secrets(revision.after)),
                "projectRevision": revision.project_revision,
                "createdAt": revision.created_at,
            }
            for revision in revisions
        ]

    @router.post("/projects/{project_id}/{kind}", response_model=JobOut, status_code=202)
    async def job_create(project_id: str, kind: str, body: JobRequest) -> dict[str, Any]:
        if kind not in {
            "preprocess",
            "detect",
            "ocr",
            "mask",
            "translate",
            "render",
            "export",
            "inpaint",
            "typeset",
        }:
            raise HTTPException(status_code=404, detail="Unknown project operation")
        store = registry.get(project_id)
        job = queue.create_job(
            store,
            kind=kind,
            image_ids=body.image_ids,
            region_ids=body.region_ids,
            options=body.options,
            lineage=(
                body.lineage.model_dump(mode="json", by_alias=True)
                if body.lineage is not None
                else None
            ),
        )
        return _job_dict(job)

    @router.get("/jobs", response_model=list[JobOut])
    async def jobs_list(
        project_id: Annotated[str | None, Query(alias="projectId")] = None,
    ) -> list[dict[str, Any]]:
        if project_id:
            jobs = queue.list_jobs(registry.get(project_id))
        else:
            jobs = [job for store in registry.stores() for job in queue.list_jobs(store)]
            jobs.sort(key=lambda job: job.created_at, reverse=True)
        return [_job_dict(job) for job in jobs]

    @router.get("/jobs/{job_id}", response_model=JobOut)
    async def job_get(job_id: str) -> dict[str, Any]:
        store, _job = registry.find_job(job_id)
        return _job_dict(queue.get_job(store, job_id))

    @router.post("/jobs/{job_id}/{action}", response_model=JobOut)
    async def job_action(job_id: str, action: str) -> dict[str, Any]:
        store, _job = registry.find_job(job_id)
        actions = {
            "pause": queue.pause,
            "resume": queue.resume,
            "cancel": queue.cancel,
            "retry": queue.retry,
        }
        operation = actions.get(action)
        if operation is None:
            raise HTTPException(status_code=404, detail="Unknown job action")
        return _job_dict(operation(store, job_id))

    app.include_router(router)
    workbench = resolve_frontend_dist(resolved_settings)
    if workbench is not None:
        app.mount("/", StaticFiles(directory=workbench, html=True), name="workbench")
    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "manga_localizer.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
    )


if __name__ == "__main__":
    run()
