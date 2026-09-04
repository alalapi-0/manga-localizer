from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from manga_localizer.services import cloud_full_page_clean_plates as cloud_service

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-image"
GEMINI_API_PROFILE = "google-gemini-interactions-v1beta"
PROMPT = (
    "The first image is the current manga quality plate. The second image is its binary edit "
    "mask: white pixels are the only edit region and black pixels must be preserved. Remove all "
    "source-language text inside the white mask and reconstruct the original line art, screentone, "
    "texture, lighting, and background. Add no text, objects, holes, blur bands, or repeated "
    "texture. Return exactly one edited image at the input composition and aspect ratio."
)
_CLAIM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ +()-]*$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPAQUE_ID_RE = re.compile(r"^[^/\\\x00\r\n]{1,128}$")


class CloudImageCLIError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalInputs:
    context: dict[str, Any]
    quality: bytes
    mask: bytes


@dataclass(frozen=True)
class RouteIdentity:
    runtime: str
    provider: str
    tool: str
    model_version: str
    api_profile: str
    actor_id: str
    invocation_prefix: str


_NATIVE_ROUTES = {
    "codex": {
        "provider": "codex-native-route",
        "tool": "image_gen",
        "modelVersion": "native-image-model-unreported",
        "apiProfile": "codex-native-subscription-v1",
        "actorId": "codex-agent",
        "invocationPrefix": "codex-native",
    },
    "cursor": {
        "provider": "cursor-native-route",
        "tool": "GenerateImage",
        "modelVersion": "auto-native-image-model-unreported",
        "apiProfile": "cursor-native-subscription-v1",
        "actorId": "cursor-agent",
        "invocationPrefix": "cursor-native",
    },
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _loopback_base(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CloudImageCLIError("The Manga Localizer API must be an HTTP loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CloudImageCLIError("The Manga Localizer API address contains unsupported fields")
    return value.rstrip("/")


def _claim(value: str, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or _CLAIM_RE.fullmatch(value) is None
    ):
        raise CloudImageCLIError(f"The {field} label is invalid")
    return value


def _gemini_model(value: str) -> str:
    if _MODEL_RE.fullmatch(value) is None or "image" not in value.lower():
        raise CloudImageCLIError("The selected Gemini model is not an image model identifier")
    return value


def _opaque_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
        raise CloudImageCLIError(f"The {field} identity is invalid")
    return value


def _native_route(runtime: str, model_label: str | None) -> RouteIdentity:
    values = _NATIVE_ROUTES.get(runtime)
    if values is None:
        raise CloudImageCLIError("Native execution requires --runtime codex or cursor")
    model_version = values["modelVersion"] if model_label is None else model_label
    return RouteIdentity(
        runtime=runtime,
        provider=values["provider"],
        tool=values["tool"],
        model_version=_claim(model_version, field="native model", maximum=128),
        api_profile=values["apiProfile"],
        actor_id=values["actorId"],
        invocation_prefix=values["invocationPrefix"],
    )


def _gemini_route(runtime: str, model: str) -> RouteIdentity:
    if runtime not in _NATIVE_ROUTES:
        raise CloudImageCLIError("Direct API execution requires --runtime codex or cursor")
    return RouteIdentity(
        runtime=runtime,
        provider="google-gemini-api",
        tool="interactions-v1beta-image-edit",
        model_version=_gemini_model(model),
        api_profile=GEMINI_API_PROFILE,
        actor_id=f"{runtime}-agent",
        invocation_prefix="gemini",
    )


def _checked_response(response: httpx.Response, operation: str) -> httpx.Response:
    if response.status_code < 200 or response.status_code >= 300:
        raise CloudImageCLIError(f"{operation} failed with HTTP {response.status_code}")
    return response


def _json_object(response: httpx.Response, operation: str) -> dict[str, Any]:
    _checked_response(response, operation)
    try:
        payload = response.json()
    except ValueError as error:
        raise CloudImageCLIError(f"{operation} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise CloudImageCLIError(f"{operation} returned an invalid object")
    return payload


def _fetch_local_inputs(client: httpx.Client, *, api_base: str, image_id: str) -> LocalInputs:
    context = _json_object(
        client.get(f"{api_base}/api/images/{image_id}/page-gates/cloud-full-page"),
        "Cloud context lookup",
    )
    quality = _checked_response(
        client.get(f"{api_base}/api/images/{image_id}/generated/quality"),
        "Quality plate lookup",
    ).content
    mask_id = context.get("maskArtifactId")
    if not isinstance(mask_id, str) or not mask_id:
        raise CloudImageCLIError("Cloud context has no accepted G7 mask")
    mask = _checked_response(
        client.get(f"{api_base}/api/images/{image_id}/page-gates/mask/artifacts/{mask_id}"),
        "Accepted mask lookup",
    ).content
    if _sha256(quality) != context.get("qualityChecksum"):
        raise CloudImageCLIError("Quality plate checksum changed before the image operation")
    if _sha256(mask) != context.get("maskChecksum"):
        raise CloudImageCLIError("Accepted mask checksum changed before the image operation")
    return LocalInputs(context=context, quality=quality, mask=mask)


def _provider_request(model: str, quality: bytes, mask: bytes) -> dict[str, Any]:
    return {
        "model": model,
        "input": [
            {"type": "text", "text": PROMPT},
            {
                "type": "image",
                "mime_type": "image/png",
                "data": base64.b64encode(quality).decode("ascii"),
            },
            {
                "type": "image",
                "mime_type": "image/png",
                "data": base64.b64encode(mask).decode("ascii"),
            },
        ],
        "response_format": {"type": "image", "mime_type": "image/png"},
    }


def _output_image(payload: dict[str, Any]) -> tuple[bytes, str]:
    candidates: list[dict[str, Any]] = []
    for key in ("output_image", "outputImage"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            content = step.get("content")
            if isinstance(content, list):
                candidates.extend(item for item in content if isinstance(item, dict))
    for item in reversed(candidates):
        if item.get("type") not in {None, "image"}:
            continue
        encoded = item.get("data")
        media_type = item.get("mime_type") or item.get("mimeType") or "image/png"
        if not isinstance(encoded, str) or not isinstance(media_type, str):
            continue
        try:
            return base64.b64decode(encoded, validate=True), media_type
        except ValueError:
            continue
    raise CloudImageCLIError("Gemini returned no decodable image")


def _provider_parameters(route: RouteIdentity) -> dict[str, Any]:
    return {
        "apiProfile": route.api_profile,
        "responseMimeType": "image/png",
        "inputRoles": ["quality-plate", "accepted-g7-mask"],
        "outputCount": 1,
    }


def _read_native_raw(value: str | None) -> bytes:
    if not value:
        raise CloudImageCLIError("Native execution requires --raw-image")
    path = Path(value)
    if not path.is_absolute():
        raise CloudImageCLIError("Native raw image path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CloudImageCLIError("Native raw image is unavailable or is a symlink") from error
    payload = b""
    try:
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode):
            raise CloudImageCLIError("Native raw image must be a regular file")
        if facts.st_size <= 0 or facts.st_size > cloud_service.MAX_RAW_BYTES:
            raise CloudImageCLIError("Native raw image violates the byte limit")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(cloud_service.MAX_RAW_BYTES + 1)
    except OSError as error:
        raise CloudImageCLIError("Native raw image could not be read") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload or len(payload) > cloud_service.MAX_RAW_BYTES:
        raise CloudImageCLIError("Native raw image violates the byte limit")
    return payload


def _prepare_path(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise CloudImageCLIError("Native preparation directory must be absolute")
    try:
        occupied = path.exists() or path.is_symlink()
    except OSError as error:
        raise CloudImageCLIError("Native preparation directory could not be inspected") from error
    if occupied:
        raise CloudImageCLIError("Native preparation directory must not already exist")
    return path


def _write_new(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _prepare_native_inputs(
    path: Path,
    *,
    local: LocalInputs,
    route: RouteIdentity,
    image_id: str,
    session_id: str,
    normalization_profile: str = cloud_service.NORMALIZATION_PROFILE,
) -> dict[str, str]:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
        path.chmod(0o700)
        quality_path = path / "quality.png"
        mask_path = path / "mask.png"
        request_path = path / "request.json"
        manifest = {
            "schemaVersion": 1,
            "executionMode": "native",
            "runtime": route.runtime,
            "tool": route.tool,
            "modelVersion": route.model_version,
            "underlyingProvider": "unreported",
            "claimStatus": cloud_service.CLAIM_STATUS,
            "imageId": image_id,
            "generationId": local.context["generationId"],
            "sessionId": session_id,
            "qualityPath": str(quality_path),
            "qualitySha256": _sha256(local.quality),
            "maskPath": str(mask_path),
            "maskSha256": _sha256(local.mask),
            "prompt": PROMPT,
            "promptSha256": _sha256(PROMPT.encode()),
            "outputRequirement": "exactly-one-local-png",
        }
        if normalization_profile != cloud_service.NORMALIZATION_PROFILE:
            manifest["normalizationProfile"] = normalization_profile
        _write_new(quality_path, local.quality)
        _write_new(mask_path, local.mask)
        _write_new(
            request_path,
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        )
        return {
            "qualityPath": str(quality_path),
            "maskPath": str(mask_path),
            "requestPath": str(request_path),
        }
    except OSError as error:
        raise CloudImageCLIError(
            "Native preparation failed; the preparation directory may be incomplete"
        ) from error


def _metadata(
    *,
    local: LocalInputs,
    raw: bytes,
    raw_media_type: str,
    route: RouteIdentity,
    quota_class: str,
    invocation_evidence: str,
    task_id: str,
    thread_id: str,
    session_id: str,
    normalization_profile: str = cloud_service.NORMALIZATION_PROFILE,
) -> tuple[dict[str, Any], bytes]:
    context = local.context
    target = context.get("targetGrid")
    if not isinstance(target, dict) or not all(
        type(target.get(key)) is int and target[key] > 0 for key in ("width", "height")
    ):
        raise CloudImageCLIError("Cloud context target grid is invalid")
    provider_normalized, normalization, _raw_grid, detected_media_type = (
        cloud_service._normalize_for_profile(
            raw,
            (target["width"], target["height"]),
            quality=local.quality,
            mask=local.mask,
            profile=normalization_profile,
        )
    )
    if raw_media_type != detected_media_type:
        raise CloudImageCLIError("Raw media type does not match the image bytes")
    normalized, composite = cloud_service._strict_mask_composite(
        local.quality, provider_normalized, local.mask
    )
    delta = cloud_service._delta_manifest(local.quality, normalized, local.mask)
    if delta["outsideMaskChangedPixelCount"] != 0:
        raise CloudImageCLIError("Strict composite changed pixels outside the accepted mask")
    parameters = _provider_parameters(route)
    strict_route = cloud_service._strict_route_manifest(
        normalization,
        composite,
        delta,
        context["orderedInputs"],
        quota_class=quota_class,
        provider_parameters=parameters,
    )
    evidence_hash = hashlib.sha256(invocation_evidence.encode()).hexdigest()
    invocation_id = f"{route.invocation_prefix}-{evidence_hash[:32]}"
    actor = {
        "actorKind": route.runtime,
        "actorId": route.actor_id,
        "taskId": task_id,
        "threadId": thread_id,
        "sessionId": session_id,
        "operationSource": "script",
    }
    metadata = {
        "routeProfile": cloud_service.CLOUD_FULL_PAGE_PROFILE,
        "invocationId": invocation_id,
        "provider": route.provider,
        "tool": route.tool,
        "modelVersion": route.model_version,
        "quotaClass": quota_class,
        "providerParameters": parameters,
        "claimStatus": cloud_service.CLAIM_STATUS,
        "promptSha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "rawSha256": _sha256(raw),
        "rawMediaType": detected_media_type,
        "normalizedSha256": _sha256(normalized),
        "normalizationManifest": normalization,
        "normalizationDigest": cloud_service._digest(normalization),
        "deltaManifest": delta,
        "deltaDigest": cloud_service._digest(delta),
        "routeManifest": strict_route,
        "routeChecksum": cloud_service._digest(strict_route),
        "ancestry": cloud_service._ANCESTRY,
        "expectedRevision": context["imageRevision"],
        "lineage": {
            "runId": context["runId"],
            "pageGenerationId": context["generationId"],
            "expectedSequence": context["nextSequence"],
            "actor": actor,
        },
        **{
            key: context[key]
            for key in (
                "projectChecksum",
                "sourceChecksum",
                "g7Checksum",
                "legacyStateChecksum",
                "qualityChecksum",
                "backgroundChecksum",
                "maskArtifactId",
                "maskChecksum",
                "orderedInputs",
                "orderedInputDigest",
            )
        },
    }
    return metadata, normalized


def _ingest(
    *,
    args: argparse.Namespace,
    api_base: str,
    local_client: httpx.Client,
    local: LocalInputs,
    raw: bytes,
    raw_media_type: str,
    route: RouteIdentity,
    quota_class: str,
    invocation_evidence: str,
) -> dict[str, Any]:
    metadata, normalized = _metadata(
        local=local,
        raw=raw,
        raw_media_type=raw_media_type,
        route=route,
        quota_class=quota_class,
        invocation_evidence=invocation_evidence,
        task_id=args.task_id,
        thread_id=args.thread_id,
        session_id=args.session_id,
        normalization_profile=getattr(
            args, "normalization_profile", cloud_service.NORMALIZATION_PROFILE
        ),
    )
    candidate = _json_object(
        local_client.post(
            f"{api_base}/api/images/{args.image_id}/page-gates/cloud-full-page/candidates",
            files={
                "raw": ("raw-output", raw, raw_media_type),
                "normalized": ("strict-composite.png", normalized, "image/png"),
                "metadata": (None, json.dumps(metadata, separators=(",", ":")), "application/json"),
            },
        ),
        "Strict cloud candidate ingest",
    )
    return {
        "status": "ingested-pending-review",
        "executionMode": args.mode,
        "runtime": route.runtime,
        "imageId": args.image_id,
        "generationId": local.context["generationId"],
        "candidateId": candidate.get("candidateId"),
        "invocationId": metadata["invocationId"],
        "invocationEvidenceSha256": hashlib.sha256(invocation_evidence.encode()).hexdigest(),
        "provider": metadata["provider"],
        "tool": metadata["tool"],
        "modelVersion": route.model_version,
        "claimStatus": metadata["claimStatus"],
        "quotaClass": quota_class,
        "promptSha256": metadata["promptSha256"],
        "rawSha256": metadata["rawSha256"],
        "normalizedSha256": metadata["normalizedSha256"],
        "routeChecksum": metadata["routeChecksum"],
        "outsideMaskChangedPixelCount": metadata["deltaManifest"]["outsideMaskChangedPixelCount"],
        **(
            {"normalizationProfile": cloud_service.REGISTRATION_PROFILE}
            if metadata["normalizationManifest"]["profile"] == cloud_service.REGISTRATION_PROFILE
            else {}
        ),
    }


def execute(
    args: argparse.Namespace,
    *,
    environ: dict[str, str],
    local_client: httpx.Client,
    provider_client: httpx.Client | None = None,
) -> dict[str, Any]:
    api_base = _loopback_base(args.api_base)
    normalization_profile = getattr(
        args, "normalization_profile", cloud_service.NORMALIZATION_PROFILE
    )
    if normalization_profile not in (
        cloud_service.NORMALIZATION_PROFILE,
        cloud_service.REGISTRATION_PROFILE,
    ):
        raise CloudImageCLIError("Unknown cloud normalization profile")
    if args.mode != "native" and normalization_profile != cloud_service.NORMALIZATION_PROFILE:
        raise CloudImageCLIError("Registration is available only for explicit native execution")
    _opaque_id(args.image_id, field="image")
    _opaque_id(args.task_id, field="task")
    _opaque_id(args.thread_id, field="thread")
    args.session_id = args.session_id or f"{args.runtime}-native-image-session"
    _opaque_id(args.session_id, field="session")
    if args.mode == "native":
        route = _native_route(args.runtime, args.model_label)
        if args.gemini_model is not None:
            raise CloudImageCLIError("Native execution does not accept --gemini-model")
        if args.quota_class not in {None, "included"}:
            raise CloudImageCLIError("Native subscription execution uses quota class included")
        quota_class = "included"
        prepare_path = _prepare_path(args.prepare_dir)
        if args.execute:
            if prepare_path is not None:
                raise CloudImageCLIError("Native ingest cannot also prepare generation inputs")
            raw = _read_native_raw(args.raw_image)
        elif args.raw_image is not None:
            raise CloudImageCLIError("--raw-image requires --execute")
        local = _fetch_local_inputs(local_client, api_base=api_base, image_id=args.image_id)
        if not args.execute:
            prepared = (
                {}
                if prepare_path is None
                else _prepare_native_inputs(
                    prepare_path,
                    local=local,
                    route=route,
                    image_id=args.image_id,
                    session_id=args.session_id,
                    normalization_profile=getattr(
                        args, "normalization_profile", cloud_service.NORMALIZATION_PROFILE
                    ),
                )
            )
            return {
                "status": (
                    "native-generation-prepared" if prepare_path is not None else "native-ready"
                ),
                "executionMode": "native",
                "runtime": route.runtime,
                "imageId": args.image_id,
                "generationId": local.context["generationId"],
                "qualitySha256": _sha256(local.quality),
                "maskSha256": _sha256(local.mask),
                "provider": route.provider,
                "tool": route.tool,
                "modelVersion": route.model_version,
                "underlyingProvider": "unreported",
                "claimStatus": cloud_service.CLAIM_STATUS,
                "quotaClass": quota_class,
                "prompt": PROMPT,
                "promptSha256": _sha256(PROMPT.encode()),
                **(
                    {"normalizationProfile": normalization_profile}
                    if normalization_profile != cloud_service.NORMALIZATION_PROFILE
                    else {}
                ),
                **prepared,
            }
        native_media_type = "image/png"
        invocation_evidence = cloud_service._digest(
            {
                "runtime": route.runtime,
                "tool": route.tool,
                "modelVersion": route.model_version,
                "rawSha256": _sha256(raw),
                "imageId": args.image_id,
                "generationId": local.context["generationId"],
                "sessionId": args.session_id,
            }
        )
        return _ingest(
            args=args,
            api_base=api_base,
            local_client=local_client,
            local=local,
            raw=raw,
            raw_media_type=native_media_type,
            route=route,
            quota_class=quota_class,
            invocation_evidence=invocation_evidence,
        )

    if args.mode != "gemini-api":
        raise CloudImageCLIError("Unsupported image execution mode")
    route = _gemini_route(args.runtime, args.gemini_model or DEFAULT_GEMINI_MODEL)
    if args.model_label is not None or args.raw_image is not None or args.prepare_dir is not None:
        raise CloudImageCLIError("Direct Gemini fallback does not accept native-only arguments")
    if args.execute and args.quota_class not in {"included", "prepaid"}:
        raise CloudImageCLIError("Direct API execution requires --quota-class included or prepaid")
    environment_key = environ.get("MANGA_LOCALIZER_GEMINI_API_KEY")
    if args.execute and (
        not environment_key
        or any(character in environment_key for character in ("\r", "\n", "\x00"))
    ):
        raise CloudImageCLIError("MANGA_LOCALIZER_GEMINI_API_KEY is unavailable")
    local = _fetch_local_inputs(local_client, api_base=api_base, image_id=args.image_id)
    if not args.execute:
        return {
            "status": "direct-api-fallback-ready",
            "executionMode": "gemini-api",
            "runtime": route.runtime,
            "imageId": args.image_id,
            "generationId": local.context["generationId"],
            "qualitySha256": _sha256(local.quality),
            "maskSha256": _sha256(local.mask),
            "provider": route.provider,
            "tool": route.tool,
            "modelVersion": route.model_version,
            "claimStatus": cloud_service.CLAIM_STATUS,
        }
    if provider_client is None:
        raise CloudImageCLIError("Direct Gemini fallback has no provider client")
    assert environment_key is not None
    provider_response = _checked_response(
        provider_client.post(
            GEMINI_ENDPOINT,
            headers={"x-goog-api-key": environment_key, "Content-Type": "application/json"},
            json=_provider_request(route.model_version, local.quality, local.mask),
        ),
        "Gemini image edit",
    )
    payload = _json_object(provider_response, "Gemini image edit")
    raw, media_type = _output_image(payload)
    invocation_evidence = str(
        payload.get("id") or payload.get("name") or _sha256(provider_response.content)
    )
    receipt = _ingest(
        args=args,
        api_base=api_base,
        local_client=local_client,
        local=local,
        raw=raw,
        raw_media_type=media_type,
        route=route,
        quota_class=args.quota_class,
        invocation_evidence=invocation_evidence,
    )
    return receipt | {"credentialSource": "environment"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or import one native-agent image edit; direct Gemini API use is an explicit "
            "optional fallback."
        )
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--mode", choices=("native", "gemini-api"), default="native")
    parser.add_argument("--runtime", choices=("codex", "cursor"), required=True)
    parser.add_argument("--raw-image")
    parser.add_argument("--prepare-dir")
    parser.add_argument(
        "--normalization-profile",
        choices=(cloud_service.NORMALIZATION_PROFILE, cloud_service.REGISTRATION_PROFILE),
        default=cloud_service.NORMALIZATION_PROFILE,
        help="Explicit opt-in bounded whole-frame registration; default keeps legacy bytes.",
    )
    parser.add_argument(
        "--model-label",
        help="Native tool-reported model label; omit when the native tool does not report one.",
    )
    parser.add_argument("--gemini-model")
    parser.add_argument("--quota-class", choices=("included", "prepaid"))
    parser.add_argument("--task-id", default="manga-native-image")
    parser.add_argument("--thread-id", default="manga-localizer")
    parser.add_argument("--session-id")
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        with httpx.Client(timeout=30, trust_env=False) as local_client:
            if args.mode == "gemini-api":
                with httpx.Client(timeout=180, trust_env=False) as provider_client:
                    receipt = execute(
                        args,
                        environ=dict(os.environ),
                        local_client=local_client,
                        provider_client=provider_client,
                    )
            else:
                receipt = execute(
                    args,
                    environ=dict(os.environ),
                    local_client=local_client,
                )
    except (
        CloudImageCLIError,
        cloud_service.ProjectError,
        httpx.HTTPError,
        KeyError,
        ValueError,
    ) as error:
        sys.stderr.write(f"cloud-image: {error}\n")
        return 1
    sys.stdout.write(json.dumps(receipt, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
