from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "manga_state_contract", ROOT / "scripts/check_state.py"
)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def _documents():
    return (
        checker.load_document((ROOT / ".agent/STATE.yaml").read_bytes()),
        checker.load_document((ROOT / "hub.connection.yaml").read_bytes()),
    )


@pytest.mark.parametrize("content", [b"key: 1\nkey: 2\n", b"key: &fact 1\ncopy: *fact\n"])
def test_state_rejects_ambiguous_yaml(content: bytes) -> None:
    with pytest.raises(checker.StateError):
        checker.load_document(content)


def test_current_state_is_readable_with_complete_unknown_fallbacks() -> None:
    state, declaration = _documents()
    result = checker.validate_documents(state, declaration)
    assert result["fields"] == result["fallback_reasons"] == 21
    assert result["unknown"] == 1
    assert not (ROOT / ".agent/STATE.md").exists()


@pytest.mark.parametrize("fault", ["counts", "accepted", "activation", "fallback", "copied_count"])
def test_state_rejects_false_acceptance_or_detached_business_truth(fault: str) -> None:
    state, declaration = map(copy.deepcopy, _documents())
    if fault == "counts":
        state["progress"]["total"] += 1
    elif fault == "accepted":
        state["current_work"]["accepted"] = True
    elif fault == "activation":
        state["execution_control"]["project_execution"] = "active"
    elif fault == "fallback":
        del declaration["unknown_fields"]["progress.completed"]
    else:
        declaration["mapping"]["progress.completed"]["value_map"] = {"140": 140}
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration)
