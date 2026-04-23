# slot-scheduler

`slot-scheduler` is a small, repo-friendly experiment launcher for mixed compute fleets.

It is designed for the annoying real-world setup where some jobs should go through Slurm, some should go through plain SSH, and some can run locally. Instead of hard-coding all of that inside one project script, this repo keeps the resource inventory, backend logic, and queueing loop separate from your experiment code.

## Positioning

`slot-scheduler` is not trying to compete with full experiment platforms such as ClearML.

ClearML, SkyPilot, Ray-based platforms, and similar systems already do much more than this project:

- experiment tracking
- artifact and model management
- dashboards and web UIs
- pipelines and higher-level orchestration
- cluster- or cloud-level control planes

This project exists for the narrower case where those systems feel too heavy.

The goal here is:

- no server to deploy
- no database to manage
- no required SDK instrumentation in training code
- no assumption that all compute belongs to one cluster manager
- plain files, plain YAML, plain shell or Python commands

In other words, `slot-scheduler` is best thought of as a lightweight scheduling layer for messy real-world research compute, not as a full MLOps platform.

## What It Does

- Treats each runnable resource as a `slot`
- Supports `local`, `ssh + tmux`, and `slurm` backends
- Reads slots and jobs from YAML
- Launches one job per slot
- Polls running jobs and backfills open slots
- Writes a JSONL event log for inspection and post-processing
- Records per-job console logs and exit-code markers

## What It Does Not Do Yet

- No automatic rsync / code sync layer
- No DAG / dependency scheduling
- No autoscaling or utilization-aware packing
- No database; state is intentionally plain files

That is deliberate for the first version. The goal is a simple, hackable control plane that you can point at different repos.

## Use This When

`slot-scheduler` is a good fit if you want to:

- keep existing training scripts unchanged
- schedule plain shell or Python commands
- mix `local`, `ssh`, and `slurm` resources in one inventory
- run from inside a normal repo without deploying extra infrastructure
- keep state transparent and grep-friendly
- tweak scheduling behavior quickly in code

## Use Something Else When

You should probably use a heavier system if you need:

- experiment tracking, lineage, artifacts, and dashboards
- multi-user access control and tenancy
- managed cloud provisioning at scale
- autoscaling clusters and infrastructure optimization
- pipelines, workflow graphs, or production-grade orchestration
- a UI-first workflow for non-programmatic users

For those cases, tools such as ClearML, SkyPilot, Ray ecosystems, Dask, Runhouse, Ansible, or GNU Parallel may be a better starting point depending on the shape of the problem.

## Non-Goals

This repo is intentionally not trying to be:

- a ClearML replacement
- an experiment tracking system
- a workflow engine
- a cloud control plane
- a distributed runtime

The value proposition is narrower: make it easy to keep a small fleet of heterogeneous machines busy without forcing the rest of your stack to change.

## Layout

```text
slot-scheduler/
├── pyproject.toml
├── README.md
├── examples/
│   ├── inventory.leap2.yaml
│   ├── inventory.mixed.yaml
│   ├── inventory.txstate-ssh.yaml
│   └── jobs.demo.yaml
├── src/slot_scheduler/
│   ├── backends.py
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── scheduler.py
│   └── state.py
└── tests/
```

## Config Format

Inventory:

```yaml
defaults:
  password_env: LEAP2_PASSWORD
  poll_seconds: 30

slots:
  - name: gpu1-001
    backend: slurm
    host: gpu1-001
    node: gpu1-001
    gpu: 0
    partition: gpu1
    gres: gpu:1
    cpus_per_task: 48
    time_limit: 24:00:00
    workdir: /mmfs1/home/sqp17/Projects/VLMLP
    tags: [a100, leap2]

  - name: gpu2-001-g0
    backend: ssh
    host: gpu2-001
    gpu: 0
    workdir: /var/tmp/sqp17/code/VLMLP
    run_root: /var/tmp/sqp17/slot-scheduler/runs
    tags: [ssh, spillover]
```

SSH aliases from `~/.ssh/config` work too. That is often the cleanest way to use per-host keys:

```yaml
slots:
  - name: sun
    backend: ssh
    host: sun
    run_root: /tmp/slot-scheduler/runs
    tags: [ssh, sshkey]
```

Jobs:

```yaml
jobs:
  - name: smoke-a
    command: ["bash", "-lc", "hostname && sleep 5"]

  - name: only-on-slurm
    slots: [gpu1-001]
    command: ["bash", "-lc", "nvidia-smi && sleep 10"]

  - name: ssh-or-local
    backends: [ssh, local]
    retries: 1
    env:
      OMP_NUM_THREADS: "8"
    command: ["python", "-c", "print('hello from slot-scheduler')"]
```

Each job can optionally restrict itself with:

- `slots`: explicit slot names
- `backends`: allowed backend kinds
- `required_tags`: tags that must exist on the chosen slot
- `retries`: retry count after a non-zero exit code

## Usage

Create a run directory and launch:

```bash
uv run slot-scheduler run \
  --inventory examples/inventory.mixed.yaml \
  --jobs examples/jobs.demo.yaml \
  --run-dir .runs/demo
```

Show a compact summary later:

```bash
uv run slot-scheduler status --run-dir .runs/demo
```

Dry-run scheduling without launching:

```bash
uv run slot-scheduler run \
  --inventory examples/inventory.mixed.yaml \
  --jobs examples/jobs.demo.yaml \
  --run-dir .runs/demo \
  --dry-run
```

If your SSH hosts already use key-based login through `~/.ssh/config`, no password environment variable is required.

## State Files

Every run writes:

- `state.jsonl`: event log
- `console/*.log`: local backend logs, plus the target log paths for SSH/Slurm jobs
- `status/*.exitcode`: exit-code marker files

The JSONL log is meant to be easy to grep, summarize, or feed into a dashboard later.

## Notes For VLMLP

This repo intentionally stops at generic job scheduling. Things like:

- selecting best strategies from old metrics
- building `run_experiment.py` commands
- copying repo files to remote hosts

belong in the experiment repo or in a thin adapter layer on top of `slot-scheduler`.
