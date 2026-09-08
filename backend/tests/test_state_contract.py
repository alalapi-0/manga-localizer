from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
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


def _bounded_documents(tmp_path):
    state, declaration = map(copy.deepcopy, _documents())
    base = "docs/reports/all-projects-governance/fixture"
    folder = tmp_path / base
    folder.mkdir(parents=True)
    contract_id = "SYNTHETIC-BOUND-RECOVERY"
    contract = f"# {contract_id}\nSynthetic declaration fixture, not an owner grant.\n".encode()
    (folder / "contract.md").write_bytes(contract)
    target = {
        "page": 134,
        "project_id": "00000000-0000-4000-8000-000000000001",
        "batch_id": state["catalog_observation"]["batch_id"],
        "item_id": "00000000-0000-4000-8000-000000000002",
        "image_id": "00000000-0000-4000-8000-000000000003",
        "generation_id": "00000000-0000-4000-8000-000000000004",
        "run_id": "synthetic-run",
    }
    recovery = {
        "contract_id": contract_id,
        "contract": f"{base}/contract.md",
        "registration": f"{base}/candidate.json",
        "activation_ref": "owner-goal:synthetic-test",
        "target": target,
        "limits": {
            "page_slots": 1,
            "native_generation_limit": 1,
            "g8_ingest_concurrency": 1,
            "automatic_owner_approval": False,
            "export_allowed": False,
            "background_worker_allowed": False,
        },
        "effects": sorted(checker.RECOVERY_EFFECTS),
    }
    registration = {
        **copy.deepcopy(recovery),
        "stage": "C",
        "contract_sha256": hashlib.sha256(contract).hexdigest(),
        "accepted_execution_plan": {
            "judge": "PASS",
            "governor": "APPROVE",
            "semantic_sha256": "a" * 64,
        },
    }
    (folder / "candidate.json").write_text(json.dumps(registration))
    state["all_projects_governance"].update(
        {
            "stage": "C",
            "contract": recovery["contract"],
            "candidate_manifest": recovery["registration"],
        }
    )
    state["execution_control"].update(
        {
            "project_execution": checker.BOUNDED_EXECUTION,
            "recovery": recovery,
        }
    )
    return state, declaration, registration, folder


def test_bound_recovery_is_readable_but_validation_never_grants_authority(tmp_path):
    state, declaration, _, _ = _bounded_documents(tmp_path)
    result = checker.validate_documents(state, declaration, root=tmp_path)
    assert result["execution_declaration"] == checker.BOUNDED_EXECUTION
    assert result["grants_execution_authority"] is False
    assert result["fields"] == 21
    # Closing a unit retains its historical declaration without activating it.
    state["execution_control"]["project_execution"] = checker.INACTIVE_EXECUTION
    state["all_projects_governance"]["stage"] = "D"
    assert (
        checker.validate_documents(state, declaration, root=tmp_path)["execution_declaration"]
        == checker.INACTIVE_EXECUTION
    )


@pytest.mark.parametrize(
    "fault",
    [
        "stage",
        "activation",
        "contract_missing",
        "contract_changed",
        "unaccepted_plan",
        "target_changed",
        "identity_missing",
        "bad_uuid",
        "multiple_pages",
        "boolean_slots",
        "owner_pending",
        "business_state",
        "owner_authority",
        "extra_effect",
        "duplicate_effect",
        "wildcard_path",
        "symlink",
    ],
)
def test_recovery_rejects_detached_or_expanded_effect_declarations(tmp_path, fault):
    state, declaration, registration, folder = _bounded_documents(tmp_path)
    recovery = state["execution_control"]["recovery"]
    if fault == "stage":
        state["all_projects_governance"]["stage"] = "A"
    elif fault == "activation":
        recovery["activation_ref"] = "historical-file:auto-resume"
    elif fault == "contract_missing":
        (folder / "contract.md").unlink()
    elif fault == "contract_changed":
        (folder / "contract.md").write_text("Changed contract")
    elif fault == "unaccepted_plan":
        registration["accepted_execution_plan"]["governor"] = "pending"
    elif fault == "target_changed":
        recovery["target"]["image_id"] = "00000000-0000-4000-8000-000000000009"
    elif fault == "identity_missing":
        del recovery["target"]["run_id"]
        registration["target"] = copy.deepcopy(recovery["target"])
    elif fault == "bad_uuid":
        recovery["target"]["generation_id"] = "not-a-uuid"
        registration["target"] = copy.deepcopy(recovery["target"])
    elif fault in {"multiple_pages", "boolean_slots"}:
        recovery["limits"]["page_slots"] = 2 if fault == "multiple_pages" else True
        registration["limits"] = copy.deepcopy(recovery["limits"])
    elif fault == "owner_pending":
        recovery["target"]["page"] = state["catalog_observation"]["pending_pages"][0]
        registration["target"] = copy.deepcopy(recovery["target"])
    elif fault == "business_state":
        state["execution_control"]["state"] = "PROCESSING"
        state["current_work"]["status"] = "PROCESSING"
    elif fault == "owner_authority":
        state["execution_control"]["authority"] = "agent-final-review"
    elif fault == "extra_effect":
        recovery["effects"].append("approve")
    elif fault == "duplicate_effect":
        recovery["effects"][-1] = recovery["effects"][0]
    elif fault == "wildcard_path":
        recovery["contract"] = "../contract.md"
        state["all_projects_governance"]["contract"] = recovery["contract"]
    else:
        content = (folder / "contract.md").read_bytes()
        (folder / "contract.md").unlink()
        outside = tmp_path / "outside.md"
        outside.write_bytes(content)
        (folder / "contract.md").symlink_to(outside)
    (folder / "candidate.json").write_text(json.dumps(registration))
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)
