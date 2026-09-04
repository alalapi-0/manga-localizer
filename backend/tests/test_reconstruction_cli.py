import json
import os
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from manga_localizer import reconstruction_cli as cli
from manga_localizer.services import reconstructions as service

from .conftest import png_bytes
from .test_reconstructions import _prepare, _snapshot


def _request(client, app, tmp_path, runtime="codex"):
    prepared = _prepare(client, app, tmp_path)
    prompt = tmp_path / "g2-prompt.txt"
    prompt.write_text("Restore fine lines; preserve all identity, expression, text and objects.")
    args = dict(
        api_base="http://127.0.0.1:18080",
        image_id=prepared["targetImage"]["id"],
        runtime=runtime,
        session_id="g2-test-session",
        attempt_id="attempt-1",
        prompt_path=prompt,
        prepare_dir=tmp_path / "native",
        client=client,
    )
    receipt = cli.prepare(**args)
    raw = tmp_path / "native-raw.png"
    raw.write_bytes(png_bytes(color="gray"))
    return prepared, args, receipt, raw


@pytest.mark.parametrize("runtime", ["codex", "cursor"])
def test_prepare_import_replay_only_local_pending_and_exact_provenance(
    client, app, tmp_path, runtime, monkeypatch
):
    prepared, args, receipt, raw = _request(client, app, tmp_path, runtime)
    directory = Path(receipt["requestPath"]).parent
    assert directory.stat().st_mode & 0o777 == 0o700
    manifest = json.loads(Path(receipt["requestPath"]).read_text())
    assert manifest["orderedInputs"][0]["role"] == "immutable-original"
    assert manifest["orderedInputs"][1]["role"] == "accepted-G1"
    assert Path(receipt["originalPath"]).suffix == ".png"
    assert service.sha(Path(receipt["originalPath"]).read_bytes()) == service.sha(prepared["data"])
    assert "mask" not in json.dumps(manifest)
    calls = []
    request = client.request

    def observe(method, url, **kwargs):
        calls.append((method, str(url)))
        return request(method, url, **kwargs)

    monkeypatch.setattr(client, "request", observe)
    candidate = cli.import_result(request_path=receipt["requestPath"], raw_path=raw, client=client)
    assert candidate["state"] == "pending" and not candidate["replayed"]
    assert candidate["rawChecksum"] == service.sha(raw.read_bytes())
    assert candidate["request"]["runtime"] == runtime
    assert candidate["request"]["provider"] == "unreported"
    before = _snapshot(prepared["store"])
    replay = cli.import_result(request_path=receipt["requestPath"], raw_path=raw, client=client)
    assert replay["candidateId"] == candidate["candidateId"] and replay["replayed"]
    assert _snapshot(prepared["store"]) == before
    assert all(url.startswith("http://127.0.0.1:18080/") for _, url in calls)
    assert [method for method, _ in calls] == ["GET", "POST", "GET", "POST"]
    assert all("/page-gates/reconstruction" in url for _, url in calls)
    # CAS changes caused by import cannot mint another invocation for this attempt.
    next_preparation = cli.prepare(**(args | {"prepare_dir": tmp_path / "same-attempt"}))
    assert next_preparation["invocationId"] == receipt["invocationId"]


