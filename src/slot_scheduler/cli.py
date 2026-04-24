from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from .config import load_inventory, load_jobs
from .schedlang import compile_document, load_schedlang, write_yaml
from .scheduler import SchedulerConfig, run_scheduler
from .state import render_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule jobs across local, SSH, and Slurm slots.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="launch a scheduler run")
    run_parser.add_argument("--inventory", required=True, type=Path)
    run_parser.add_argument("--jobs", required=True, type=Path)
    run_parser.add_argument("--run-dir", required=True, type=Path)
    run_parser.add_argument("--poll-seconds", type=int, default=None)
    run_parser.add_argument("--deadline-hours", type=float, default=None)
    run_parser.add_argument("--default-password-env", type=str, default=None)
    run_parser.add_argument("--dry-run", action="store_true")

    compile_parser = subparsers.add_parser("compile", help="compile experimental schedlang into YAML")
    compile_parser.add_argument("--dsl", required=True, type=Path)
    compile_parser.add_argument("--jobs-out", required=True, type=Path)
    compile_parser.add_argument("--inventory-in", type=Path, default=None)
    compile_parser.add_argument("--inventory-out", type=Path, default=None)
    compile_parser.add_argument("--report-out", type=Path, default=None)

    status_parser = subparsers.add_parser("status", help="summarize a previous run")
    status_parser.add_argument("--run-dir", type=Path, default=None)
    status_parser.add_argument("--state-file", type=Path, default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        defaults, slots, host_policies = load_inventory(args.inventory)
        jobs = load_jobs(args.jobs)
        config = SchedulerConfig(
            run_dir=args.run_dir,
            poll_seconds=args.poll_seconds if args.poll_seconds is not None else defaults.poll_seconds,
            deadline_hours=args.deadline_hours,
            dry_run=args.dry_run,
            default_password_env=args.default_password_env or defaults.password_env,
            host_policies=host_policies,
        )
        state_path = run_scheduler(slots, jobs, config)
        print(state_path)
        return 0

    if args.command == "compile":
        document = load_schedlang(args.dsl)
        if args.inventory_in is None and args.inventory_out is not None:
            parser.error("--inventory-out requires --inventory-in")
        if args.inventory_in is None and document.policies:
            parser.error("DSL contains policy blocks; provide --inventory-in and --inventory-out to materialize them")
        if args.inventory_in is not None and args.inventory_out is None and document.policies:
            parser.error("policy blocks require --inventory-out")
        base_inventory = None
        if args.inventory_in is not None:
            base_inventory = yaml.safe_load(args.inventory_in.read_text(encoding="utf-8")) or {}
            if not isinstance(base_inventory, dict):
                parser.error("inventory input must be a YAML mapping")

        bundle = compile_document(document, base_inventory)
        write_yaml(args.jobs_out, bundle["jobs"])
        if args.inventory_out is not None:
            inventory_payload = bundle.get("inventory")
            if not isinstance(inventory_payload, dict):
                parser.error("inventory output requested but no derived inventory was produced")
            write_yaml(args.inventory_out, inventory_payload)
        if args.report_out is not None:
            report_payload = bundle.get("report")
            if not isinstance(report_payload, dict):
                parser.error("report generation failed")
            write_yaml(args.report_out, report_payload)

        print(args.jobs_out)
        if args.inventory_out is not None:
            print(args.inventory_out)
        if args.report_out is not None:
            print(args.report_out)
        return 0

    state_file = args.state_file
    if args.run_dir is not None:
        state_file = args.run_dir / "state.jsonl"
    if state_file is None:
        parser.error("status requires --run-dir or --state-file")
    print(render_status(state_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
