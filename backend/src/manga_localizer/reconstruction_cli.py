"""Prepare native G2 inputs and import an operator-supplied result; never generate/accept."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

from manga_localizer.services import reconstructions as service


class ReconstructionCLIError(RuntimeError):
    pass


def _base(value):
    url = urlparse(value)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ReconstructionCLIError("The API must be an HTTP loopback origin")
    return value.rstrip("/")


def _label(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
    ):
        raise ReconstructionCLIError("Invalid reconstruction identity label")
    return value


def _read(path, limit):
    path = Path(path)
    if not path.is_absolute():
        raise ReconstructionCLIError("Native input paths must be absolute")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
                raise ReconstructionCLIError("Native input is not a bounded regular file")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ReconstructionCLIError("Native input exceeds the size limit")
        return data
    except OSError as error:
        raise ReconstructionCLIError("Native input could not be read") from error


def _response(response):
    if not 200 <= response.status_code < 300:
        raise ReconstructionCLIError(
            f"Local reconstruction API returned HTTP {response.status_code}"
        )
    return response


def _json(response):
    value = _response(response).json()
    if not isinstance(value, dict):
        raise ReconstructionCLIError("Invalid local reconstruction response")
    return value


def _write_new(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _invocation_id(attempt_id, metadata):
    seed = {
        "attemptId": _label(attempt_id),
        "metadata": service._stable_request(
            {key: value for key, value in metadata.items() if key != "invocationId"}
        ),
    }
    return metadata["runtime"] + "-g2-" + service.digest(seed)[:40]


def prepare(
    *,
    api_base,
    image_id,
    runtime,
    session_id,
    attempt_id,
    prompt_path,
    prepare_dir,
    task_id=None,
    thread_id=None,
    client=None,
):
    base = _base(api_base)
    try:
        image_id = str(uuid.UUID(image_id))
    except ValueError as error:
        raise ReconstructionCLIError("Image identity must be a UUID") from error
    if runtime not in {"codex", "cursor"}:
        raise ReconstructionCLIError("Unsupported native runtime")
    _label(session_id)
    _label(attempt_id)
    actor = {
        "actorKind": runtime,
        "actorId": runtime + "-agent",
        "sessionId": session_id,
        "taskId": task_id,
        "threadId": thread_id,
        "operationSource": "script",
    }
    for value in (task_id, thread_id):
        if value is not None:
            _label(value)
    prompt = _read(prompt_path, 64 * 1024)
    if not prompt.decode("utf-8").strip():
        raise ReconstructionCLIError("The reconstruction prompt is empty")
    directory = Path(prepare_dir)
    if not directory.is_absolute() or directory.exists() or directory.is_symlink():
        raise ReconstructionCLIError("Preparation requires a new absolute directory")
    if client is None:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=120) as local:
            return prepare(
                api_base=base,
                image_id=image_id,
                runtime=runtime,
                session_id=session_id,
                attempt_id=attempt_id,
                prompt_path=prompt_path,
                prepare_dir=directory,
                task_id=task_id,
                thread_id=thread_id,
                client=local,
            )
    route = f"{base}/api/images/{image_id}/page-gates/reconstruction"
    context = _json(client.get(route))
    original = _response(client.get(route + "/inputs/original")).content
    baseline = _response(client.get(route + "/inputs/baseline")).content
    if (
        service.sha(original) != context["sourceChecksum"]
        or service.sha(baseline) != context["baselineChecksum"]
    ):
        raise ReconstructionCLIError("Reconstruction inputs changed during preparation")
    with Image.open(io.BytesIO(original)) as opened:
        suffix = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(opened.format)
    if suffix is None:
        raise ReconstructionCLIError("Unsupported immutable source raster")
    original_name = "original." + suffix
    model = (
        "native-image-model-unreported"
        if runtime == "codex"
        else "auto-native-image-model-unreported"
    )
    metadata = {
        "profile": service.PROFILE,
        "runtime": runtime,
        "tool": "image_gen" if runtime == "codex" else "GenerateImage",
        "provider": "unreported",
        "modelVersion": model,
        "claimStatus": "operator-attested-client-supplied-unverified",
        "promptSha256": service.sha(prompt),
        **{
            key: context[key]
            for key in ("sourceChecksum", "baselineChecksum", "baselineEventId", "decisionEventId")
        },
        "expectedRevision": context["imageRevision"],
        "lineage": {
            "runId": context["runId"],
            "pageGenerationId": context["generationId"],
            "expectedSequence": context["nextSequence"],
            "actor": actor,
        },
    }
    metadata["invocationId"] = _invocation_id(attempt_id, metadata)
    metadata = service.dump_import_request(metadata)
    manifest = {
        "schemaVersion": 1,
        "profile": service.PROFILE,
        "apiBase": base,
        "imageId": image_id,
        "attemptId": attempt_id,
        "metadata": metadata,
        "targetGrid": context["targetGrid"],
        "orderedInputs": [
            {"role": "immutable-original", "file": original_name, "sha256": service.sha(original)},
            {"role": "accepted-G1", "file": "baseline.png", "sha256": service.sha(baseline)},
        ],
        "promptFile": "prompt.txt",
    }
    manifest["manifestDigest"] = service.digest(manifest)
    try:
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        for name, data in (
            (original_name, original),
            ("baseline.png", baseline),
            ("prompt.txt", prompt),
        ):
            _write_new(directory / name, data)
        _write_new(
            directory / "request.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        )
    except OSError as error:
        raise ReconstructionCLIError(
            "Preparation failed; an incomplete private directory may remain"
        ) from error
    return {
        "originalPath": str(directory / original_name),
        "baselinePath": str(directory / "baseline.png"),
        "promptPath": str(directory / "prompt.txt"),
        "requestPath": str(directory / "request.json"),
        "manifestDigest": manifest["manifestDigest"],
        "invocationId": metadata["invocationId"],
    }


def import_result(*, request_path, raw_path, lettering_mask_path=None, client=None):
    if client is None:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=120) as local:
            return import_result(
                request_path=request_path,
                raw_path=raw_path,
                lettering_mask_path=lettering_mask_path,
                client=local,
            )
    manifest = json.loads(_read(request_path, 128 * 1024))
    if not isinstance(manifest, dict) or set(manifest) != {
        "schemaVersion",
        "profile",
        "apiBase",
        "imageId",
        "attemptId",
        "metadata",
        "targetGrid",
        "orderedInputs",
        "promptFile",
        "manifestDigest",
    }:
        raise ReconstructionCLIError("Invalid reconstruction preparation manifest")
    if (
        manifest["schemaVersion"] != 1
        or manifest["profile"] != service.PROFILE
        or manifest["manifestDigest"]
        != service.digest({k: v for k, v in manifest.items() if k != "manifestDigest"})
    ):
        raise ReconstructionCLIError("Reconstruction preparation manifest changed")
    base = _base(manifest["apiBase"])
    iid = str(uuid.UUID(manifest["imageId"]))
    metadata = service.dump_import_request(manifest["metadata"])
    if metadata["invocationId"] != _invocation_id(manifest["attemptId"], metadata):
        raise ReconstructionCLIError("Prepared reconstruction invocation changed")
    if manifest["promptFile"] != "prompt.txt":
        raise ReconstructionCLIError("Invalid prepared prompt reference")
    original_name = manifest["orderedInputs"][0]["file"]
    if original_name not in {"original.png", "original.jpg", "original.webp"}:
        raise ReconstructionCLIError("Invalid immutable source reference")
    expected = [
        {
            "role": "immutable-original",
            "file": original_name,
            "sha256": metadata["sourceChecksum"],
        },
        {"role": "accepted-G1", "file": "baseline.png", "sha256": metadata["baselineChecksum"]},
    ]
    if manifest["orderedInputs"] != expected:
        raise ReconstructionCLIError("Prepared reconstruction input order changed")
    directory = Path(request_path).parent
    for entry in expected:
        if service.sha(_read(directory / entry["file"], service.MAX_RAW_BYTES)) != entry["sha256"]:
            raise ReconstructionCLIError("Prepared reconstruction input changed")
    if service.sha(_read(directory / "prompt.txt", 64 * 1024)) != metadata["promptSha256"]:
        raise ReconstructionCLIError("Prepared reconstruction prompt changed")
    raw = _read(raw_path, service.MAX_RAW_BYTES)
    lettering_mask = None
    if lettering_mask_path is not None:
        lettering_mask = _read(lettering_mask_path, service.MAX_RAW_BYTES)
        metadata["letteringLock"] = True
        metadata["letteringMaskSha256"] = service.sha(lettering_mask)
        metadata["invocationId"] = _invocation_id(manifest["attemptId"], metadata)
        metadata = service.dump_import_request(metadata)
    grid = manifest["targetGrid"]
    normalized, _normalization = service.normalize(raw, (grid["width"], grid["height"]))
    if lettering_mask is not None:
        baseline = _read(directory / "baseline.png", service.MAX_RAW_BYTES)
        normalized, _normalization = service.lock_lettering(normalized, baseline, lettering_mask)
    route = f"{base}/api/images/{iid}/page-gates/reconstruction"
    current = _json(client.get(route))
    if (
        current["generationId"] != metadata["lineage"]["pageGenerationId"]
        or current["runId"] != metadata["lineage"]["runId"]
        or current["targetGrid"] != grid
        or any(
            current[k] != metadata[k]
            for k in ("sourceChecksum", "baselineChecksum", "baselineEventId", "decisionEventId")
        )
    ):
        raise ReconstructionCLIError("Prepared G2 inputs or generation are stale")
    metadata["expectedRevision"] = current["imageRevision"]
    metadata["lineage"]["expectedSequence"] = current["nextSequence"]
    files = {"raw": ("native-result", raw)}
    if lettering_mask is not None:
        files["letteringMask"] = ("lettering-mask.png", lettering_mask)
    result = _json(
        client.post(
            route + "/candidates",
            files=files,
            data={"metadata": json.dumps(metadata, separators=(",", ":"))},
        )
    )
    if (
        result.get("candidateId")
        != service._candidate_id(current["generationId"], metadata["invocationId"])
        or result.get("checksum") != service.sha(normalized)
        or result.get("rawChecksum") != service.sha(raw)
        or result.get("requestDigest") != service.digest(service._stable_request(metadata))
    ):
        raise ReconstructionCLIError("Import receipt does not bind the submitted candidate")
    if result.get("state") != "pending" and not result.get("replayed"):
        raise ReconstructionCLIError("Import did not return a pending candidate")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_subparsers(dest="action", required=True)
    prepare_parser = action.add_parser("prepare")
    prepare_parser.add_argument("--api-base", default="http://127.0.0.1:18080")
    for flag in ("image-id", "runtime", "session-id", "attempt-id", "prompt-path", "prepare-dir"):
        prepare_parser.add_argument("--" + flag, required=True)
    prepare_parser.add_argument("--task-id")
    prepare_parser.add_argument("--thread-id")
    importer = action.add_parser("import")
    importer.add_argument("--request-path", required=True)
    importer.add_argument("--raw-path", required=True)
    importer.add_argument("--lettering-mask", dest="lettering_mask_path")
    args = vars(parser.parse_args(argv))
    mode = args.pop("action")
    try:
        result = prepare(**args) if mode == "prepare" else import_result(**args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ReconstructionCLIError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print(
            "Reconstruction operation failed; no successful receipt is available", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
