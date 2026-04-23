from __future__ import annotations

from collections import deque
from pathlib import Path

from slot_scheduler.models import JobSpec, PendingJob, SlotSpec
from slot_scheduler.scheduler import SchedulerConfig, job_matches_slot, pop_next_compatible_job, run_scheduler
from slot_scheduler.state import load_events


def test_job_matches_slot_filters_by_backend_and_tags() -> None:
    slot = SlotSpec(name="gpu1-001", backend="slurm", tags=("a100", "leap2"))
    job = JobSpec(
        name="demo",
        command=("echo", "hi"),
        backends=("slurm",),
        required_tags=("a100",),
    )
    assert job_matches_slot(job, slot) is True
    assert job_matches_slot(JobSpec(name="bad", command=("echo",), backends=("ssh",)), slot) is False


def test_pop_next_compatible_job_rotates_queue() -> None:
    slot = SlotSpec(name="local-g0", backend="local", tags=("local",))
    queue = deque(
        [
            PendingJob(JobSpec(name="slurm-only", command=("echo",), backends=("slurm",))),
            PendingJob(JobSpec(name="local-ok", command=("echo",), backends=("local",))),
        ]
    )

    selected = pop_next_compatible_job(queue, slot)

    assert selected is not None
    assert selected.spec.name == "local-ok"
    assert len(queue) == 1
    assert queue[0].spec.name == "slurm-only"


def test_scheduler_reports_blocked_jobs_when_no_slot_matches(tmp_path: Path) -> None:
    slots = [SlotSpec(name="ssh-a", backend="ssh", host="sun")]
    jobs = [JobSpec(name="local-only", command=("echo", "hi"), backends=("local",))]

    state_path = run_scheduler(
        slots,
        jobs,
        SchedulerConfig(run_dir=tmp_path / "run", poll_seconds=1, dry_run=True),
    )

    events = load_events(state_path)
    assert any(event.get("event") == "blocked" for event in events)
