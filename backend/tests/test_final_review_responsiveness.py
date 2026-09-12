from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .test_final_reviews import _ACTOR, _strict_batch


@pytest.mark.parametrize("operation", ["read", "update"])
def test_synchronous_review_request_yields_event_loop_until_durable_completion(
    app, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    store = app.state.final_reviews.get(batch["id"])
    method = "batch" if operation == "read" else "update_item"
    original = getattr(store, method)
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("synthetic review gate timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, method, blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        if operation == "read":
            pending = executor.submit(
                client.get, f"/api/final-review-batches/{batch['id']}?includeItems=false"
            )
        else:
            pending = executor.submit(
                client.patch,
                f"/api/final-review-items/{item['id']}",
                json={
                    "verdict": "issues",
                    "issueCodes": ["ai_inpaint"],
                    "feedback": "synthetic async completion check",
                    "expectedRevision": item["revision"],
                    "expectedBatchRevision": batch["revision"],
                    "actor": _ACTOR,
                },
            )
        try:
            assert entered.wait(5)
            health = executor.submit(client.get, "/api/health").result(timeout=3)
            assert health.status_code == 200
            assert not pending.done(), "HTTP must await the actual review result"
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 200, response.text
    if operation == "update":
        durable = store.item(item["id"])
        assert durable["verdict"] == "issues"
        assert durable["revision"] == item["revision"] + 1
