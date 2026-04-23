from __future__ import annotations

import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .models import ActiveRun, JobSpec, SlotSpec


DEFAULT_SSH_OPTIONS = ("-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8")


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text).strip("_") or "job"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _job_stem(slot: SlotSpec, job: JobSpec, attempt: int) -> str:
    return f"{_safe_name(slot.name)}_{_safe_name(job.name)}_a{attempt}_{_timestamp()}"


def _merge_env(slot: SlotSpec, job: JobSpec) -> dict[str, str]:
    env = dict(slot.env)
    if slot.gpu is not None and "CUDA_VISIBLE_DEVICES" not in env:
        env["CUDA_VISIBLE_DEVICES"] = str(slot.gpu)
    env.update(job.env)
    return env


def _command_text(job: JobSpec) -> str:
    if job.shell:
        return job.command[0]
    return " ".join(shlex.quote(part) for part in job.command)


def _env_prefix(env: dict[str, str]) -> str:
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())


def _wrap_shell_command(command_text: str, cwd: str | None, env: dict[str, str], log_path: str, status_path: str) -> str:
    steps: list[str] = []
    if cwd:
        steps.append(f"cd {shlex.quote(cwd)}")
    env_prefix = _env_prefix(env)
    body = f"{env_prefix} {command_text}".strip()
    if steps:
        body = " && ".join([*steps, body])
    return (
        f"{body} > {shlex.quote(log_path)} 2>&1; "
        f"rc=$?; "
        f"mkdir -p {shlex.quote(str(Path(status_path).parent))}; "
        f"printf '%s\\n' \"$rc\" > {shlex.quote(status_path)}; "
        f"exit \"$rc\""
    )


def _ssh_command(slot: SlotSpec, password: str | None, remote_cmd: str) -> list[str]:
    cmd: list[str] = []
    if password:
        cmd.extend(["sshpass", "-p", password])
    cmd.append("ssh")
    options = tuple(slot.ssh_options) if slot.ssh_options else DEFAULT_SSH_OPTIONS
    cmd.extend(options)
    if not slot.host:
        raise ValueError(f"ssh slot {slot.name} is missing host")
    cmd.extend([slot.host, f"bash --noprofile --norc -lc {shlex.quote(remote_cmd)}"])
    return cmd


def _local_paths(run_dir: Path, slot: SlotSpec, job: JobSpec, attempt: int) -> tuple[Path, Path]:
    stem = _job_stem(slot, job, attempt)
    log_path = run_dir / "console" / f"{stem}.log"
    status_path = run_dir / "status" / f"{stem}.exitcode"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path, status_path


def _remote_paths(run_dir: Path, slot: SlotSpec, job: JobSpec, attempt: int) -> tuple[Path, Path]:
    base = Path(slot.run_root or f"/tmp/slot-scheduler/{run_dir.name}")
    stem = _job_stem(slot, job, attempt)
    return base / "console" / f"{stem}.log", base / "status" / f"{stem}.exitcode"


def launch(slot: SlotSpec, job: JobSpec, attempt: int, run_dir: Path, password: str | None, dry_run: bool) -> ActiveRun:
    if slot.backend == "local":
        return _launch_local(slot, job, attempt, run_dir, dry_run)
    if slot.backend == "slurm":
        return _launch_slurm(slot, job, attempt, run_dir, dry_run)
    if slot.backend == "ssh":
        return _launch_ssh(slot, job, attempt, run_dir, password, dry_run)
    raise ValueError(f"unsupported backend {slot.backend}")


def is_alive(run: ActiveRun, password: str | None) -> bool:
    if run.slot.backend == "local":
        return bool(run.process) and run.process.poll() is None
    if run.slot.backend == "slurm":
        if not run.job_id:
            return False
        completed = subprocess.run(
            ["squeue", "-h", "-j", run.job_id],
            check=False,
            text=True,
            capture_output=True,
        )
        return bool(completed.stdout.strip())
    if not run.session_name:
        return False
    completed = subprocess.run(
        _ssh_command(run.slot, password, f"tmux has-session -t {shlex.quote(run.session_name)}"),
        check=False,
        text=True,
        capture_output=True,
    )
    return completed.returncode == 0


def read_exit_code(run: ActiveRun, password: str | None) -> int | None:
    if run.slot.backend == "local":
        if run.process is None:
            return 0
        return run.process.poll()
    if run.slot.backend == "slurm":
        path = Path(run.status_path)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    completed = subprocess.run(
        _ssh_command(run.slot, password, f"cat {shlex.quote(run.status_path)} 2>/dev/null || true"),
        check=False,
        text=True,
        capture_output=True,
    )
    text = completed.stdout.strip()
    return int(text) if text else None


