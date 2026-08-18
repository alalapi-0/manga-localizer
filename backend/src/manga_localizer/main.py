from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response, UploadFile
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
    ConfigOut,
    HealthOut,
    ImageOut,
    ImageReviewRequest,
    JobOut,
    JobRequest,
    LocalImportRequest,
    OpenAISessionConfig,
    ProjectCreate,
    ProjectOpen,
    ProjectOut,
    ProjectPatch,
    ReadingOrderRequest,
    RegionCreate,
    RegionOut,
    RegionPatch,
    SelectInpaintCandidateRequest,
    StageReviewRequest,
)
from manga_localizer.security import (
    UnsafePathError,
    UnsafeRemoteEndpointError,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.images import (
    StageReviewObservationConflict,
    image_path,
    import_local,
    ingest_bytes,
    invalidate_image_pipeline,
    list_images,
    review_image,
    review_image_stage,
    stage_reviews,
    thumbnail_path,
    validate_image_bytes,
)
from manga_localizer.services.inpaint_candidates import (
    candidate_image_path,
    public_candidates_from_status,
    select_inpaint_candidate,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectNotFound,
    ProjectRegistry,
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
    update_region,
)
from manga_localizer.services.trust import (
    invalidate_trust,
    is_region_trusted,
    recognition_payload,
    recognition_uses_input_variant,
    region_disposition,
)
from manga_localizer.workbench_static import (
    companion_url_for,
    cors_origins_for,
    resolve_frontend_dist,
)

_GENERATED_IMAGE_CACHE_HEADERS = {"Cache-Control": "private, no-store"}


def _generated_image_response(path: Path, media_type: str = "image/png") -> FileResponse:
    return FileResponse(path, media_type=media_type, headers=_GENERATED_IMAGE_CACHE_HEADERS)


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


def _image_dict(image: ImageAsset) -> dict[str, Any]:
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
        "inpaintCandidate": selected_inpaint_candidate,
        "inpaintCandidates": inpaint_candidate_records,
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
    providers = ProviderRegistry(resolved_settings)
    queue = PersistentJobQueue(registry, providers, resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(resolved_settings.log_level)
        registry.load_catalog()
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
    @app.exception_handler(StageReviewObservationConflict)
    @app.exception_handler(JobConflict)
    async def conflict_handler(_request: Request, error: Exception) -> JSONResponse:
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
                project.settings = settings_with_defaults(body.settings, base=project.settings)
                changed_settings = {
                    key
                    for key in previous_settings.keys() | project.settings.keys()
                    if previous_settings.get(key) != project.settings.get(key)
                }
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
        imported = [
            ingest_bytes(store, resolved_settings, data=data, relative_path=path)
            for data, path in zip(buffered, paths, strict=True)
        ]
        return [_image_dict(image) for image in imported]

    @router.post(
        "/projects/{project_id}/images/import-local",
        response_model=list[ImageOut],
        status_code=201,
    )
    async def images_import_local(
        project_id: str, body: LocalImportRequest, response: Response
    ) -> list[dict[str, Any]]:
        store = registry.get(project_id)
        imported, failures = import_local(store, resolved_settings, body.paths)
        if failures:
            response.headers["X-Manga-Localizer-Import-Failures"] = str(len(failures))
        if failures and not imported:
            raise ProjectError(f"No images imported; first failure: {failures[0]['error']}")
        return [_image_dict(image) for image in imported]

    @router.get("/projects/{project_id}/images", response_model=list[ImageOut])
    async def images_list(project_id: str) -> list[dict[str, Any]]:
        return [_image_dict(image) for image in list_images(registry.get(project_id))]

    @router.patch("/images/{image_id}/review", response_model=ImageOut)
    async def image_review(image_id: str, body: ImageReviewRequest) -> dict[str, Any]:
        store, image = registry.find_image(image_id)
        reviewed = review_image(
            store,
            image.id,
            review_state=body.review_state,
            expected_revision=body.expected_revision,
        )
        return _image_dict(reviewed)

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
        )
        return _image_dict(reviewed)

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
        return _image_dict(selected)

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
        region = create_region(store, image.id, body.model_dump())
        return _region_dict(region)

    @router.patch("/regions/{region_id}", response_model=RegionOut)
    async def region_patch(region_id: str, body: RegionPatch) -> dict[str, Any]:
        store, region = registry.find_region(region_id)
        updated = update_region(store, region.id, body.model_dump(exclude_unset=True))
        return _region_dict(updated)

    @router.delete("/regions/{region_id}", status_code=204)
    async def region_delete(
        region_id: str,
        expected_revision: Annotated[int | None, Query(alias="expectedRevision", ge=0)] = None,
    ) -> Response:
        store, region = registry.find_region(region_id)
        delete_region(store, region.id, expected_revision)
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
