"""Read-only Manga state/declaration checks; never starts the application or Hub."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


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


def validate_documents(state: dict, declaration: dict) -> dict:
    if state["metadata"]["sole_source"] != ".agent/STATE.yaml":
        raise StateError("The sole state path changed")
    if (
        state["metadata"]["format"] != "yaml"
        or state["project"]["id"] != "manga-localizer"
    ):
        raise StateError("State identity/format is inconsistent")
    control = state["execution_control"]
    if control["project_execution"] != "not_active_in_this_governance_goal":
        raise StateError("This maintenance state cannot activate page execution")
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