def test_import_lettering_lock_recomputes_invocation_and_replays(client, app, tmp_path):
    prepared, _args, receipt, raw = _request(client, app, tmp_path)
    directory = Path(receipt["requestPath"]).parent
    with Image.open(directory / "baseline.png") as baseline:
        mask_image = Image.new("L", baseline.size, 0)
        ImageDraw.Draw(mask_image).rectangle((0, 0, 24, 24), fill=255)
    mask_path = tmp_path / "lettering-mask.png"
    mask_image.save(mask_path)
    manifest = json.loads(Path(receipt["requestPath"]).read_text())
    grid = (manifest["targetGrid"]["width"], manifest["targetGrid"]["height"])
    native, _ = service.normalize(raw.read_bytes(), grid)
    locked, lock_manifest = service.lock_lettering(
        native, (directory / "baseline.png").read_bytes(), mask_path.read_bytes()
    )
    candidate = cli.import_result(
        request_path=receipt["requestPath"],
        raw_path=raw,
        lettering_mask_path=mask_path,
        client=client,
    )
    assert candidate["state"] == "pending" and not candidate["replayed"]
    assert candidate["request"]["invocationId"] != receipt["invocationId"]
    assert candidate["request"]["letteringLock"] is True
    assert candidate["checksum"] == service.sha(locked) != service.sha(native)
    assert lock_manifest["letteringLock"] is True
    before = _snapshot(prepared["store"])
    replay = cli.import_result(
        request_path=receipt["requestPath"],
        raw_path=raw,
        lettering_mask_path=mask_path,
        client=client,
    )
    assert replay["candidateId"] == candidate["candidateId"] and replay["replayed"]
    assert _snapshot(prepared["store"]) == before
    unlocked = cli.import_result(request_path=receipt["requestPath"], raw_path=raw, client=client)
    assert unlocked["candidateId"] != candidate["candidateId"]
    assert unlocked["checksum"] == service.sha(native)


@pytest.mark.parametrize(
    "change",
    [
        "manifest",
        "resealed-attempt",
        "reordered-inputs",
        "prompt",
        "original",
        "baseline",
        "raw-symlink",
        "raw-fifo",
        "invalid-raster",
    ],
)
def test_tampered_preparation_and_bad_raw_fail_before_network(client, app, tmp_path, change):
    prepared, _args, receipt, raw = _request(client, app, tmp_path)
    if change in {"manifest", "resealed-attempt", "reordered-inputs"}:
        p = Path(receipt["requestPath"])
        body = json.loads(p.read_text())
        if change == "reordered-inputs":
            body["orderedInputs"].reverse()
        else:
            body["attemptId"] = "tampered"
        if change != "manifest":
            body["manifestDigest"] = service.digest(
                {key: value for key, value in body.items() if key != "manifestDigest"}
            )
        p.write_text(json.dumps(body))
    elif change in {"prompt", "original", "baseline"}:
        Path(receipt[change + "Path"]).write_bytes(b"changed")
    elif change == "raw-symlink":
        link = tmp_path / "linked.png"
        link.symlink_to(raw)
        raw = link
    elif change == "raw-fifo":
        raw = tmp_path / "raw-fifo"
        os.mkfifo(raw)
    else:
        raw.write_bytes(b"invalid raster")
    before = _snapshot(prepared["store"])

    def no_network(request):
        pytest.fail("Invalid prepared inputs reached the network")

    with httpx.Client(transport=httpx.MockTransport(no_network)) as local:
        with pytest.raises((cli.ReconstructionCLIError, service.ProjectError)):
            cli.import_result(request_path=receipt["requestPath"], raw_path=raw, client=local)
    assert _snapshot(prepared["store"]) == before


@pytest.mark.parametrize("field", ["decisionEventId", "baselineEventId", "generationId"])
def test_generation_change_rejects_without_post(client, app, tmp_path, field):
    _prepared, _args, receipt, raw = _request(client, app, tmp_path)
    calls = []

    def stale(request):
        calls.append(request.method)
        assert request.method == "GET"
        context = client.get(request.url.path).json()
        context[field] = "00000000-0000-0000-0000-000000000000"
        return httpx.Response(200, json=context)

    with httpx.Client(transport=httpx.MockTransport(stale)) as local:
        with pytest.raises(cli.ReconstructionCLIError, match="stale"):
            cli.import_result(request_path=receipt["requestPath"], raw_path=raw, client=local)
    assert calls == ["GET"]


