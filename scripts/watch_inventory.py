#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import textwrap
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml


SSH_OPTS = [
    "-o",
    "ConnectTimeout=4",
    "-o",
    "PreferredAuthentications=password,keyboard-interactive,gssapi-with-mic,publickey",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
]

FALLBACK_PASSWORD_ENV = "SLOT_SCHEDULER_WATCH_SSH_PASS"

REMOTE_PROBE_SCRIPT = textwrap.dedent(
    r"""
    set -euo pipefail

    probe_path="$1"
    current="${probe_path}"
    while [[ ! -e "${current}" && "${current}" != "/" ]]; do
      current="$(dirname "${current}")"
    done
    if [[ ! -e "${current}" ]]; then
      current="/"
    fi

    nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader,nounits | sed 's/^/GPU|/'

    proc_lines="$(nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null || true)"
    if [[ -n "${proc_lines}" ]]; then
      while IFS= read -r line; do
        [[ -n "${line}" && "${line}" != "No running processes found" ]] || continue
        printf 'PROC|%s\n' "${line}"
      done <<<"${proc_lines}"
    fi

    proc_pids="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null | sed '/^No running processes found$/d' | paste -sd, -)"
    if [[ -n "${proc_pids}" ]]; then
      ps -o pid=,user=,args= -p "${proc_pids}" | sed 's/^ *//; s/^/PS|/'
    fi

    df -lhP "${current}" 2>/dev/null | awk -v path="${current}" 'NR==2 { printf "PATH|%s,%s,%s,%s,%s\n", path, $2, $3, $4, $5 }'
    """
).strip()


@dataclass
class HostConfig:
    host: str
    backend_kinds: set[str] = field(default_factory=set)
    password_env: str | None = None
    probe_path: str | None = None
    declared_gpus: set[int] = field(default_factory=set)
    use_all_gpus: bool = False


@dataclass
class HostSnapshot:
    host: str
    ok: bool
    gpu_lines: list[str] = field(default_factory=list)
    proc_lines: list[str] = field(default_factory=list)
    ps_lines: list[str] = field(default_factory=list)
    path_payload: str | None = None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch GPU/process state for hosts declared in a slot-scheduler inventory."
    )
    parser.add_argument("--inventory", required=True, type=Path, help="Path to inventory YAML")
    parser.add_argument("--once", "-1", action="store_true", help="Print one snapshot and exit")
    parser.add_argument(
        "-n",
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds (default: 1)",
    )
    parser.add_argument("--askpass", dest="askpass", action="store_true", help="Prompt once for a fallback SSH password")
    parser.add_argument(
        "--no-askpass",
        dest="askpass",
        action="store_false",
        help="Do not prompt; only use key auth or password env vars",
    )
    parser.set_defaults(askpass=False)
    return parser.parse_args()


def simplify_command(value: str) -> str:
    parts = value.split()
    if not parts:
        return value
    first = os.path.basename(parts[0])
    if first.startswith("python"):
        return " ".join([first, *parts[1:]])
    return value


