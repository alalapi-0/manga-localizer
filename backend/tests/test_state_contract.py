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
        "effects": [
            "final_review_refresh",
            "g10_typeset",
            "g4_regions",
            "g4_reopen",
            "g5_classification",
            "g6_source_review",
            "g7_mask",
            "g8_native_generation",
            "g8_native_ingest",
            "g9_bind",
        ],
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


V2_CONTRACT_ID = "ALL-PROJECTS-CODEX-GOVERNANCE-V1-MANGA-PAGE134-CONTROLLED-RECOVERY-v2"
V2_PROFILE = "page134_g7_material_remask_v2"
V2_TARGET = {
    "page": 134,
    "project_id": "b1b85f3e-4d72-4956-b519-6b30fee02fcc",
    "batch_id": "a734c596-faae-4875-ae61-f694a3c26d4a",
    "item_id": "7975117b-c953-4ac4-847e-5249d9521a2c",
    "image_id": "71a0fad1-5fb2-4e14-8bd8-7624f1cd2cc9",
    "generation_id": "809e3762-7d11-40f8-88cf-99f6d9111691",
    "run_id": "final-review-7975117b-r2-a2",
}
V2_EFFECTS = [
    "final_review_refresh",
    "g10_typeset",
    "g7_mask",
    "g8_native_generation",
    "g8_native_ingest",
    "g9_bind",
]
V2_BUDGET = {
    "prior_receipt": {
        "commit": "1cb5a7ae3877c283c88fb1f43727652dee7ea458",
        "path": ("docs/reports/all-projects-governance/manga-review-landing/page134-recovery.json"),
        "semantic_sha256": ("d1c93d67aa28a4b37a8a5bb9ddd5373905b17cb1eccff5ee0cf2c0848c9ade41"),
    },
    "prior": {"generation": 1, "ingest": 1},
    "additional": {"generation": 1, "ingest": 1},
    "cumulative": {"generation": 2, "ingest": 2},
}


def _v2_documents(tmp_path):
    state, declaration, registration, folder = _bounded_documents(tmp_path)
    contract = (
        f"# {V2_CONTRACT_ID}\nInactive synthetic Stage C declaration fixture; no real effects.\n"
    ).encode()
    (folder / "contract.md").write_bytes(contract)
    recovery = state["execution_control"]["recovery"]
    recovery.update(
        {
            "contract_id": V2_CONTRACT_ID,
            "profile": V2_PROFILE,
            "target": copy.deepcopy(V2_TARGET),
            "effect_budget": copy.deepcopy(V2_BUDGET),
            "effects": copy.deepcopy(V2_EFFECTS),
        }
    )
    registration.update(
        {
            "contract_id": V2_CONTRACT_ID,
            "contract_sha256": hashlib.sha256(contract).hexdigest(),
            "profile": V2_PROFILE,
            "target": copy.deepcopy(V2_TARGET),
            "effect_budget": copy.deepcopy(V2_BUDGET),
            "effects": copy.deepcopy(V2_EFFECTS),
        }
    )
    (folder / "candidate.json").write_text(json.dumps(registration))
    return state, declaration, registration, folder


def _write_registration(folder: Path, registration: dict) -> None:
    (folder / "candidate.json").write_text(json.dumps(registration))


def test_v2_g7_only_recovery_is_static_and_never_grants_authority(tmp_path):
    state, declaration, _, _ = _v2_documents(tmp_path)
    result = checker.validate_documents(state, declaration, root=tmp_path)
    assert result["execution_declaration"] == "owner_authorized_single_page_recovery"
    assert result["grants_execution_authority"] is False

    # An inactive state may preserve the full declaration for later review.
    state["execution_control"]["project_execution"] = "not_active_in_this_governance_goal"
    state["all_projects_governance"]["stage"] = "A"
    result = checker.validate_documents(state, declaration, root=tmp_path)
    assert state["execution_control"]["recovery"]["profile"] == V2_PROFILE
    assert result["execution_declaration"] == "not_active_in_this_governance_goal"
    assert result["grants_execution_authority"] is False


@pytest.mark.parametrize(
    "fault",
    ["unknown_profile", "registration_profile", "v2_without_profile", "wrong_contract"],
)
def test_v2_rejects_missing_unknown_or_detached_profile(tmp_path, fault):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    recovery = state["execution_control"]["recovery"]
    if fault == "unknown_profile":
        recovery["profile"] = registration["profile"] = "page134_g7_remask_v3"
    elif fault == "registration_profile":
        registration["profile"] = "page134_g7_remask_v3"
    elif fault == "v2_without_profile":
        del recovery["profile"]
        del registration["profile"]
        del recovery["effect_budget"]
        del registration["effect_budget"]
        recovery["effects"] = registration["effects"] = [
            "final_review_refresh",
            "g10_typeset",
            "g4_regions",
            "g4_reopen",
            "g5_classification",
            "g6_source_review",
            "g7_mask",
            "g8_native_generation",
            "g8_native_ingest",
            "g9_bind",
        ]
    else:
        recovery["contract_id"] = registration["contract_id"] = (
            "ALL-PROJECTS-CODEX-GOVERNANCE-V1-MANGA-PAGE134-OTHER-v2"
        )
        contract = f"# {recovery['contract_id']}\nDifferent synthetic contract.\n".encode()
        (folder / "contract.md").write_bytes(contract)
        registration["contract_sha256"] = hashlib.sha256(contract).hexdigest()
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("page", 135), ("image_id", "00000000-0000-4000-8000-000000000009")],
)
def test_v2_rejects_a_consistently_registered_but_wrong_full_target(tmp_path, field, value):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    wrong_target = copy.deepcopy(V2_TARGET)
    wrong_target[field] = value
    state["execution_control"]["recovery"]["target"] = wrong_target
    registration["target"] = copy.deepcopy(wrong_target)
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


