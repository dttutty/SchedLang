from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import BackendKind, HostPolicy, InventoryDefaults, JobSpec, SlotSpec


VALID_BACKENDS = {"local", "ssh", "slurm"}


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _string_map(value: Any, label: str) -> dict[str, str]:
    data = _require_mapping(value, label)
    return {str(key): str(item) for key, item in data.items()}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    return dict(_require_mapping(value, label))


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    items = _require_list(value, label)
    return tuple(str(item) for item in items)


def _normalize_backend(value: Any, label: str) -> BackendKind:
    text = str(value)
    if text not in VALID_BACKENDS:
        raise ValueError(f"{label} must be one of {sorted(VALID_BACKENDS)}")
    return text  # type: ignore[return-value]


def _normalize_command(value: Any) -> tuple[tuple[str, ...], bool]:
    if isinstance(value, str):
        return (value,), True
    if isinstance(value, list):
        return tuple(str(item) for item in value), False
    raise ValueError("job command must be a string or list of strings")


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _require_mapping(data, f"{path}")


def _load_host_policies(data: dict[str, Any]) -> dict[str, HostPolicy]:
    policies: dict[str, HostPolicy] = {}
    for item in _require_list(data.get("host_policies"), "host_policies"):
        policy_data = _require_mapping(item, "host_policy")
        host = str(policy_data["host"])
        max_active_slots = int(policy_data["max_active_slots"]) if "max_active_slots" in policy_data else None
        max_active_fraction = float(policy_data["max_active_fraction"]) if "max_active_fraction" in policy_data else None

        if max_active_slots is None and max_active_fraction is None:
            raise ValueError(f"host policy for {host} must set max_active_slots or max_active_fraction")
        if max_active_slots is not None and max_active_slots < 1:
            raise ValueError(f"host policy for {host} must set max_active_slots >= 1")
        if max_active_fraction is not None and not 0.0 < max_active_fraction <= 1.0:
            raise ValueError(f"host policy for {host} must set 0 < max_active_fraction <= 1")
        if host in policies:
            raise ValueError(f"duplicate host policy for {host}")

        policies[host] = HostPolicy(
            host=host,
            max_active_slots=max_active_slots,
            max_active_fraction=max_active_fraction,
        )
    return policies


def load_inventory(path: Path) -> tuple[InventoryDefaults, list[SlotSpec], dict[str, HostPolicy]]:
    data = _load_yaml(path)
    defaults_data = _require_mapping(data.get("defaults"), "defaults")
    defaults = InventoryDefaults(
        password_env=str(defaults_data["password_env"]) if "password_env" in defaults_data else None,
        poll_seconds=int(defaults_data.get("poll_seconds", 20)),
    )
    host_policies = _load_host_policies(data)

    slots: list[SlotSpec] = []
    for item in _require_list(data.get("slots"), "slots"):
        slot_data = _require_mapping(item, "slot")
        backend = _normalize_backend(slot_data.get("backend"), "slot.backend")
        host = slot_data.get("host")
        node = slot_data.get("node")
        if backend == "ssh" and not host:
            raise ValueError("ssh slots require host")

        slots.append(
            SlotSpec(
                name=str(slot_data["name"]),
                backend=backend,
                host=str(host) if host is not None else None,
                gpu=int(slot_data["gpu"]) if "gpu" in slot_data and slot_data["gpu"] is not None else None,
                node=str(node) if node is not None else None,
                workdir=str(slot_data["workdir"]) if "workdir" in slot_data else None,
                run_root=str(slot_data["run_root"]) if "run_root" in slot_data else None,
                env=_string_map(slot_data.get("env"), "slot.env"),
                tags=_string_tuple(slot_data.get("tags"), "slot.tags"),
                password_env=str(slot_data["password_env"]) if "password_env" in slot_data else None,
                partition=str(slot_data["partition"]) if "partition" in slot_data else None,
                gres=str(slot_data["gres"]) if "gres" in slot_data else None,
                cpus_per_task=int(slot_data["cpus_per_task"]) if "cpus_per_task" in slot_data else None,
                time_limit=str(slot_data["time_limit"]) if "time_limit" in slot_data else None,
                output_pattern=str(slot_data["output_pattern"]) if "output_pattern" in slot_data else None,
                ssh_options=_string_tuple(slot_data.get("ssh_options"), "slot.ssh_options"),
            )
        )
    return defaults, slots, host_policies


def load_jobs(path: Path) -> list[JobSpec]:
    data = _load_yaml(path)
    jobs: list[JobSpec] = []
    for item in _require_list(data.get("jobs"), "jobs"):
        job_data = _require_mapping(item, "job")
        command, shell = _normalize_command(job_data.get("command"))
        backend_values = _string_tuple(job_data.get("backends"), "job.backends")
        backends = tuple(_normalize_backend(value, "job.backends") for value in backend_values)
        jobs.append(
            JobSpec(
                name=str(job_data["name"]),
                command=command,
                shell=shell,
                cwd=str(job_data["cwd"]) if "cwd" in job_data else None,
                env=_string_map(job_data.get("env"), "job.env"),
                slots=_string_tuple(job_data.get("slots"), "job.slots"),
                backends=backends,
                required_tags=_string_tuple(job_data.get("required_tags"), "job.required_tags"),
                retries=int(job_data.get("retries", 0)),
                requirements=_mapping(job_data.get("requirements"), "job.requirements"),
                preferences=_mapping(job_data.get("preferences"), "job.preferences"),
            )
        )
    return jobs