def _launch_local(slot: SlotSpec, job: JobSpec, attempt: int, run_dir: Path, dry_run: bool) -> ActiveRun:
    log_path, status_path = _local_paths(run_dir, slot, job, attempt)
    command_text = _command_text(job)
    env = os.environ.copy()
    env.update(_merge_env(slot, job))
    cwd = job.cwd or slot.workdir or os.getcwd()
    wrapped = _wrap_shell_command(command_text, cwd, {}, str(log_path), str(status_path))
    if dry_run:
        return ActiveRun(
            slot=slot,
            job=job,
            attempt=attempt,
            started_at=time.time(),
            log_path=str(log_path),
            status_path=str(status_path),
        )
    process = subprocess.Popen(
        ["bash", "-lc", wrapped],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    return ActiveRun(
        slot=slot,
        job=job,
        attempt=attempt,
        started_at=time.time(),
        log_path=str(log_path),
        status_path=str(status_path),
        process=process,
    )


def _launch_slurm(slot: SlotSpec, job: JobSpec, attempt: int, run_dir: Path, dry_run: bool) -> ActiveRun:
    log_path, status_path = _local_paths(run_dir, slot, job, attempt)
    cwd = job.cwd or slot.workdir or os.getcwd()
    wrapped = _wrap_shell_command(
        _command_text(job),
        cwd,
        _merge_env(slot, job),
        str(log_path),
        str(status_path),
    )
    sbatch_cmd = ["sbatch", "--parsable"]
    if slot.partition:
        sbatch_cmd.extend(["-p", slot.partition])
    if slot.node or slot.host:
        sbatch_cmd.extend(["--nodelist", slot.node or str(slot.host)])
    if slot.gres:
        sbatch_cmd.extend(["--gres", slot.gres])
    if slot.cpus_per_task is not None:
        sbatch_cmd.extend(["--cpus-per-task", str(slot.cpus_per_task)])
    if slot.time_limit:
        sbatch_cmd.extend(["-t", slot.time_limit])
    sbatch_cmd.extend(
        [
            "-J",
            _safe_name(job.name)[:32],
            "--output",
            slot.output_pattern or str(run_dir / "slurm-%j.out"),
            "--wrap",
            wrapped,
        ]
    )
    if dry_run:
        return ActiveRun(
            slot=slot,
            job=job,
            attempt=attempt,
            started_at=time.time(),
            log_path=str(log_path),
            status_path=str(status_path),
            job_id="dry-run",
        )
    completed = subprocess.run(sbatch_cmd, check=True, text=True, capture_output=True)
    return ActiveRun(
        slot=slot,
        job=job,
        attempt=attempt,
        started_at=time.time(),
        log_path=str(log_path),
        status_path=str(status_path),
        job_id=completed.stdout.strip(),
    )


def _launch_ssh(
    slot: SlotSpec,
    job: JobSpec,
    attempt: int,
    run_dir: Path,
    password: str | None,
    dry_run: bool,
) -> ActiveRun:
    log_path, status_path = _remote_paths(run_dir, slot, job, attempt)
    cwd = job.cwd or slot.workdir
    wrapped = _wrap_shell_command(
        _command_text(job),
        cwd,
        _merge_env(slot, job),
        str(log_path),
        str(status_path),
    )
    session_name = f"slot_scheduler_{_safe_name(slot.name)}_{int(time.time())}"
    remote_cmd = (
        f"mkdir -p {shlex.quote(str(log_path.parent))} {shlex.quote(str(Path(status_path).parent))} && "
        f"tmux new-session -d -s {shlex.quote(session_name)} "
        f"{shlex.quote(f'bash --noprofile --norc -lc {shlex.quote(wrapped)}')}"
    )
    if dry_run:
        return ActiveRun(
            slot=slot,
            job=job,
            attempt=attempt,
            started_at=time.time(),
            log_path=str(log_path),
            status_path=str(status_path),
            session_name=session_name,
        )
    subprocess.run(_ssh_command(slot, password, remote_cmd), check=True, text=True, capture_output=True)
    return ActiveRun(
        slot=slot,
        job=job,
        attempt=attempt,
        started_at=time.time(),
        log_path=str(log_path),
        status_path=str(status_path),
        session_name=session_name,
    )
