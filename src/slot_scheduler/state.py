from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def render_status(path: Path) -> str:
    events = load_events(path)
    if not events:
        return f"no events found in {path}"

    launched: dict[str, dict[str, Any]] = {}
    finished: list[dict[str, Any]] = []
    dry_run_finished = 0

    for event in events:
        kind = event.get("event")
        if kind == "launched":
            slot = str(event.get("slot"))
            launched[slot] = event
        elif kind == "finished":
            slot = str(event.get("slot"))
            launched.pop(slot, None)
            finished.append(event)
            if event.get("result") == "dry_run":
                dry_run_finished += 1

    success = sum(1 for event in finished if event.get("result") == "succeeded")
    failed = sum(1 for event in finished if event.get("result") == "failed")
    unknown = sum(1 for event in finished if event.get("result") == "unknown")

    lines = [
        f"state file: {path}",
        f"events: {len(events)}",
        f"finished: {len(finished)} succeeded={success} failed={failed} unknown={unknown} dry_run={dry_run_finished}",
        f"active slots: {len(launched)}",
    ]
    for slot, event in sorted(launched.items()):
        lines.append(f"{slot}: running {event.get('job')} attempt={event.get('attempt')}")
    return "\n".join(lines)