def truncate_text(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else f"{value[: max_len - 3]}..."


def load_hosts(inventory_path: Path) -> list[HostConfig]:
    data = yaml.safe_load(inventory_path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    default_password_env = defaults.get("password_env")

    hosts: OrderedDict[str, HostConfig] = OrderedDict()
    for raw_slot in data.get("slots") or []:
        if not isinstance(raw_slot, dict):
            continue
        host = raw_slot.get("host")
        if not host:
            continue
        host = str(host)
        config = hosts.get(host)
        if config is None:
            config = HostConfig(host=host)
            hosts[host] = config

        backend = str(raw_slot.get("backend", ""))
        if backend:
            config.backend_kinds.add(backend)

        password_env = raw_slot.get("password_env") or default_password_env
        if config.password_env is None and password_env:
            config.password_env = str(password_env)

        probe_path = raw_slot.get("run_root") or raw_slot.get("workdir")
        if config.probe_path is None and probe_path:
            config.probe_path = str(probe_path)

        gpu = raw_slot.get("gpu")
        if gpu is None:
            config.use_all_gpus = True
        else:
            config.declared_gpus.add(int(gpu))

    for config in hosts.values():
        if config.probe_path is None:
            config.probe_path = "/tmp"
    return list(hosts.values())


def append_unique_csv(current: str, value: str) -> str:
    if not value:
        return current
    items = [part for part in current.split(",") if part]
    if value not in items:
        items.append(value)
    return ",".join(items)


def query_slurm(hosts: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    if not hosts or shutil.which("sinfo") is None:
        return {}, {}

    node_csv = ",".join(hosts)
    node_states: dict[str, str] = {}
    node_owners: dict[str, str] = {}

    completed = subprocess.run(
        ["sinfo", "-h", "-N", "-n", node_csv, "-o", "%N|%t"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.stdout:
        for line in completed.stdout.splitlines():
            try:
                node, state = [part.strip() for part in line.split("|", 1)]
            except ValueError:
                continue
            if node:
                node_states[node] = state.upper()

    if shutil.which("squeue") is None:
        return node_states, node_owners

    completed = subprocess.run(
        ["squeue", "-h", "-w", node_csv, "-o", "%N|%u|%T"],
        check=False,
        text=True,
        capture_output=True,
    )
    if not completed.stdout:
        return node_states, node_owners

    for line in completed.stdout.splitlines():
        try:
            nodes_expr, user, _job_state = [part.strip() for part in line.split("|", 2)]
        except ValueError:
            continue
        if not nodes_expr or not user:
            continue
        expanded_nodes = [nodes_expr]
        if shutil.which("scontrol") is not None:
            expanded = subprocess.run(
                ["scontrol", "show", "hostnames", nodes_expr],
                check=False,
                text=True,
                capture_output=True,
            )
            if expanded.stdout:
                expanded_nodes = [item.strip() for item in expanded.stdout.splitlines() if item.strip()]
        for node in expanded_nodes:
            node_owners[node] = append_unique_csv(node_owners.get(node, ""), user)
    return node_states, node_owners


def _password_for_host(config: HostConfig, fallback_password: str | None) -> str | None:
    if config.password_env:
        from_env = os.environ.get(config.password_env)
        if from_env:
            return from_env
    return fallback_password


def run_remote_probe(config: HostConfig, fallback_password: str | None) -> HostSnapshot:
    password = _password_for_host(config, fallback_password)
    command: list[str] = []
    if password:
        if shutil.which("sshpass") is None:
            return HostSnapshot(host=config.host, ok=False, error="sshpass_missing")
        command.extend(["sshpass", "-p", password])

    if config.host in {"localhost", "127.0.0.1"}:
        command.extend(["bash", "--noprofile", "--norc", "-s", "--", str(config.probe_path)])
    else:
        command.extend(["ssh", *SSH_OPTS, config.host, "bash", "--noprofile", "--norc", "-s", "--", str(config.probe_path)])

    completed = subprocess.run(
        command,
        input=f"{REMOTE_PROBE_SCRIPT}\n",
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        error = completed.stderr.strip() or f"ssh_failed_rc_{completed.returncode}"
        return HostSnapshot(host=config.host, ok=False, error=error)

    snapshot = HostSnapshot(host=config.host, ok=True)
    for line in completed.stdout.splitlines():
        if line.startswith("GPU|"):
            snapshot.gpu_lines.append(line.removeprefix("GPU|").strip())
        elif line.startswith("PROC|"):
            snapshot.proc_lines.append(line.removeprefix("PROC|").strip())
        elif line.startswith("PS|"):
            snapshot.ps_lines.append(line.removeprefix("PS|").strip())
        elif line.startswith("PATH|"):
            snapshot.path_payload = line.removeprefix("PATH|").strip()
    return snapshot


def render_host(
    config: HostConfig,
    snapshot: HostSnapshot,
    node_states: dict[str, str],
    node_owners: dict[str, str],
    color: bool,
) -> tuple[list[str], int]:
    lines: list[str] = []
    gpu_count = 0

    green = "\033[32m" if color else ""
    red = "\033[31m" if color else ""
    blue = "\033[34m" if color else ""
    reset = "\033[0m" if color else ""

    state = node_states.get(config.host, "UNKNOWN")
    owner = node_owners.get(config.host, "") or "-"
    if "DRAIN" in state:
        status_display = f"{red}DRAIN{reset}"
    elif state == "IDLE":
        status_display = f"{green}idle{reset}"
    elif owner != "-":
        status_display = owner
    else:
        status_display = state

    path_display = "SSH_FAIL"
    if snapshot.ok and snapshot.path_payload:
        try:
            path_name, size, used, avail, use_pct = [part.strip() for part in snapshot.path_payload.split(",", 4)]
            path_display = f"{path_name} {avail}/{size} ({use_pct})"
        except ValueError:
            path_display = snapshot.path_payload

    lines.append(f"{config.host}: {status_display} | {path_display}")

    if not snapshot.ok:
        if snapshot.error:
            lines.append(f"  error: {snapshot.error}")
        return lines, gpu_count

    gpu_uuid_to_index: dict[str, int] = {}
    gpu_records: dict[int, tuple[str, str, str, str]] = {}
    for raw in snapshot.gpu_lines:
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) < 7:
            continue
        gpu_idx = int(parts[0])
        gpu_uuid = parts[1]
        mem_used, mem_total, gpu_util, gpu_temp = parts[3], parts[4], parts[5], parts[6]
        gpu_uuid_to_index[gpu_uuid] = gpu_idx
        gpu_records[gpu_idx] = (mem_used, mem_total, gpu_util, gpu_temp)

    selected_gpus = sorted(gpu_records)
    if config.declared_gpus and not config.use_all_gpus:
        selected_gpus = [gpu for gpu in selected_gpus if gpu in config.declared_gpus]

    proc_user_by_pid: dict[str, str] = {}
    proc_cmd_by_pid: dict[str, str] = {}
    for raw in snapshot.ps_lines:
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            continue
        pid = parts[0].strip()
        user = parts[1].strip() if len(parts) >= 2 else "?"
        cmd = parts[2].strip() if len(parts) >= 3 else ""
        if pid:
            proc_user_by_pid[pid] = user or "?"
            proc_cmd_by_pid[pid] = cmd

    proc_lines_by_gpu: dict[int, list[tuple[str, str, str]]] = {}
    for raw in snapshot.proc_lines:
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) < 4:
            continue
        gpu_uuid, pid, proc_name, proc_mem = parts[0], parts[1], parts[2], parts[3]
        gpu_idx = gpu_uuid_to_index.get(gpu_uuid)
        if gpu_idx is None:
            continue
        if config.declared_gpus and not config.use_all_gpus and gpu_idx not in config.declared_gpus:
            continue
        proc_user = proc_user_by_pid.get(pid, owner if owner != "-" else "?")
        proc_cmd = proc_cmd_by_pid.get(pid, proc_name)
        proc_cmd = truncate_text(simplify_command(proc_cmd.replace("\t", " ")), 96)
        try:
            proc_mem_human = f"{float(proc_mem) / 1024.0:.1f} GB"
        except ValueError:
            proc_mem_human = proc_mem
        proc_lines_by_gpu.setdefault(gpu_idx, []).append((proc_mem_human, proc_user, proc_cmd))

    for gpu_idx in selected_gpus:
        mem_used, mem_total, gpu_util, gpu_temp = gpu_records[gpu_idx]
        try:
            mem_pct = f"{float(mem_used) * 100.0 / float(mem_total):.1f}%"
        except (ValueError, ZeroDivisionError):
            mem_pct = "0.0%"
        lines.append(f"  gpu{gpu_idx}: {mem_pct} | {gpu_util}% | {gpu_temp}C")
        gpu_count += 1
        for proc_mem_human, proc_user, proc_cmd in proc_lines_by_gpu.get(gpu_idx, []):
            lines.append(f"{blue}    {proc_mem_human} | {proc_user} | {proc_cmd}{reset}")

    return lines, gpu_count


def render_snapshot(
    hosts: list[HostConfig],
    fallback_password: str | None,
    color: bool,
) -> str:
    node_states, node_owners = query_slurm([config.host for config in hosts])
    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as executor:
        snapshots = list(executor.map(lambda cfg: run_remote_probe(cfg, fallback_password), hosts))

    rendered_lines: list[str] = []
    total_gpus = 0
    for config, snapshot in zip(hosts, snapshots, strict=True):
        host_lines, host_gpu_count = render_host(config, snapshot, node_states, node_owners, color)
        rendered_lines.extend(host_lines)
        total_gpus += host_gpu_count

    header = (
        f"watch_inventory | {time.strftime('%F %T %Z')} | "
        f"nodes={len(hosts)} | gpus={total_gpus}"
    )
    return f"{header}\n\n" + "\n".join(rendered_lines)


def main() -> int:
    args = parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")

    hosts = load_hosts(args.inventory)
    if not hosts:
        raise SystemExit(f"no hosts with a host field found in {args.inventory}")

    fallback_password = os.environ.get(FALLBACK_PASSWORD_ENV)
    needs_prompt = args.askpass and not fallback_password and any(
        config.password_env and not os.environ.get(config.password_env) for config in hosts
    )
    if needs_prompt:
        if not sys.stdin.isatty():
            raise SystemExit("--askpass requires an interactive terminal")
        fallback_password = getpass.getpass("SSH password: ")

    if fallback_password and shutil.which("sshpass") is None:
        raise SystemExit(f"{FALLBACK_PASSWORD_ENV} is set but sshpass is not available")

    interactive = sys.stdout.isatty() and not args.once
    if interactive:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

    try:
        while True:
            frame = render_snapshot(hosts, fallback_password, color=sys.stdout.isatty())
            if interactive:
                sys.stdout.write("\033[H\033[J")
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        if interactive:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
