from __future__ import annotations

from pathlib import Path

from slot_scheduler.config import load_inventory, load_jobs


def test_load_inventory_and_jobs(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    jobs_path = tmp_path / "jobs.yaml"
    inventory_path.write_text(
        """
defaults:
  password_env: TEST_PASSWORD
  poll_seconds: 7

slots:
  - name: local-g0
    backend: local
    gpu: 0
    tags: [local, test]
  - name: ssh-g0
    backend: ssh
    host: box
    gpu: 1
    password_env: SSH_PASS
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    jobs_path.write_text(
        """
jobs:
  - name: list-command
    command: ["python", "-c", "print('ok')"]
    backends: [local, ssh]
    retries: 2
  - name: shell-command
    command: "echo shell"
    required_tags: [test]
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    defaults, slots = load_inventory(inventory_path)
    jobs = load_jobs(jobs_path)

    assert defaults.password_env == "TEST_PASSWORD"
    assert defaults.poll_seconds == 7
    assert [slot.name for slot in slots] == ["local-g0", "ssh-g0"]
    assert slots[1].host == "box"
    assert slots[1].password_env == "SSH_PASS"
    assert jobs[0].command == ("python", "-c", "print('ok')")
    assert jobs[0].backends == ("local", "ssh")
    assert jobs[0].retries == 2
    assert jobs[1].shell is True
    assert jobs[1].required_tags == ("test",)

