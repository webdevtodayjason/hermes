"""loom_bell — the desk is where the fleet's work arrives.

Watches the Loom coordination board (read-only) and turns thread activity
into physical presence:

    new HANDOFFS/*.md            -> banner + SPOKEN ("a thread handed off work")
    Active slice flips to DONE   -> banner + SPOKEN ("a thread finished")
    new Active slice claimed     -> banner only
    new JOURNAL.md entries       -> ticker lines

First run seeds state silently (no announcement storm on install). State
persists in ``~/.hermes/familiar_loom_state.json`` so gateway restarts don't
re-announce old events.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("familiar.loom")

DEFAULT_BOARD = "/Users/sem/code/loom/board"


def _state_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "familiar_loom_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, indent=1))
    except Exception:
        logger.exception("loom state save failed")


def _compact(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _journal_lines(board: Path) -> list[str]:
    try:
        return [l for l in (board / "JOURNAL.md").read_text().splitlines()
                if re.match(r"^\d{4}-\d{2}-\d{2} ", l)]
    except Exception:
        return []


def _handoffs(board: Path) -> list[str]:
    try:
        return sorted(p.name for p in (board / "HANDOFFS").glob("*.md")
                      if p.name != "_TEMPLATE.md")
    except Exception:
        return []


def _active_slices(board: Path) -> dict[str, str]:
    """Active Slices table -> {slice_name: 'done'|'active'}."""
    out: dict[str, str] = {}
    try:
        text = (board / "REGISTRY.md").read_text()
    except Exception:
        return out
    section = text.split("## Completed", 1)[0]
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 5 and cells[0] and cells[0] not in ("Slice", "---"):
            if set(cells[0]) == {"-"}:
                continue
            out[cells[0]] = "done" if "✅" in cells[4] else "active"
    return out


def check(board_dir: str | None = None) -> list[dict]:
    """Diff the board against saved state; return events, newest-safe.

    Event shape: {"kind", "text", "speak": bool}. Seeds silently on first run.
    """
    board = Path(board_dir or DEFAULT_BOARD)
    if not board.is_dir():
        return []
    state = _load_state()
    seeded = bool(state)

    journal = _journal_lines(board)
    handoffs = _handoffs(board)
    slices = _active_slices(board)

    events: list[dict] = []
    if seeded:
        # journal: announce lines beyond the remembered count
        for line in journal[int(state.get("journal", 0)):]:
            parts = line.split("·", 2)
            thread = parts[1].strip() if len(parts) > 2 else "thread"
            body = parts[2].strip() if len(parts) > 2 else line
            events.append({"kind": "journal", "speak": False,
                           "text": f"{thread}: {_compact(body, 90)}"})
        for name in handoffs:
            if name not in set(state.get("handoffs", [])):
                slug = re.sub(r"^[\dTZ:-]+-", "", name).removesuffix(".md")
                events.append({"kind": "handoff", "speak": True,
                               "text": f"New handoff on the Loom: {slug}"})
        prev = state.get("slices", {})
        for name, status in slices.items():
            if name not in prev:
                events.append({"kind": "slice", "speak": False,
                               "text": f"Loom: {name} started"})
            elif prev[name] == "active" and status == "done":
                events.append({"kind": "slice_done", "speak": True,
                               "text": f"Loom: {name} finished its slice"})

    _save_state({"journal": len(journal), "handoffs": handoffs, "slices": slices})
    if not seeded and (journal or handoffs or slices):
        logger.info("loom bell seeded: %d journal, %d handoffs, %d active slices",
                    len(journal), len(handoffs), len(slices))
    return events
