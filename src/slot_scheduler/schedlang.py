from __future__ import annotations

import ast
import itertools
import re
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

import yaml


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
BLOCK_HEADER_RE = re.compile(r"^(pool|policy|experiment)\s+([A-Za-z_][A-Za-z0-9_-]*)\s*\{$")
NESTED_BLOCK_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*\{$")
ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)$")


@dataclass(frozen=True)
class PoolSpec:
    name: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class PolicySpec:
    name: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class SchedlangDocument:
    pools: dict[str, PoolSpec] = field(default_factory=dict)
    policies: list[PolicySpec] = field(default_factory=list)
    experiments: list[ExperimentSpec] = field(default_factory=list)


class ParseError(ValueError):
    pass


class _LineParser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.index = 0

    def _clean_line(self, raw: str) -> str:
        stripped = raw.strip()
        if stripped.startswith("#"):
            return ""
        return stripped

    def next_nonempty(self) -> tuple[int, str] | None:
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            self.index += 1
            cleaned = self._clean_line(raw)
            if cleaned:
                return self.index, cleaned
        return None

    def peek_nonempty(self) -> tuple[int, str] | None:
        saved = self.index
        item = self.next_nonempty()
        self.index = saved
        return item

    def expect_nonempty(self) -> tuple[int, str]:
        item = self.next_nonempty()
        if item is None:
            raise ParseError("unexpected end of file")
        return item


def _parse_literal(raw: str, line_no: int) -> Any:
    try:
        return ast.literal_eval(raw)
    except (SyntaxError, ValueError) as exc:
        raise ParseError(f"line {line_no}: invalid literal {raw!r}") from exc


def _parse_value(parser: _LineParser, first_fragment: str, line_no: int) -> Any:
    fragment = first_fragment.strip()
    if fragment.startswith('"""'):
        rest = fragment[3:]
        if rest.endswith('"""'):
            return textwrap.dedent(rest[:-3]).strip("\n")
        parts = [rest]
        while True:
            item = parser.expect_nonempty()
            current_line_no, current = item
            if current.endswith('"""'):
                parts.append(current[:-3])
                return textwrap.dedent("\n".join(parts)).strip("\n")
            parts.append(current)
    return _parse_literal(fragment, line_no)


def _parse_mapping_block(parser: _LineParser) -> dict[str, Any]:
    values: dict[str, Any] = {}
    while True:
        item = parser.expect_nonempty()
        line_no, line = item
        if line == "}":
            return values

        nested_match = NESTED_BLOCK_RE.match(line)
        if nested_match:
            key = nested_match.group(1)
            values[key] = _parse_mapping_block(parser)
            continue

        assignment_match = ASSIGNMENT_RE.match(line)
        if assignment_match:
            key = assignment_match.group(1)
            raw_value = assignment_match.group(2)
            values[key] = _parse_value(parser, raw_value, line_no)
            continue

        raise ParseError(f"line {line_no}: expected assignment or nested block, got {line!r}")


def parse_schedlang(text: str) -> SchedlangDocument:
    parser = _LineParser(text)
    pools: dict[str, PoolSpec] = {}
    policies: list[PolicySpec] = []
    experiments: list[ExperimentSpec] = []

    while True:
        item = parser.next_nonempty()
        if item is None:
            break
        line_no, line = item
        match = BLOCK_HEADER_RE.match(line)
        if not match:
            raise ParseError(f"line {line_no}: expected pool/policy/experiment block header, got {line!r}")
        block_kind, name = match.groups()
        fields = _parse_mapping_block(parser)
        if block_kind == "pool":
            if name in pools:
                raise ParseError(f"line {line_no}: duplicate pool {name!r}")
            pools[name] = PoolSpec(name=name, fields=fields)
        elif block_kind == "policy":
            policies.append(PolicySpec(name=name, fields=fields))
        else:
            experiments.append(ExperimentSpec(name=name, fields=fields))

    return SchedlangDocument(pools=pools, policies=policies, experiments=experiments)


def load_schedlang(path: Path) -> SchedlangDocument:
    return parse_schedlang(path.read_text(encoding="utf-8"))


def _ensure_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _ensure_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [str(item) for item in value]


def _ensure_optional_string_list(value: Any, label: str) -> list[str] | None:
    if value is None:
        return None
    return _ensure_string_list(value, label)


