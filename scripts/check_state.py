"""Read-only Manga state/declaration checks; never starts the application or Hub."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from uuid import UUID

import yaml

ROOT = Path(__file__).resolve().parents[1]
INACTIVE_EXECUTION = "not_active_in_this_governance_goal"
BOUNDED_EXECUTION = "owner_authorized_single_page_recovery"
RECOVERY_EFFECTS = {
    "g4_reopen",
    "g4_regions",
    "g5_classification",
    "g6_source_review",
    "g7_mask",
    "g8_native_generation",
    "g8_native_ingest",
    "g9_bind",
    "g10_typeset",
    "final_review_refresh",
}


class StateError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise StateError("State/declaration has a duplicate or non-string key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_document(data: bytes) -> dict:
    if len(data) > 65536:
        raise StateError("Current state/declaration is not a compact document")
    if any(
        isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
        for t in yaml.scan(data)
    ):
        raise StateError("State/declaration aliases and anchors are forbidden")
    result = yaml.load(data, Loader=UniqueLoader)
    if not isinstance(result, dict):
        raise StateError("State/declaration must be a YAML mapping")
    return result


def _bound_document(root: Path, relative: object) -> bytes:
    if not isinstance(relative, str):
        raise StateError("Recovery document reference is missing")
    path = Path(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.parts[:3] != ("docs", "reports", "all-projects-governance")
    ):
        raise StateError(
            "Recovery documents must remain in the project evidence directory"
        )
    target = root / path
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise StateError("Recovery document routes cannot use symlinks")
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        raise StateError("Recovery document reference is unavailable")
    raw = target.read_bytes()
    if not raw or len(raw) > 65536:
        raise StateError("Recovery document must be nonempty and compact")
    return raw


def _validate_execution(state: dict, root: Path) -> str:
    control = state["execution_control"]
    mode = control["project_execution"]
    if mode == INACTIVE_EXECUTION:
        return mode
    if mode != BOUNDED_EXECUTION:
        raise StateError("Unknown project execution declaration")
    if (
        control.get("state") != "OWNER_R2_REVIEW"
        or control.get("authority") != "owner-r2-review"
        or state["current_work"].get("status") != "OWNER_R2_REVIEW"
        or state["current_work"].get("round") != "owner-r2-review"
    ):
        raise StateError("Bounded recovery must retain the owner final-review state")
    recovery = control.get("recovery")
    unit = state.get("all_projects_governance", {})
    if not isinstance(recovery, dict) or unit.get("stage") != "C":
        raise StateError("Bounded recovery requires the current real-effect stage")
    activation = recovery.get("activation_ref")
    if not isinstance(activation, str) or not re.fullmatch(
        r"owner-(?:goal|turn):[^\s]+", activation
    ):
        raise StateError(
            "Bounded recovery requires an explicit owner activation reference"
        )
    if recovery.get("contract") != unit.get("contract") or recovery.get(
        "registration"
    ) != unit.get("candidate_manifest"):
        raise StateError("Recovery must bind the current contract and registration")
    contract = _bound_document(root, recovery.get("contract"))
    registration = load_document(_bound_document(root, recovery.get("registration")))
    contract_id = recovery.get("contract_id")
    if (
        not isinstance(contract_id, str)
        or not contract_id
        or registration.get("contract_id") != contract_id
        or registration.get("contract") != recovery["contract"]
        or registration.get("contract_sha256") != hashlib.sha256(contract).hexdigest()
        or contract_id not in contract.decode("utf-8")
        or registration.get("stage") != "C"
        or registration.get("activation_ref") != activation
    ):
        raise StateError("Recovery contract identity is detached from its registration")
    plan = registration.get("accepted_execution_plan", {})
    if (
        not isinstance(plan, dict)
        or plan.get("judge") != "PASS"
        or plan.get("governor") != "APPROVE"
        or not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("semantic_sha256", "")))
    ):
        raise StateError("The current execution plan lacks registered acceptance")
    target = recovery.get("target")
    if not isinstance(target, dict) or target != registration.get("target"):
        raise StateError("Recovery must bind one exact registered target")
    if set(target) != {
        "page",
        "project_id",
        "batch_id",
        "item_id",
        "image_id",
        "generation_id",
        "run_id",
    }:
        raise StateError("Recovery target identity is incomplete")
    if (
        type(target["page"]) is not int
        or not 1 <= target["page"] <= state["progress"]["total"]
    ):
        raise StateError("Recovery page is outside the observed corpus")
    for key in ("project_id", "batch_id", "item_id", "image_id", "generation_id"):
        value = target[key]
        try:
            valid = isinstance(value, str) and str(UUID(value)) == value
        except ValueError:
            valid = False
        if not valid:
            raise StateError("Recovery identity must use canonical UUIDs")
    if (
        target["batch_id"] != state["catalog_observation"]["batch_id"]
        or not isinstance(target["run_id"], str)
        or not target["run_id"].strip()
        or target["page"] in state["catalog_observation"].get("pending_pages", [])
    ):
        raise StateError(
            "Recovery cannot target a detached batch or owner-pending page"
        )
    limits = recovery.get("limits", {})
    required_limits = {
        "page_slots": 1,
        "native_generation_limit": 1,
        "g8_ingest_concurrency": 1,
        "automatic_owner_approval": False,
        "export_allowed": False,
        "background_worker_allowed": False,
    }
    if (
        not isinstance(limits, dict)
        or limits != required_limits
        or any(
            type(limits[k]) is not type(value) for k, value in required_limits.items()
        )
    ):
        raise StateError("Recovery must retain the exact single-page effect limits")
    effects = recovery.get("effects")
    if (
        not isinstance(effects, list)
        or not all(isinstance(effect, str) for effect in effects)
        or len(effects) != len(RECOVERY_EFFECTS)
        or set(effects) != RECOVERY_EFFECTS
    ):
        raise StateError("Recovery effects must match the bounded gate chain")
    if registration.get("limits") != limits or registration.get("effects") != effects:
        raise StateError(
            "Recovery limits/effects differ from the accepted registration"
        )
    return mode


def validate_documents(state: dict, declaration: dict, *, root: Path = ROOT) -> dict:
    if state["metadata"]["sole_source"] != ".agent/STATE.yaml":
        raise StateError("The sole state path changed")
    if (
        state["metadata"]["format"] != "yaml"
        or state["project"]["id"] != "manga-localizer"
    ):
        raise StateError("State identity/format is inconsistent")
    control = state["execution_control"]
    execution_mode = _validate_execution(state, root)
    if state["current_work"]["status"] != control["state"]:
        raise StateError("Current business and execution states disagree")
    observation, progress = state["catalog_observation"], state["progress"]
    counts = [observation[k] for k in ("approved", "issues", "pending")]
    if any(type(n) is not int or n < 0 for n in counts):
        raise StateError("Catalog counts must be nonnegative integers")
    if (
        any(type(progress[key]) is not int for key in ("completed", "total"))
        or progress["completed"] != counts[0]
        or progress["total"] != sum(counts)
    ):
        raise StateError("Progress must retain the owner-approved counting basis")
    if (counts[1] or counts[2]) and (
        state["current_work"]["completed"] is not False
        or state["current_work"]["accepted"] is not False
        or not state["blockers"]
    ):
        raise StateError(
            "Unresolved owner reviews cannot be reported complete/accepted"
        )
    expected_source = [
        {
            "id": "state",
            "path": ".agent/STATE.yaml",
            "format": "yaml",
            "role": "current_state",
        }
    ]
    if (
        declaration["source_refs"] != expected_source
        or declaration["project_id"] != "manga-localizer"
    ):
        raise StateError("Hub declaration must read only the sole current YAML state")
    fields = {"blockers": state["blockers"]}
    for group in ("current_work", "progress", "verification", "delivery"):
        fields.update(
            {f"{group}.{name}": value for name, value in state[group].items()}
        )
    mapping, unknown = declaration["mapping"], declaration["unknown_fields"]
    if not set(mapping) <= set(fields) or set(unknown) != set(fields):
        raise StateError(
            "Every business field needs a fallback reason, including mapped fields"
        )
    if any(
        type(reason) is not str or not reason.strip() for reason in unknown.values()
    ):
        raise StateError("Unknown fallback reasons must be nonempty text")
    lifecycle_maps = {
        "current_work.status": {"OWNER_R2_REVIEW": "blocked"},
        "delivery.status": {
            "prior_code_main_verified_new_unit_in_progress": "pending_delivery",
            "main_verified": "delivered",
        },
    }
    for name, value in fields.items():
        if value is None:
            if name not in unknown or not unknown[name]:
                raise StateError("Null state facts need an explicit unknown reason")
        elif mapping.get(name) != {
            "source_ref": "state",
            "selector": {"path": name.split(".")},
            "value_map": lifecycle_maps.get(name),
        }:
            raise StateError(
                "Dynamic business facts must be read directly from the sole state"
            )
    return {
        "project_id": "manga-localizer",
        "fields": len(fields),
        "mapped": len(mapping),
        "unknown": sum(value is None for value in fields.values()),
        "fallback_reasons": len(unknown),
        "progress": progress["completed"],
        "total": progress["total"],
        "execution_declaration": execution_mode,
        "grants_execution_authority": False,
    }


def main() -> int:
    try:
        if (ROOT / ".agent/STATE.md").exists():
            raise StateError(
                "The retired Markdown state must not remain a second authority"
            )
        state = load_document((ROOT / ".agent/STATE.yaml").read_bytes())
        declaration = load_document((ROOT / "hub.connection.yaml").read_bytes())
        result = validate_documents(state, declaration)
        history = state["history"]
        commit = history["state_checkpoint_commit"]
        if (
            not re.fullmatch(r"[0-9a-f]{40}", commit)
            or history["path"] != ".agent/STATE.md"
        ):
            raise StateError("Historical checkpoint identity is invalid")
        preserved = subprocess.check_output(
            ["git", "show", f"{commit}:.agent/STATE.md"],
            cwd=ROOT,
            stderr=subprocess.PIPE,
        )
        if hashlib.sha256(preserved).hexdigest() != history["sha256"]:
            raise StateError(
                "Historical state bytes are not preserved by the checkpoint"
            )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        yaml.YAMLError,
        subprocess.CalledProcessError,
    ) as error:
        print(json.dumps({"status": "invalid", "error_type": type(error).__name__}))
        return 1
    print(json.dumps({"status": "valid", "history_preserved": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
