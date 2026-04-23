from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import backends
from .models import HostPolicy, JobSpec, PendingJob, SlotSpec
from .state import append_event


@dataclass(frozen=True)
class SchedulerConfig:
    run_dir: Path
    poll_seconds: int = 20
    deadline_hours: float | None = None
    dry_run: bool = False
    default_password_env: str | None = None
    host_policies: dict[str, HostPolicy] = field(default_factory=dict)


def job_matches_slot(job: JobSpec, slot: SlotSpec) -> bool:
    if job.slots and slot.name not in job.slots:
        return False
    if job.backends and slot.backend not in job.backends:
        return False
    if job.required_tags and not set(job.required_tags).issubset(set(slot.tags)):
        return False
    return True


def pop_next_compatible_job(queue: deque[PendingJob], slot: SlotSpec) -> PendingJob | None:
    for _ in range(len(queue)):
        item = queue.popleft()
        if job_matches_slot(item.spec, slot):
            return item
        queue.append(item)
    return None


def _resolve_password(slot: SlotSpec, default_password_env: str | None) -> str | None:
    env_name = slot.password_env or default_password_env
    if not env_name:
        return None
    return os.environ.get(env_name)


def _count_slots_per_host(slots: list[SlotSpec]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for slot in slots:
        if not slot.host:
            continue
        counts[slot.host] = counts.get(slot.host, 0) + 1
    return counts


def _count_active_slots_per_host(active: dict[str, backends.ActiveRun]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in active.values():
        if not run.slot.host:
            continue
        counts[run.slot.host] = counts.get(run.slot.host, 0) + 1
    return counts


def _host_slot_limit(policy: HostPolicy, total_slots: int) -> int:
    limits: list[int] = []
    if policy.max_active_slots is not None:
        limits.append(policy.max_active_slots)
    if policy.max_active_fraction is not None:
        fraction_limit = max(1, int(total_slots * policy.max_active_fraction))
        limits.append(fraction_limit)
    if not limits:
        return total_slots
    return min(limits)


def slot_is_available(
    slot: SlotSpec,
    occupied_slots_per_host: dict[str, int],
    slots_per_host: dict[str, int],
    host_policies: dict[str, HostPolicy],
) -> bool:
    if not slot.host:
        return True
    policy = host_policies.get(slot.host)
    if policy is None:
        return True
    total_slots = slots_per_host.get(slot.host, 1)
    limit = _host_slot_limit(policy, total_slots)
    return occupied_slots_per_host.get(slot.host, 0) < limit


def run_scheduler(slots: list[SlotSpec], jobs: list[JobSpec], config: SchedulerConfig) -> Path:
    run_dir = config.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.jsonl"
    queue: deque[PendingJob] = deque(PendingJob(spec=job) for job in jobs)
    active: dict[str, backends.ActiveRun] = {}
    slots_per_host = _count_slots_per_host(slots)
    start_time = datetime.now()
    deadline = start_time + timedelta(hours=config.deadline_hours) if config.deadline_hours is not None else None

    append_event(
        state_path,
        {
            "event": "start",
            "time": start_time.isoformat(),
            "run_dir": str(run_dir),
            "poll_seconds": config.poll_seconds,
            "dry_run": config.dry_run,
            "deadline": deadline.isoformat() if deadline else None,
            "slot_count": len(slots),
            "job_count": len(jobs),
        },
    )

    deadline_logged = False

    while queue or active:
        launched_any = False
        for slot_name, run in list(active.items()):
            password = _resolve_password(run.slot, config.default_password_env)
            if backends.is_alive(run, password):
                continue
            exit_code = backends.read_exit_code(run, password)
            if exit_code == 0:
                result = "succeeded"
            elif exit_code is None:
                result = "unknown"
            else:
                result = "failed"

            append_event(
                state_path,
                {
                    "event": "finished",
                    "time": datetime.now().isoformat(),
                    "slot": run.slot.name,
                    "backend": run.slot.backend,
                    "job": run.job.name,
                    "attempt": run.attempt,
                    "log_path": run.log_path,
                    "status_path": run.status_path,
                    "job_id": run.job_id,
                    "session_name": run.session_name,
                    "exit_code": exit_code,
                    "result": result,
                    "elapsed_sec": round(time.time() - run.started_at, 1),
                },
            )
            active.pop(slot_name, None)
            if result == "failed" and run.attempt <= run.job.retries:
                queue.append(PendingJob(spec=run.job, attempts=run.attempt))
                append_event(
                    state_path,
                    {
                        "event": "requeued",
                        "time": datetime.now().isoformat(),
                        "job": run.job.name,
                        "previous_attempt": run.attempt,
                        "next_attempt": run.attempt + 1,
                    },
                )

        can_launch = deadline is None or datetime.now() < deadline
        if not can_launch and not deadline_logged:
            append_event(
                state_path,
                {
                    "event": "deadline_reached",
                    "time": datetime.now().isoformat(),
                    "remaining_queue": len(queue),
                },
            )
            deadline_logged = True

        if can_launch:
            occupied_slots_per_host = _count_active_slots_per_host(active)
            for slot in slots:
                if slot.name in active:
                    continue
                if not slot_is_available(slot, occupied_slots_per_host, slots_per_host, config.host_policies):
                    continue
                pending = pop_next_compatible_job(queue, slot)
                if pending is None:
                    continue
                attempt = pending.attempts + 1
                password = _resolve_password(slot, config.default_password_env)
                launched = backends.launch(slot, pending.spec, attempt, run_dir, password, config.dry_run)
                append_event(
                    state_path,
                    {
                        "event": "launched",
                        "time": datetime.now().isoformat(),
                        "slot": slot.name,
                        "backend": slot.backend,
                        "job": pending.spec.name,
                        "attempt": attempt,
                        "log_path": launched.log_path,
                        "status_path": launched.status_path,
                        "job_id": launched.job_id,
                        "session_name": launched.session_name,
                    },
                )
                if config.dry_run:
                    append_event(
                        state_path,
                        {
                            "event": "finished",
                            "time": datetime.now().isoformat(),
                            "slot": slot.name,
                            "backend": slot.backend,
                            "job": pending.spec.name,
                            "attempt": attempt,
                            "log_path": launched.log_path,
                            "status_path": launched.status_path,
                            "job_id": launched.job_id,
                            "session_name": launched.session_name,
                            "exit_code": 0,
                            "result": "dry_run",
                            "elapsed_sec": 0.0,
                        },
                    )
                else:
                    active[slot.name] = launched
                if slot.host:
                    occupied_slots_per_host[slot.host] = occupied_slots_per_host.get(slot.host, 0) + 1
                launched_any = True

        if not active and queue and can_launch and not launched_any:
            blocked = sorted(pending.spec.name for pending in queue)
            append_event(
                state_path,
                {
                    "event": "blocked",
                    "time": datetime.now().isoformat(),
                    "remaining_queue": len(queue),
                    "jobs": blocked,
                    "reason": "no compatible or currently available slots for remaining jobs",
                },
            )
            break

        if not active and (not queue or not can_launch):
            break
        if not active and queue and can_launch:
            continue
        time.sleep(max(1, config.poll_seconds))

    append_event(
        state_path,
        {
            "event": "end",
            "time": datetime.now().isoformat(),
            "remaining_queue": len(queue),
            "active_slots": sorted(active),
        },
    )
    return state_path
