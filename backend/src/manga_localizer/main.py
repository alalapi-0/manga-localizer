from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from starlette.datastructures import UploadFile as StarletteUploadFile

from manga_localizer import __version__
from manga_localizer.config import Settings, get_settings
from manga_localizer.database import ImageAsset, Job, Project, Revision, TextRegion
from manga_localizer.imaging import font_capabilities
from manga_localizer.logging_utils import configure_logging, redact, without_secrets
from manga_localizer.providers.registry import ProviderRegistry
from manga_localizer.queue import JobConflict, PersistentJobQueue
from manga_localizer.schemas import (
    ConfigOut,
    HealthOut,
    ImageOut,
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
)
from manga_localizer.security import (
    UnsafePathError,
    UnsafeRemoteEndpointError,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.images import (
    image_path,
    import_local,
    ingest_bytes,
    invalidate_image_pipeline,
    list_images,
    thumbnail_path,
    validate_image_bytes,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectNotFound,
    ProjectRegistry,
    RevisionConflict,
    add_revision,
    public_root,
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
    if changed & {"detectorProvider"}:
        stages.update(("detection", "ocr", "translation", "inpaint", "typeset", "export"))
    if changed & {"ocrProvider", "sourceLanguage"}:
        stages.update(("ocr", "translation", "typeset", "export"))
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


def _image_dict(image: ImageAsset) -> dict[str, Any]:
    pipeline_status = {
        key: image.status.get(key, "pending")
        for key in ("detection", "ocr", "translation", "inpaint", "typeset", "export")
    }
    regions = image.__dict__.get("regions", [])
    processing_errors = redact(list(image.processing_errors or []))
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
        "regionCount": len(regions),
        "confirmedCount": sum(region.confirmed for region in regions),
        "ignoredCount": sum(region.ignored for region in regions),
        "processingErrors": processing_errors,
        "error": processing_errors[-1]["error"] if processing_errors else None,
        "revision": image.revision,
        "detectorProvider": image.status.get("detectorProvider") or None,
        "ocrProvider": image.status.get("ocrProvider") or None,
        "translatorProvider": image.status.get("translatorProvider") or None,
        "inpaintingProvider": image.status.get("inpaintingProvider") or None,
        "typesettingProvider": image.status.get("typesettingProvider") or None,
        "thumbnailUrl": f"/api/images/{image.id}/thumbnail",
        "contentUrl": f"/api/images/{image.id}/content",
        "createdAt": image.created_at,
        "updatedAt": image.updated_at,
    }


def _region_dict(region: TextRegion) -> dict[str, Any]:
    return {
        "id": region.id,
        "imageId": region.image_id,
        "x": region.x,
        "y": region.y,
        "width": region.width,
        "height": region.height,
        "rotation": region.rotation,
        "sourceText": region.source_text,
        "translationText": region.translation_text,
        "type": region.region_type,
        "direction": region.direction,
        "order": region.reading_order,
        "confidence": region.confidence,
        "ignored": region.ignored,
        "confirmed": region.confirmed,
        "style": region.style,
        "repair": region.repair,
        "ocrProvider": region.ocr_provider,
        "translationProvider": region.translation_provider,
        "revision": region.revision,
        "createdAt": region.created_at,
        "updatedAt": region.updated_at,
    }


def _job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "projectId": job.project_id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "total": job.total,
        "completed": job.completed,
        "error": redact(job.error) if job.error else None,
        "options": redact(without_secrets(job.options)),
        "items": [
            {
                "id": item.id,
                "imageId": item.image_id,
                "regionId": item.region_id,
                "position": item.position,
                "status": item.status,
                "progress": item.progress,
                "error": redact(item.error) if item.error else None,
                "output": redact(without_secrets(item.output)),
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
    resolved_settings = settings or get_settings()
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
    app.state.ready = False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "If-Match"],
    )

    @app.exception_handler(ProjectNotFound)
    @app.exception_handler(RegionNotFound)
    async def not_found_handler(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(RevisionConflict)
    @app.exception_handler(JobConflict)
    async def conflict_handler(_request: Request, error: Exception) -> JSONResponse:
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
        return {
            "status": "ok" if app.state.ready else "degraded",
            "version": __version__,
            "database": "ok",
            "queue": "running"
            if queue.running
            else ("disabled" if not start_worker else "stopped"),
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

    @router.get("/images/{image_id}/content")
    async def image_content(
        image_id: str,
        variant: Annotated[str, Query()] = "original",
    ) -> FileResponse:
        store, image = registry.find_image(image_id)
        if variant in {"erased", "inpainted", "typeset"}:
            stage = "inpainted" if variant in {"erased", "inpainted"} else "typeset"
            status_stage = "inpaint" if stage == "inpainted" else "typeset"
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
            return FileResponse(target, media_type="image/png")
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

    @router.get("/images/{image_id}/generated/{stage}")
    async def image_generated(image_id: str, stage: str) -> FileResponse:
        stage_directory = {"inpainted": "inpainted", "typeset": "typeset", "mask": "masks"}
        if stage not in stage_directory:
            raise HTTPException(status_code=404, detail="Unknown generated image stage")
        store, image = registry.find_image(image_id)
        status_stage = "inpaint" if stage in {"inpainted", "mask"} else "typeset"
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
        return FileResponse(target, media_type="image/png")

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
        if kind not in {"detect", "ocr", "translate", "render", "export", "inpaint", "typeset"}:
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