def test_v2_rejects_contract_bytes_that_do_not_match_registration(tmp_path):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    (folder / "contract.md").write_text(f"# {V2_CONTRACT_ID}\nChanged synthetic contract bytes.\n")
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize("fault", ["g4", "g5", "g6", "missing", "duplicate"])
def test_v2_rejects_legacy_mixed_missing_or_duplicate_effects(tmp_path, fault):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    effects = copy.deepcopy(V2_EFFECTS)
    if fault in {"g4", "g5", "g6"}:
        effects[2] = {
            "g4": "g4_reopen",
            "g5": "g5_classification",
            "g6": "g6_source_review",
        }[fault]
    elif fault == "missing":
        effects.pop()
    else:
        effects[-1] = effects[0]
    state["execution_control"]["recovery"]["effects"] = effects
    registration["effects"] = copy.deepcopy(effects)
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize(
    ("section", "field", "bad_value"),
    [
        (section, field, bad_value)
        for section in ("prior", "additional", "cumulative")
        for field in ("generation", "ingest")
        for bad_value in (
            True,
            2.0 if section == "cumulative" else 1.0,
            "2" if section == "cumulative" else "1",
            -1,
        )
    ],
)
def test_v2_rejects_every_non_exact_or_non_integer_effect_count(
    tmp_path, section, field, bad_value
):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    state["execution_control"]["recovery"]["effect_budget"][section][field] = bad_value
    registration["effect_budget"][section][field] = bad_value
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize(
    ("section", "field", "bad_value"),
    [
        ("prior", "generation", 0),
        ("prior", "generation", 2),
        ("additional", "ingest", 2),
        ("cumulative", "generation", 3),
        ("cumulative", "ingest", 1),
    ],
)
def test_v2_rejects_reset_exhausted_or_inconsistent_effect_counts(
    tmp_path, section, field, bad_value
):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    state["execution_control"]["recovery"]["effect_budget"][section][field] = bad_value
    registration["effect_budget"][section][field] = bad_value
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize(
    "fault", ["missing_budget", "extra_budget", "missing_count", "extra_count", "detached"]
)
def test_v2_rejects_missing_extra_or_detached_budget_fields(tmp_path, fault):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    budget = state["execution_control"]["recovery"]["effect_budget"]
    if fault == "missing_budget":
        del budget["additional"]
        registration["effect_budget"] = copy.deepcopy(budget)
    elif fault == "extra_budget":
        budget["runtime_counter"] = {"generation": 0, "ingest": 0}
        registration["effect_budget"] = copy.deepcopy(budget)
    elif fault == "missing_count":
        del budget["prior"]["ingest"]
        registration["effect_budget"] = copy.deepcopy(budget)
    elif fault == "extra_count":
        budget["cumulative"]["other"] = 0
        registration["effect_budget"] = copy.deepcopy(budget)
    else:
        registration["effect_budget"]["additional"]["generation"] = 2
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("commit", "0" * 40),
        ("commit", 123),
        ("path", "docs/reports/all-projects-governance/other.json"),
        ("path", False),
        ("semantic_sha256", "0" * 64),
        ("semantic_sha256", 123),
    ],
)
def test_v2_rejects_wrong_or_non_string_prior_receipt_fields(tmp_path, field, bad_value):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    receipt = state["execution_control"]["recovery"]["effect_budget"]["prior_receipt"]
    receipt[field] = bad_value
    registration["effect_budget"]["prior_receipt"][field] = bad_value
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("page_slots", 2),
        ("native_generation_limit", 2),
        ("g8_ingest_concurrency", 2),
        ("automatic_owner_approval", True),
        ("export_allowed", True),
        ("background_worker_allowed", True),
    ],
)
def test_v2_retains_all_six_limits_and_does_not_treat_concurrency_as_budget(
    tmp_path, field, bad_value
):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    limits = state["execution_control"]["recovery"]["limits"]
    limits[field] = bad_value
    registration["limits"] = copy.deepcopy(limits)
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


@pytest.mark.parametrize("fault", ["registration_count_type", "registration_limit_type"])
def test_v2_rejects_registration_only_numeric_type_corruption(tmp_path, fault):
    state, declaration, registration, folder = _v2_documents(tmp_path)
    if fault == "registration_count_type":
        registration["effect_budget"]["prior"]["generation"] = True
    else:
        registration["limits"]["g8_ingest_concurrency"] = 1.0
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)


def test_legacy_declaration_cannot_silently_select_the_six_effect_chain(tmp_path):
    state, declaration, registration, folder = _bounded_documents(tmp_path)
    state["execution_control"]["recovery"]["effects"] = copy.deepcopy(V2_EFFECTS)
    registration["effects"] = copy.deepcopy(V2_EFFECTS)
    _write_registration(folder, registration)
    with pytest.raises(checker.StateError):
        checker.validate_documents(state, declaration, root=tmp_path)