def _ensure_string_list_like(value: Any, label: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    return _ensure_string_list(value, label)


def _ensure_int(value: Any, label: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return int(value)


def _ensure_string_mapping(value: Any, label: str) -> dict[str, str]:
    mapping = _ensure_mapping(value, label)
    return {str(key): str(item) for key, item in mapping.items()}


def _slugify(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return text.strip("-") or "item"


def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return Template(value).safe_substitute({key: str(item) for key, item in variables.items()})
    if isinstance(value, list):
        return [_substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {str(key): str(_substitute(item, variables)) for key, item in value.items()}
    return value


def _substitute_typed(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return Template(value).safe_substitute({key: str(item) for key, item in variables.items()})
    if isinstance(value, list):
        return [_substitute_typed(item, variables) for item in value]
    if isinstance(value, dict):
        return {str(key): _substitute_typed(item, variables) for key, item in value.items()}
    return value


def _matrix_rows(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    if not matrix:
        return [{}]
    keys = list(matrix.keys())
    values: list[list[Any]] = []
    for key in keys:
        raw = matrix[key]
        if not isinstance(raw, list):
            raise ValueError(f"experiment.matrix.{key} must be a list")
        values.append(list(raw))
    rows: list[dict[str, Any]] = []
    for combo in itertools.product(*values):
        rows.append({key: value for key, value in zip(keys, combo, strict=True)})
    return rows


def _job_name(experiment_name: str, name_template: str | None, variables: dict[str, Any]) -> str:
    if name_template:
        return str(_substitute(name_template, variables))
    if not variables:
        return experiment_name
    suffix = "_".join(f"{_slugify(key)}-{_slugify(str(value))}" for key, value in variables.items())
    return f"{experiment_name}_{suffix}"


def _merge_mapping_values(base: Any, override: Any, label: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if base is not None:
        merged.update(_ensure_mapping(base, f"{label} (base)"))
    if override is not None:
        merged.update(_ensure_mapping(override, label))
    return merged


def _normalize_requirements(mapping: dict[str, Any], label: str) -> dict[str, Any]:
    aliases = {
        "backend": "backends",
        "host": "hosts",
        "slot": "slots",
        "host_tags": "required_tags",
        "tags": "required_tags",
    }
    list_fields = {"backends", "hosts", "slots", "required_tags"}
    int_fields = {"gpu_count", "gpu_mem_gb", "cpu_count", "ram_gb"}

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in mapping.items():
        key = aliases.get(str(raw_key), str(raw_key))
        field_label = f"{label}.{raw_key}"
        if key in list_fields:
            normalized[key] = _ensure_string_list_like(raw_value, field_label)
        elif key in int_fields:
            normalized[key] = _ensure_int(raw_value, field_label)
        else:
            supported = sorted(list_fields | int_fields | set(aliases))
            raise ValueError(f"{label} has unsupported field {raw_key!r}; supported fields are {supported}")
    return normalized


def _normalize_preferences(mapping: dict[str, Any], label: str) -> dict[str, Any]:
    aliases = {
        "backend": "backends",
        "host": "hosts",
        "slot": "slots",
        "tags": "host_tags",
    }
    list_fields = {"backends", "hosts", "slots", "host_tags", "avoid_host_tags", "preferred_tags"}
    string_fields = {"placement"}

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in mapping.items():
        key = aliases.get(str(raw_key), str(raw_key))
        field_label = f"{label}.{raw_key}"
        if key in list_fields:
            normalized[key] = _ensure_string_list_like(raw_value, field_label)
        elif key in string_fields:
            normalized[key] = str(raw_value)
        else:
            supported = sorted(list_fields | string_fields | set(aliases))
            raise ValueError(f"{label} has unsupported field {raw_key!r}; supported fields are {supported}")
    return normalized


def compile_jobs_document(document: SchedlangDocument) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    for experiment in document.experiments:
        fields = dict(experiment.fields)
        pool_name = fields.pop("use_pool", None)
        pool_fields: dict[str, Any] = {}
        if pool_name is not None:
            pool_spec = document.pools.get(str(pool_name))
            if pool_spec is None:
                raise ValueError(f"experiment {experiment.name!r} references unknown pool {pool_name!r}")
            pool_fields = dict(pool_spec.fields)

        merged_fields = dict(pool_fields)
        merged_fields.update(fields)
        requires_fields = _merge_mapping_values(
            pool_fields.get("requires"),
            fields.get("requires"),
            f"experiment {experiment.name}.requires",
        )
        prefers_fields = _merge_mapping_values(
            pool_fields.get("prefers"),
            fields.get("prefers"),
            f"experiment {experiment.name}.prefers",
        )

        matrix = merged_fields.pop("matrix", {})
        if matrix is None:
            matrix = {}
        matrix_mapping = _ensure_mapping(matrix, f"experiment {experiment.name}.matrix")
        env_mapping = _ensure_string_mapping(merged_fields.pop("env", {}), f"experiment {experiment.name}.env")
        command_value = merged_fields.pop("command", None)
        if command_value is None:
            raise ValueError(f"experiment {experiment.name!r} must define command")
        name_template = merged_fields.pop("name_template", None)
        cwd = merged_fields.pop("cwd", None)
        retries = merged_fields.pop("retries", 0)
        legacy_requirements: dict[str, Any] = {}
        backends = _ensure_optional_string_list(merged_fields.pop("backends", None), f"experiment {experiment.name}.backends")
        if backends:
            legacy_requirements["backends"] = list(backends)
        required_tags = _ensure_optional_string_list(
            merged_fields.pop("required_tags", None),
            f"experiment {experiment.name}.required_tags",
        )
        if required_tags:
            legacy_requirements["required_tags"] = list(required_tags)
        slots = _ensure_optional_string_list(merged_fields.pop("slots", None), f"experiment {experiment.name}.slots")
        if slots:
            legacy_requirements["slots"] = list(slots)
        merged_fields.pop("requires", None)
        merged_fields.pop("prefers", None)
        if merged_fields:
            unknown_keys = ", ".join(sorted(merged_fields))
            raise ValueError(f"experiment {experiment.name!r} has unsupported fields: {unknown_keys}")
        requirements = dict(legacy_requirements)
        requirements.update(_normalize_requirements(requires_fields, f"experiment {experiment.name}.requires"))
        preferences = _normalize_preferences(prefers_fields, f"experiment {experiment.name}.prefers")

        for variables in _matrix_rows(matrix_mapping):
            job: dict[str, Any] = {
                "name": _job_name(experiment.name, str(name_template) if name_template is not None else None, variables),
                "command": _substitute(command_value, variables),
            }
            if env_mapping:
                job["env"] = _substitute(env_mapping, variables)
            if cwd is not None:
                job["cwd"] = _substitute(str(cwd), variables)
            if requirements.get("backends"):
                job["backends"] = list(requirements["backends"])
            if requirements.get("required_tags"):
                job["required_tags"] = list(requirements["required_tags"])
            if requirements.get("slots"):
                job["slots"] = list(requirements["slots"])
            if requirements:
                job["requirements"] = _substitute_typed(requirements, variables)
            if preferences:
                job["preferences"] = _substitute_typed(preferences, variables)
            retries_int = _ensure_int(retries, f"experiment {experiment.name}.retries")
            if retries_int:
                job["retries"] = retries_int
            jobs.append(job)

    return {"jobs": jobs}


def compile_inventory_document(document: SchedlangDocument, base_inventory: dict[str, Any]) -> dict[str, Any]:
    inventory = dict(base_inventory)
    existing_policies: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for raw_policy in inventory.get("host_policies", []) or []:
        if isinstance(raw_policy, dict) and "host" in raw_policy:
            existing_policies[str(raw_policy["host"])] = dict(raw_policy)

    for policy in document.policies:
        fields = dict(policy.fields)
        hosts = _ensure_string_list(fields.pop("hosts", None), f"policy {policy.name}.hosts")
        max_active_slots = fields.pop("max_active_slots", None)
        max_active_fraction = fields.pop("max_active_fraction", None)
        if fields:
            unknown_keys = ", ".join(sorted(fields))
            raise ValueError(f"policy {policy.name!r} has unsupported fields: {unknown_keys}")
        if max_active_slots is None and max_active_fraction is None:
            raise ValueError(f"policy {policy.name!r} must define max_active_slots or max_active_fraction")
        if max_active_slots is not None:
            max_active_slots = _ensure_int(max_active_slots, f"policy {policy.name}.max_active_slots")
        if max_active_fraction is not None and not isinstance(max_active_fraction, (int, float)):
            raise ValueError(f"policy {policy.name}.max_active_fraction must be a number")

        for host in hosts:
            compiled: dict[str, Any] = {"host": host}
            if max_active_slots is not None:
                compiled["max_active_slots"] = max_active_slots
            if max_active_fraction is not None:
                compiled["max_active_fraction"] = float(max_active_fraction)
            existing_policies[host] = compiled

    inventory["host_policies"] = list(existing_policies.values())
    return inventory


def _build_inventory_index(inventory: dict[str, Any]) -> dict[str, Any]:
    raw_slots = inventory.get("slots", []) or []
    if not isinstance(raw_slots, list):
        raise ValueError("inventory.slots must be a list")

    slots: list[dict[str, Any]] = []
    hosts: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for raw_slot in raw_slots:
        slot_mapping = _ensure_mapping(raw_slot, "inventory.slot")
        slot_name = str(slot_mapping["name"])
        backend = str(slot_mapping["backend"])
        host = str(slot_mapping.get("host") or slot_mapping.get("node") or slot_name)
        tags = _ensure_string_list(slot_mapping.get("tags", []), f"inventory.slot {slot_name}.tags")
        gpu = slot_mapping.get("gpu")

        slot_info = {
            "name": slot_name,
            "backend": backend,
            "host": host,
            "tags": tags,
            "gpu": gpu,
        }
        slots.append(slot_info)

        host_info = hosts.setdefault(
            host,
            {
                "name": host,
                "slot_names": [],
                "gpu_slot_names": [],
                "backends": set(),
                "tags": set(),
            },
        )
        host_info["slot_names"].append(slot_name)
        if gpu is not None:
            host_info["gpu_slot_names"].append(slot_name)
        host_info["backends"].add(backend)
        host_info["tags"].update(tags)

    normalized_hosts: list[dict[str, Any]] = []
    for host_info in hosts.values():
        normalized_hosts.append(
            {
                "name": host_info["name"],
                "slot_names": list(host_info["slot_names"]),
                "gpu_slot_names": list(host_info["gpu_slot_names"]),
                "slot_count": len(host_info["slot_names"]),
                "gpu_slot_count": len(host_info["gpu_slot_names"]),
                "backends": sorted(host_info["backends"]),
                "tags": sorted(host_info["tags"]),
            }
        )

    return {"slots": slots, "hosts": normalized_hosts}


def _job_candidates_from_inventory(job: dict[str, Any], inventory: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    candidates = list(inventory["slots"])
    notes: list[str] = []
    requirements = _ensure_mapping(job.get("requirements", {}), f"job {job.get('name')}.requirements")

    slot_names = requirements.get("slots") or job.get("slots") or []
    if slot_names:
        allowed = {str(name) for name in slot_names}
        candidates = [slot for slot in candidates if slot["name"] in allowed]
        notes.append(f"filtered to explicit slots: {sorted(allowed)}")

    backends = requirements.get("backends") or job.get("backends") or []
    if backends:
        allowed = {str(name) for name in backends}
        candidates = [slot for slot in candidates if slot["backend"] in allowed]
        notes.append(f"filtered to backends: {sorted(allowed)}")

    required_tags = requirements.get("required_tags") or job.get("required_tags") or []
    if required_tags:
        allowed = {str(tag) for tag in required_tags}
        candidates = [slot for slot in candidates if allowed.issubset(set(slot["tags"]))]
        notes.append(f"requires slot tags: {sorted(allowed)}")

    hosts = requirements.get("hosts") or []
    if hosts:
        allowed = {str(host) for host in hosts}
        candidates = [slot for slot in candidates if slot["host"] in allowed]
        notes.append(f"filtered to hosts: {sorted(allowed)}")

    return candidates, notes


def _preferred_slots_from_candidates(
    candidates: list[dict[str, Any]],
    preferences: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not candidates or not preferences:
        return candidates, []

    notes: list[str] = []
    current = list(candidates)

    hosts = preferences.get("hosts") or []
    if hosts:
        allowed = {str(host) for host in hosts}
        narrowed = [slot for slot in current if slot["host"] in allowed]
        if narrowed:
            current = narrowed
            notes.append(f"preferred hosts matched: {sorted(allowed)}")

    backends = preferences.get("backends") or []
    if backends:
        allowed = {str(backend) for backend in backends}
        narrowed = [slot for slot in current if slot["backend"] in allowed]
        if narrowed:
            current = narrowed
            notes.append(f"preferred backends matched: {sorted(allowed)}")

    preferred_tags = preferences.get("host_tags") or preferences.get("preferred_tags") or []
    if preferred_tags:
        allowed = {str(tag) for tag in preferred_tags}
        narrowed = [slot for slot in current if allowed.issubset(set(slot["tags"]))]
        if narrowed:
            current = narrowed
            notes.append(f"preferred host tags matched: {sorted(allowed)}")

    avoid_host_tags = preferences.get("avoid_host_tags") or []
    if avoid_host_tags:
        blocked = {str(tag) for tag in avoid_host_tags}
        narrowed = [slot for slot in current if not blocked.intersection(set(slot["tags"]))]
        if narrowed:
            current = narrowed
            notes.append(f"avoided host tags when possible: {sorted(blocked)}")

    return current, notes


def compile_report_document(jobs_payload: dict[str, Any], inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    jobs = [_ensure_mapping(item, "compiled job") for item in jobs_payload.get("jobs", []) or []]
    inventory_index = _build_inventory_index(inventory) if inventory is not None else None

    report_jobs: list[dict[str, Any]] = []
    status_counts: OrderedDict[str, int] = OrderedDict()
    for job in jobs:
        name = str(job["name"])
        requirements = _ensure_mapping(job.get("requirements", {}), f"job {name}.requirements")
        preferences = _ensure_mapping(job.get("preferences", {}), f"job {name}.preferences")
        notes: list[str] = []

        if inventory_index is None:
            status = "inventory_not_provided"
            candidate_slots: list[dict[str, Any]] = []
            preferred_slots: list[dict[str, Any]] = []
            notes.append("no inventory supplied; skipped placement analysis")
        else:
            candidate_slots, candidate_notes = _job_candidates_from_inventory(job, inventory_index)
            notes.extend(candidate_notes)
            preferred_slots, preferred_notes = _preferred_slots_from_candidates(candidate_slots, preferences)
            notes.extend(preferred_notes)

            if not candidate_slots:
                status = "unschedulable"
                notes.append("no slot satisfies the current hard constraints")
            else:
                gpu_count = int(requirements.get("gpu_count", 1))
                if gpu_count > 1:
                    host_counts: OrderedDict[str, int] = OrderedDict()
                    for slot in candidate_slots:
                        host_counts[slot["host"]] = host_counts.get(slot["host"], 0) + 1
                    multi_gpu_hosts = [host for host, count in host_counts.items() if count >= gpu_count]
                    if not multi_gpu_hosts:
                        status = "unschedulable"
                        notes.append(f"requires gpu_count >= {gpu_count}, but no compatible host has enough slots")
                    else:
                        status = "needs_multi_slot_runtime"
                        notes.append(
                            f"compatible hosts exist for gpu_count={gpu_count}: {sorted(multi_gpu_hosts)}, "
                            "but the current runtime launches one slot per job"
                        )
                else:
                    status = "ready"

        candidate_slot_names = [slot["name"] for slot in candidate_slots]
        candidate_hosts = sorted({str(slot["host"]) for slot in candidate_slots})
        preferred_slot_names = [slot["name"] for slot in preferred_slots]
        preferred_hosts = sorted({str(slot["host"]) for slot in preferred_slots})

        report_jobs.append(
            {
                "name": name,
                "requirements": requirements,
                "preferences": preferences,
                "status": status,
                "candidate_slots": candidate_slot_names,
                "candidate_hosts": candidate_hosts,
                "preferred_slots": preferred_slot_names,
                "preferred_hosts": preferred_hosts,
                "notes": notes,
            }
        )
        status_counts[status] = status_counts.get(status, 0) + 1

    summary = {
        "job_count": len(report_jobs),
        "status_counts": dict(status_counts),
        "inventory_provided": inventory is not None,
    }
    return {"summary": summary, "jobs": report_jobs}


def compile_document(document: SchedlangDocument, base_inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    jobs_payload = compile_jobs_document(document)
    inventory_payload = compile_inventory_document(document, base_inventory) if base_inventory is not None else None
    report_payload = compile_report_document(jobs_payload, inventory_payload if inventory_payload is not None else base_inventory)

    payload: dict[str, Any] = {
        "jobs": jobs_payload,
        "report": report_payload,
    }
    if inventory_payload is not None:
        payload["inventory"] = inventory_payload
    return payload


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
