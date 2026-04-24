from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any
from typing import Literal


BackendKind = Literal["local", "ssh", "slurm"]


@dataclass(frozen=True)
class InventoryDefaults:
    password_env: str | None = None
    poll_seconds: int = 20


@dataclass(frozen=True)
class HostPolicy:
    host: str
    max_active_slots: int | None = None
    max_active_fraction: float | None = None


@dataclass(frozen=True)
class SlotSpec:
    name: str
    backend: BackendKind
    host: str | None = None
    gpu: int | None = None
    node: str | None = None
    workdir: str | None = None
    run_root: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    password_env: str | None = None
    partition: str | None = None
    gres: str | None = None
    cpus_per_task: int | None = None
    time_limit: str | None = None
    output_pattern: str | None = None
    ssh_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobSpec:
    name: str
    command: tuple[str, ...]
    shell: bool = False
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    slots: tuple[str, ...] = ()
    backends: tuple[BackendKind, ...] = ()
    required_tags: tuple[str, ...] = ()
    retries: int = 0
    requirements: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingJob:
    spec: JobSpec
    attempts: int = 0


@dataclass
class ActiveRun:
    slot: SlotSpec
    job: JobSpec
    attempt: int
    started_at: float
    log_path: str
    status_path: str
    process: subprocess.Popen[str] | None = None
    job_id: str | None = None
    session_name: str | None = None