@pytest.mark.parametrize("field", ["candidateId", "checksum", "rawChecksum", "requestDigest"])
def test_invalid_import_receipt_does_not_report_success(client, app, tmp_path, field):
    _prepared, _args, receipt, raw = _request(client, app, tmp_path)

    def wrong_receipt(request):
        response = client.request(
            request.method, request.url.path, content=request.content, headers=request.headers
        )
        body = response.json()
        assert response.status_code == 200
        if request.method == "POST":
            body[field] = "invalid"
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(wrong_receipt)) as local:
        with pytest.raises(cli.ReconstructionCLIError, match="receipt"):
            cli.import_result(request_path=receipt["requestPath"], raw_path=raw, client=local)


def test_default_import_reads_and_normalizes_once(client, app, tmp_path, monkeypatch):
    _prepared, _args, receipt, raw = _request(client, app, tmp_path)
    counts = {"read": 0, "normalize": 0}
    read, normalize = cli._read, service.normalize

    def read_once(path, limit):
        counts["read"] += 1
        return read(path, limit)

    def normalize_once(data, grid):
        counts["normalize"] += 1
        return normalize(data, grid)

    class ContextOnly:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, route):
            raise RuntimeError("stop before server normalization")

    monkeypatch.setattr(cli.httpx, "Client", lambda **_: ContextOnly())
    monkeypatch.setattr(cli, "_read", read_once)
    monkeypatch.setattr(service, "normalize", normalize_once)
    with pytest.raises(RuntimeError, match="stop before"):
        cli.import_result(request_path=receipt["requestPath"], raw_path=raw)
    assert counts == {"read": 5, "normalize": 1}


@pytest.mark.parametrize(
    "base",
    [
        "https://example.com",
        "http://127.0.0.1/extra",
        "http://u:p@localhost",
        "http://localhost?x=1",
    ],
)
def test_cli_refuses_nonlocal_or_ambiguous_origins_before_network(tmp_path, base):
    with pytest.raises(cli.ReconstructionCLIError):
        cli.prepare(
            api_base=base,
            image_id="bad",
            runtime="codex",
            session_id="a",
            attempt_id="a",
            prompt_path=tmp_path / "missing",
            prepare_dir=tmp_path / "native",
        )


def test_preparation_failure_is_private_incomplete_and_cli_errors_redacted(
    client, app, tmp_path, monkeypatch, capsys
):
    _prepared, args, _receipt, _raw = _request(client, app, tmp_path)

    def fail(path, data):
        raise OSError("SENSITIVE-FILE-OR-URL")

    monkeypatch.setattr(cli, "_write_new", fail)
    target = tmp_path / "incomplete"
    with pytest.raises(cli.ReconstructionCLIError) as error:
        cli.prepare(**(args | {"prepare_dir": target}))
    assert "SENSITIVE" not in str(error.value)
    assert target.stat().st_mode & 0o777 == 0o700
    assert not (target / "request.json").exists()

    def fail_import(**kwargs):
        raise httpx.ConnectError("SECRET transport detail")

    monkeypatch.setattr(cli, "import_result", fail_import)
    assert cli.main(["import", "--request-path", "/input", "--raw-path", "/raw"]) == 1
    captured = capsys.readouterr()
    assert "SECRET" not in captured.err and "Traceback" not in captured.err and not captured.out


def test_default_client_ignores_proxy_environment_and_redirects(tmp_path, monkeypatch):
    # Capture constructor options without allowing a real network operation.
    observed = {}

    def client(**kwargs):
        observed.update(kwargs)
        raise RuntimeError("stop before network")

    monkeypatch.setattr(cli.httpx, "Client", client)
    monkeypatch.setenv("HTTP_PROXY", "http://should-not-be-used.invalid")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Restore, preserve all content.")
    with pytest.raises(RuntimeError):
        cli.prepare(
            api_base="http://127.0.0.1:18080",
            image_id="00000000-0000-0000-0000-000000000001",
            runtime="codex",
            session_id="a",
            attempt_id="a",
            prompt_path=prompt,
            prepare_dir=tmp_path / "out",
        )
    assert observed["trust_env"] is False and observed["follow_redirects"] is False
