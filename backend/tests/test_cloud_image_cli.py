from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from manga_localizer import cloud_image_cli
from manga_localizer.services import cloud_full_page_clean_plates as cloud_service


def _png(color: str, size: tuple[int, int] = (4, 6)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(color: str) -> bytes:
    image = Image.new("RGB", (4, 6), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _mask() -> bytes:
    image = Image.new("L", (4, 6), 0)
    image.putpixel((1, 2), 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _args(
    *,
    mode: str = "native",
    runtime: str = "codex",
    raw_image: str | None = None,
    prepare_dir: str | None = None,
    model_label: str | None = None,
    gemini_model: str | None = None,
    execute: bool = True,
    quota_class: str | None = None,
    session_id: str | None = "session-1",
) -> argparse.Namespace:
    return argparse.Namespace(
        api_base="http://127.0.0.1:8000",
        image_id="image-1",
        mode=mode,
        runtime=runtime,
        raw_image=raw_image,
        prepare_dir=prepare_dir,
        model_label=model_label,
        gemini_model=gemini_model,
        quota_class=quota_class,
        task_id="task-1",
        thread_id="thread-1",
        session_id=session_id,
        execute=execute,
    )


def _context(quality: bytes, mask: bytes) -> dict[str, object]:
    ordered = [
        {
            "position": 1,
            "role": "quality-plate",
            "sha256": hashlib.sha256(quality).hexdigest(),
            "width": 4,
            "height": 6,
        },
        {
            "position": 2,
            "role": "accepted-g7-mask",
            "sha256": hashlib.sha256(mask).hexdigest(),
            "width": 4,
            "height": 6,
        },
    ]
    return {
        "imageRevision": 7,
        "generationId": "generation-1",
        "runId": "run-1",
        "nextSequence": 9,
        "projectChecksum": "1" * 64,
        "sourceChecksum": "2" * 64,
        "g7Checksum": "3" * 64,
        "legacyStateChecksum": "4" * 64,
        "qualityChecksum": hashlib.sha256(quality).hexdigest(),
        "backgroundChecksum": "5" * 64,
        "maskArtifactId": "mask-1",
        "maskChecksum": hashlib.sha256(mask).hexdigest(),
        "orderedInputs": ordered,
        "orderedInputDigest": cloud_service._digest(ordered),
        "targetGrid": {"width": 4, "height": 6},
    }


def _raw_with_inside_and_outside_changes(quality: bytes) -> bytes:
    provider_image = Image.open(io.BytesIO(quality)).convert("RGB")
    provider_image.putpixel((1, 2), (1, 2, 3))
    provider_image.putpixel((0, 0), (4, 5, 6))
    return cloud_service._png_bytes(provider_image)


def _local_handler(
    *,
    quality: bytes,
    mask: bytes,
    observed: dict[str, object],
) -> httpx.MockTransport:
    context = _context(quality, mask)

    def handler(request: httpx.Request) -> httpx.Response:
        observed["localCalls"] = int(observed.get("localCalls", 0)) + 1
        if request.method == "GET" and request.url.path.endswith("cloud-full-page"):
            return httpx.Response(200, json=context)
        if request.method == "GET" and request.url.path.endswith("generated/quality"):
            return httpx.Response(200, content=quality, headers={"Content-Type": "image/png"})
        if request.method == "GET" and "/page-gates/mask/artifacts/" in request.url.path:
            return httpx.Response(200, content=mask, headers={"Content-Type": "image/png"})
        if request.method == "POST" and request.url.path.endswith("/candidates"):
            observed["ingestCalls"] = int(observed.get("ingestCalls", 0)) + 1
            body = request.content.decode("utf-8", errors="ignore")
            observed["ingestBody"] = body
            assert '"outsideMaskChangedPixelCount":0' in body
            assert '"originKind":"deterministic-mask-composite"' in body
            return httpx.Response(200, json={"candidateId": "candidate-1"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("runtime", "provider", "tool", "model", "api_profile"),
    [
        (
            "codex",
            "codex-native-route",
            "image_gen",
            "native-image-model-unreported",
            "codex-native-subscription-v1",
        ),
        (
            "cursor",
            "cursor-native-route",
            "GenerateImage",
            "auto-native-image-model-unreported",
            "cursor-native-subscription-v1",
        ),
    ],
)
def test_native_execute_imports_local_raw_without_key_or_provider_call(
    tmp_path: Path,
    runtime: str,
    provider: str,
    tool: str,
    model: str,
    api_profile: str,
):
    quality = _png("white")
    mask = _mask()
    raw_path = tmp_path / "native.png"
    raw_path.write_bytes(_raw_with_inside_and_outside_changes(quality))
    observed: dict[str, object] = {"localCalls": 0, "ingestCalls": 0}
    with httpx.Client(
        transport=_local_handler(quality=quality, mask=mask, observed=observed)
    ) as local_client:
        receipt = cloud_image_cli.execute(
            _args(runtime=runtime, raw_image=str(raw_path)),
            environ={"MANGA_LOCALIZER_GEMINI_API_KEY": "must-be-ignored"},
            local_client=local_client,
            provider_client=None,
        )
    body = str(observed["ingestBody"])
    assert observed["ingestCalls"] == 1
    assert f'"actorKind":"{runtime}"' in body
    assert '"operationSource":"script"' in body
    assert f'"provider":"{provider}"' in body
    assert f'"tool":"{tool}"' in body
    assert f'"modelVersion":"{model}"' in body
    assert f'"apiProfile":"{api_profile}"' in body
    assert "must-be-ignored" not in body
    assert receipt == {
        **receipt,
        "status": "ingested-pending-review",
        "executionMode": "native",
        "runtime": runtime,
        "candidateId": "candidate-1",
        "provider": provider,
        "tool": tool,
        "modelVersion": model,
        "claimStatus": cloud_service.CLAIM_STATUS,
        "quotaClass": "included",
        "outsideMaskChangedPixelCount": 0,
    }
    assert "credentialSource" not in receipt


def test_registration_failure_does_not_ingest_or_call_provider(tmp_path):
    quality, mask = _png("white"), _mask()
    raw_path = tmp_path / "native.png"
    raw_path.write_bytes(quality)
    observed = {"localCalls": 0, "ingestCalls": 0}
    args = _args(raw_image=str(raw_path))
    args.normalization_profile = cloud_service.REGISTRATION_PROFILE
    with httpx.Client(
        transport=_local_handler(quality=quality, mask=mask, observed=observed)
    ) as local:
        with pytest.raises(cloud_service.ProjectError, match="too small"):
            cloud_image_cli.execute(args, environ={}, local_client=local, provider_client=None)
    assert observed["ingestCalls"] == 0


def test_registration_is_native_only_without_any_api_effect():
    args = _args(mode="gemini-api")
    args.normalization_profile = cloud_service.REGISTRATION_PROFILE
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("external effect"))
    ) as local:
        with pytest.raises(cloud_image_cli.CloudImageCLIError, match="native execution"):
            cloud_image_cli.execute(args, environ={}, local_client=local, provider_client=local)


def test_native_dry_run_needs_no_key_raw_or_provider_client():
    quality = _png("white")
    mask = _mask()
    observed: dict[str, object] = {"localCalls": 0, "ingestCalls": 0}
    with httpx.Client(
        transport=_local_handler(quality=quality, mask=mask, observed=observed)
    ) as local_client:
        receipt = cloud_image_cli.execute(
            _args(execute=False),
            environ={},
            local_client=local_client,
            provider_client=None,
        )
    assert receipt["status"] == "native-ready"
    assert receipt["underlyingProvider"] == "unreported"
    assert receipt["tool"] == "image_gen"
    assert receipt["prompt"] == cloud_image_cli.PROMPT
    assert observed == {"localCalls": 3, "ingestCalls": 0}


def test_native_prepare_exports_checksum_bound_inputs_and_nonsecret_manifest(tmp_path: Path):
    quality = _png("white")
    mask = _mask()
    output = tmp_path / "native-inputs"
    observed: dict[str, object] = {"localCalls": 0, "ingestCalls": 0}
    with httpx.Client(
        transport=_local_handler(quality=quality, mask=mask, observed=observed)
    ) as local_client:
        receipt = cloud_image_cli.execute(
            _args(runtime="cursor", execute=False, prepare_dir=str(output)),
            environ={"MANGA_LOCALIZER_GEMINI_API_KEY": "must-not-be-exported"},
            local_client=local_client,
        )
    assert receipt["status"] == "native-generation-prepared"
    assert (output / "quality.png").read_bytes() == quality
    assert (output / "mask.png").read_bytes() == mask
    manifest_text = (output / "request.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["runtime"] == "cursor"
    assert manifest["tool"] == "GenerateImage"
    assert manifest["underlyingProvider"] == "unreported"
    assert manifest["claimStatus"] == cloud_service.CLAIM_STATUS
    assert manifest["sessionId"] == "session-1"
    assert manifest["prompt"] == cloud_image_cli.PROMPT
    assert "must-not-be-exported" not in manifest_text


def test_main_loopback_client_disables_env_proxy(monkeypatch):
    seen: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)

        def __enter__(self):
            raise ValueError("stop-after-client-kwargs")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cloud_image_cli.httpx, "Client", FakeClient)
    assert (
        cloud_image_cli.main(
            [
                "--runtime",
                "cursor",
                "--image-id",
                "image-1",
            ]
        )
        == 1
    )
    assert seen
    assert seen[0].get("trust_env") is False


def test_main_sanitizes_native_prepare_inspection_failure(tmp_path: Path, monkeypatch, capsys):
    sensitive_detail = "SENSITIVE-inspection-detail"

    def failed_exists(_path):
        raise PermissionError(sensitive_detail)

    monkeypatch.setattr(Path, "exists", failed_exists)
    assert (
        cloud_image_cli.main(
            [
                "--runtime",
                "codex",
                "--image-id",
                "image-1",
                "--prepare-dir",
                str(tmp_path / "native-inputs"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cloud-image: Native preparation directory could not be inspected\n"
    assert sensitive_detail not in captured.err
    assert "Traceback" not in captured.err


def test_main_sanitizes_incomplete_native_prepare_write(tmp_path: Path, monkeypatch, capsys):
    quality = _png("white")
    mask = _mask()
    output = tmp_path / "native-inputs"
    observed: dict[str, object] = {"localCalls": 0, "ingestCalls": 0}
    client_class = httpx.Client

    def local_client(*_args, **_kwargs):
        return client_class(transport=_local_handler(quality=quality, mask=mask, observed=observed))

    original_write_new = cloud_image_cli._write_new

    def fail_after_quality(path: Path, payload: bytes):
        if path.name == "mask.png":
            raise OSError("SENSITIVE-write-detail")
        original_write_new(path, payload)

    monkeypatch.setattr(cloud_image_cli.httpx, "Client", local_client)
    monkeypatch.setattr(cloud_image_cli, "_write_new", fail_after_quality)
    assert (
        cloud_image_cli.main(
            [
                "--runtime",
                "codex",
                "--image-id",
                "image-1",
                "--prepare-dir",
                str(output),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "cloud-image: Native preparation failed; the preparation directory may be incomplete\n"
    )
    assert "SENSITIVE-write-detail" not in captured.err
    assert "Traceback" not in captured.err
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "quality.png").read_bytes() == quality
    assert not (output / "mask.png").exists()
    assert not (output / "request.json").exists()
    assert observed == {"localCalls": 3, "ingestCalls": 0}


def test_native_default_session_and_invocation_are_stable_across_process_retries(
    tmp_path: Path,
):
    quality = _png("white")
    mask = _mask()
    raw_path = tmp_path / "native.png"
    raw_path.write_bytes(_raw_with_inside_and_outside_changes(quality))
    receipts = []
    bodies = []
    for _ in range(2):
        observed: dict[str, object] = {"localCalls": 0, "ingestCalls": 0}
        with httpx.Client(
            transport=_local_handler(quality=quality, mask=mask, observed=observed)
        ) as local_client:
            receipts.append(
                cloud_image_cli.execute(
                    _args(raw_image=str(raw_path), session_id=None),
                    environ={},
                    local_client=local_client,
                )
            )
        bodies.append(str(observed["ingestBody"]))
    assert receipts[0]["invocationId"] == receipts[1]["invocationId"]
    assert all('"sessionId":"codex-native-image-session"' in body for body in bodies)


def test_main_reports_native_aspect_failure_without_traceback(tmp_path: Path, monkeypatch, capsys):
    quality = _png("white")
    mask = _mask()
    raw_path = tmp_path / "landscape.png"
    raw_path.write_bytes(_png("white", (6, 4)))
    client_class = httpx.Client

    def local_client(*_args, **_kwargs):
        return client_class(transport=_local_handler(quality=quality, mask=mask, observed={}))

    monkeypatch.setattr(cloud_image_cli.httpx, "Client", local_client)
    assert (
        cloud_image_cli.main(
            [
                "--runtime",
                "codex",
                "--image-id",
                "image-1",
                "--raw-image",
                str(raw_path),
                "--execute",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "aspect differs" in captured.err
    assert "Traceback" not in captured.err


def test_direct_gemini_fallback_is_explicit_single_call_and_strict_ingest():
    quality = _png("white")
    mask = _mask()
    provider_raw = _raw_with_inside_and_outside_changes(quality)
    observed: dict[str, object] = {
        "localCalls": 0,
        "ingestCalls": 0,
        "providerCalls": 0,
    }

    def provider_handler(request: httpx.Request) -> httpx.Response:
        observed["providerCalls"] = int(observed["providerCalls"]) + 1
        assert request.headers["x-goog-api-key"] == "test-api-key"
        payload = json.loads(request.content)
        assert len(payload["input"]) == 3
        assert "test-api-key" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "id": "provider-interaction-1",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "image",
                                "mime_type": "image/png",
                                "data": base64.b64encode(provider_raw).decode(),
                            }
                        ],
                    }
                ],
            },
        )

    with (
        httpx.Client(
            transport=_local_handler(quality=quality, mask=mask, observed=observed)
        ) as local_client,
        httpx.Client(transport=httpx.MockTransport(provider_handler)) as provider_client,
    ):
        receipt = cloud_image_cli.execute(
            _args(mode="gemini-api", runtime="cursor", quota_class="prepaid"),
            environ={"MANGA_LOCALIZER_GEMINI_API_KEY": "test-api-key"},
            local_client=local_client,
            provider_client=provider_client,
        )
    assert observed["providerCalls"] == 1
    assert observed["ingestCalls"] == 1
    assert '"quotaClass":"prepaid"' in str(observed["ingestBody"])
    assert receipt["executionMode"] == "gemini-api"
    assert receipt["provider"] == "google-gemini-api"
    assert receipt["credentialSource"] == "environment"
    assert "test-api-key" not in json.dumps(receipt)


def test_direct_gemini_fallback_missing_key_fails_before_local_or_provider_call():
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid fallback must have no network effect")

    with (
        httpx.Client(transport=httpx.MockTransport(forbidden)) as local_client,
        httpx.Client(transport=httpx.MockTransport(forbidden)) as provider_client,
        pytest.raises(cloud_image_cli.CloudImageCLIError, match="unavailable"),
    ):
        cloud_image_cli.execute(
            _args(mode="gemini-api", quota_class="included"),
            environ={},
            local_client=local_client,
            provider_client=provider_client,
        )


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com",
        "http://127.0.0.1:8000?token=value",
        "http://user:password@localhost:8000",
    ],
)
def test_loopback_base_rejects_remote_or_credential_bearing_addresses(value: str):
    with pytest.raises(cloud_image_cli.CloudImageCLIError):
        cloud_image_cli._loopback_base(value)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (_args(runtime="unknown"), "runtime"),
        (_args(mode="unknown"), "Unsupported"),
        (_args(raw_image="relative.png"), "absolute"),
        (_args(raw_image=None), "raw-image"),
        (_args(raw_image="/missing.png", quota_class="prepaid"), "quota class"),
        (
            _args(raw_image="/missing.png", gemini_model="gemini-3.1-flash-image"),
            "gemini-model",
        ),
        (_args(execute=False, raw_image="/unused.png"), "requires --execute"),
        (_args(mode="gemini-api", quota_class=None), "quota-class"),
    ],
)
def test_invalid_mode_combinations_fail_before_loopback(args: argparse.Namespace, message: str):
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid arguments must fail before loopback access")

    with (
        httpx.Client(transport=httpx.MockTransport(forbidden)) as local_client,
        pytest.raises(cloud_image_cli.CloudImageCLIError, match=message),
    ):
        cloud_image_cli.execute(
            args,
            environ={"MANGA_LOCALIZER_GEMINI_API_KEY": "unused"},
            local_client=local_client,
            provider_client=None,
        )


