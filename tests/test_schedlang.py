from __future__ import annotations

from pathlib import Path

import yaml

from slot_scheduler.schedlang import (
    compile_document,
    compile_inventory_document,
    compile_jobs_document,
    compile_report_document,
    load_schedlang,
    parse_schedlang,
    write_yaml,
)


def test_parse_and_compile_schedlang_jobs() -> None:
    document = parse_schedlang(
        '''
pool ssh_demo {
  requires {
    backends = ["ssh"]
    host_tags = ["txstate"]
  }
}

experiment smoke {
  use_pool = "ssh_demo"
  matrix {
    dataset = ["ETTh2", "ETTm2"]
    pred_len = [96, 192]
  }
  env {
    OMP_NUM_THREADS = "8"
  }
  requires {
    gpu_count = 1
  }
  prefers {
    host_tags = ["a100"]
    avoid_host_tags = ["shared"]
    placement = "spread"
  }
  retries = 1
  name_template = "job_${dataset}_${pred_len}"
  command = """
bash -lc "python run.py ${dataset} ${pred_len}"
"""
}
        '''.strip()
    )

    payload = compile_jobs_document(document)
    jobs = payload["jobs"]

    assert len(jobs) == 4
    assert jobs[0]["name"] == "job_ETTh2_96"
    assert jobs[0]["backends"] == ["ssh"]
    assert jobs[0]["required_tags"] == ["txstate"]
    assert jobs[0]["requirements"] == {"backends": ["ssh"], "required_tags": ["txstate"], "gpu_count": 1}
    assert jobs[0]["preferences"] == {
        "host_tags": ["a100"],
        "avoid_host_tags": ["shared"],
        "placement": "spread",
    }
    assert jobs[0]["env"]["OMP_NUM_THREADS"] == "8"
    assert jobs[0]["command"] == 'bash -lc "python run.py ETTh2 96"'
    assert jobs[0]["retries"] == 1


def test_compile_schedlang_merges_legacy_and_structured_requirements() -> None:
    document = parse_schedlang(
        """
pool ssh_pool {
  backends = ["ssh"]
}

experiment large_train {
  use_pool = "ssh_pool"
  required_tags = ["txstate"]
  requires {
    slots = ["sun-g0", "sun-g1", "sun-g2", "sun-g3"]
    gpu_count = 4
  }
  command = "echo train"
}
        """.strip()
    )

    payload = compile_jobs_document(document)
    assert payload == {
        "jobs": [
            {
                "name": "large_train",
                "command": "echo train",
                "backends": ["ssh"],
                "required_tags": ["txstate"],
                "slots": ["sun-g0", "sun-g1", "sun-g2", "sun-g3"],
                "requirements": {
                    "backends": ["ssh"],
                    "required_tags": ["txstate"],
                    "slots": ["sun-g0", "sun-g1", "sun-g2", "sun-g3"],
                    "gpu_count": 4,
                },
            }
        ]
    }


def test_compile_schedlang_inventory_overlay() -> None:
    document = parse_schedlang(
        """
policy shared_half {
  hosts = ["sun", "moon"]
  max_active_fraction = 0.5
}
        """.strip()
    )
    base_inventory = {"defaults": {"poll_seconds": 20}, "host_policies": [{"host": "gauss", "max_active_slots": 1}]}

    compiled = compile_inventory_document(document, base_inventory)

    assert compiled["host_policies"] == [
        {"host": "gauss", "max_active_slots": 1},
        {"host": "sun", "max_active_fraction": 0.5},
        {"host": "moon", "max_active_fraction": 0.5},
    ]


def test_compile_schedlang_report_flags_multi_gpu_runtime_gap() -> None:
    jobs_payload = {
        "jobs": [
            {
                "name": "large-train",
                "command": "echo train",
                "requirements": {
                    "backends": ["ssh"],
                    "required_tags": ["txstate"],
                    "gpu_count": 4,
                },
            }
        ]
    }
    inventory = {
        "slots": [
            {"name": "sun-g0", "backend": "ssh", "host": "sun", "gpu": 0, "tags": ["txstate"]},
            {"name": "sun-g1", "backend": "ssh", "host": "sun", "gpu": 1, "tags": ["txstate"]},
            {"name": "sun-g2", "backend": "ssh", "host": "sun", "gpu": 2, "tags": ["txstate"]},
            {"name": "sun-g3", "backend": "ssh", "host": "sun", "gpu": 3, "tags": ["txstate"]},
        ]
    }

    report = compile_report_document(jobs_payload, inventory)

    assert report["summary"]["status_counts"] == {"needs_multi_slot_runtime": 1}
    assert report["jobs"][0]["status"] == "needs_multi_slot_runtime"
    assert report["jobs"][0]["candidate_hosts"] == ["sun"]


def test_compile_document_includes_jobs_inventory_and_report() -> None:
    document = parse_schedlang(
        """
policy shared_half {
  hosts = ["sun"]
  max_active_fraction = 0.5
}

experiment smoke {
  requires {
    backend = "ssh"
    host = "sun"
  }
  command = "echo smoke"
}
        """.strip()
    )
    inventory = {
        "slots": [
            {"name": "sun-g0", "backend": "ssh", "host": "sun", "gpu": 0, "tags": ["txstate"]},
            {"name": "moon-g0", "backend": "ssh", "host": "moon", "gpu": 0, "tags": ["txstate"]},
        ]
    }

    payload = compile_document(document, inventory)

    assert payload["jobs"]["jobs"][0]["requirements"]["hosts"] == ["sun"]
    assert payload["inventory"]["host_policies"] == [{"host": "sun", "max_active_fraction": 0.5}]
    assert payload["report"]["jobs"][0]["candidate_slots"] == ["sun-g0"]
    assert payload["report"]["jobs"][0]["status"] == "ready"


def test_load_schedlang_and_write_yaml(tmp_path: Path) -> None:
    dsl_path = tmp_path / "example.sched"
    jobs_out = tmp_path / "compiled" / "jobs.yaml"
    dsl_path.write_text(
        """
experiment one {
  command = "echo hello"
}
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    document = load_schedlang(dsl_path)
    payload = compile_jobs_document(document)
    write_yaml(jobs_out, payload)

    written = yaml.safe_load(jobs_out.read_text(encoding="utf-8"))
    assert written == {"jobs": [{"name": "one", "command": "echo hello"}]}