def test_native_rejects_symlink_before_loopback(tmp_path: Path):
    target = tmp_path / "target.png"
    target.write_bytes(_png("white"))
    link = tmp_path / "link.png"
    link.symlink_to(target)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: (_ for _ in ()).throw(
                    AssertionError("symlink must fail before loopback access")
                )
            )
        ) as local_client,
        pytest.raises(cloud_image_cli.CloudImageCLIError, match="symlink"),
    ):
        cloud_image_cli.execute(
            _args(raw_image=str(link)),
            environ={},
            local_client=local_client,
        )


def test_native_rejects_non_png_raw(tmp_path: Path):
    quality = _png("white")
    mask = _mask()
    raw_path = tmp_path / "native.jpg"
    raw_path.write_bytes(_jpeg("white"))
    observed: dict[str, object] = {"localCalls": 0, "ingestCalls": 0}
    with (
        httpx.Client(
            transport=_local_handler(quality=quality, mask=mask, observed=observed)
        ) as local_client,
        pytest.raises(cloud_image_cli.CloudImageCLIError, match="media type"),
    ):
        cloud_image_cli.execute(
            _args(raw_image=str(raw_path)),
            environ={},
            local_client=local_client,
        )
    assert observed["ingestCalls"] == 0


def test_native_rejects_oversize_raw_before_loopback(tmp_path: Path, monkeypatch):
    raw_path = tmp_path / "native.png"
    raw_path.write_bytes(_png("white"))
    monkeypatch.setattr(cloud_service, "MAX_RAW_BYTES", 1)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: (_ for _ in ()).throw(
                    AssertionError("oversize raw must fail before loopback access")
                )
            )
        ) as local_client,
        pytest.raises(cloud_image_cli.CloudImageCLIError, match="byte limit"),
    ):
        cloud_image_cli.execute(
            _args(raw_image=str(raw_path)),
            environ={},
            local_client=local_client,
        )


def test_parser_exposes_no_provider_or_keychain_override():
    parser = cloud_image_cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--image-id",
                "image-1",
                "--runtime",
                "cursor",
                "--provider",
                "invented-provider",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--image-id",
                "image-1",
                "--runtime",
                "cursor",
                "--credential-source",
                "keychain",
            ]
        )
