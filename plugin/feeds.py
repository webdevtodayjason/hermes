"""feeds — host-formatted info pages for the device.

The firmware renders host pages dumbly (title + 2 lines, 34 chars each), so
all formatting intelligence lives here and improves without a reflash.

    slot 0: cron jobs   — next runs + last result, from ~/.hermes/cron/jobs.json
    slot 1: gateway     — uptime, platform health, sessions/tools/tokens today
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

_WIDTH = 34


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _fit(s: str) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= _WIDTH else s[:_WIDTH]


def _epoch(v) -> float:
    """next_run_at/last_run_at as sortable epoch; unknown sorts last."""
    try:
        if isinstance(v, (int, float)):
            return float(v)
        return datetime.fromisoformat(str(v)).timestamp()
    except Exception:
        return 9e18


def _hhmm(v) -> str:
    e = _epoch(v)
    if e >= 9e17:
        return "--:--"
    return datetime.fromtimestamp(e).strftime("%H:%M")


def cron_page() -> dict:
    try:
        jobs = json.loads((_home() / "cron" / "jobs.json").read_text()).get("jobs", [])
    except Exception:
        return {"type": "page", "slot": 0, "title": "CRON: store unreadable", "lines": []}
    active = [j for j in jobs if j.get("enabled") and str(j.get("state") or "") != "paused"]
    active.sort(key=lambda j: _epoch(j.get("next_run_at")))
    lines = []
    for j in active[:2]:
        status = str(j.get("last_status") or "")
        mark = "ok" if status == "ok" else ("--" if not status else "ER")
        lines.append(_fit(f"{_hhmm(j.get('next_run_at'))} {str(j.get('name', 'job'))[:24]} {mark}"))
    title = f"CRON: {len(active)} ACTIVE" if active else "CRON: none active"
    return {"type": "page", "slot": 0, "title": title, "lines": lines}


def vitals_page(started_at: float, stats: dict, link_line: str = "") -> dict:
    up = max(0, int(time.time() - started_at))
    upstr = f"{up // 3600}h{(up % 3600) // 60:02d}m" if up >= 3600 else f"{up // 60}m"
    plat = "?"
    try:
        gs = json.loads((_home() / "gateway_state.json").read_text())
        parts = [f"{name[:2]}:{'ok' if (p or {}).get('state') == 'connected' else 'ER'}"
                 for name, p in (gs.get("platforms") or {}).items()]
        if parts:
            plat = " ".join(parts[:4])
    except Exception:
        pass
    tok = int(stats.get("tokens_today", 0))
    tokstr = f"{tok / 1000:.0f}k" if tok >= 1000 else str(tok)
    lines = [
        _fit(f"up {upstr}  {plat}"),
        _fit(f"S:{stats.get('total', 0)} tools:{stats.get('tools_today', 0)} tok:{tokstr}"),
    ]
    if link_line:
        lines.append(_fit(link_line))
    return {"type": "page", "slot": 1, "title": "GATEWAY", "lines": lines}


def fleet_page() -> dict:
    """Kanban worker fleet at a glance (slot 2). Degrades to 'idle' text."""
    db = _home() / "kanban.db"
    if not db.exists():
        return {"type": "page", "slot": 2, "title": "FLEET: no kanban", "lines": []}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            by_status = dict(con.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())
            active = con.execute(
                "SELECT COALESCE(assignee,'worker'), title FROM tasks "
                "WHERE completed_at IS NULL AND started_at IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
            done24 = con.execute(
                "SELECT COUNT(*) FROM tasks WHERE completed_at >= ?",
                (time.time() - 86400,)).fetchone()[0]
            last_done = con.execute(
                "SELECT title FROM tasks WHERE completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception:
        return {"type": "page", "slot": 2, "title": "FLEET: db unreadable", "lines": []}
    running = sum(v for k, v in by_status.items()
                  if str(k).lower() in ("claimed", "running", "in_progress"))
    queued = sum(v for k, v in by_status.items()
                 if str(k).lower() in ("queued", "todo", "backlog", "open", "pending"))
    blocked = sum(v for k, v in by_status.items() if "block" in str(k).lower())
    lines = [_fit(f"run:{running} queue:{queued} blocked:{blocked} done24h:{done24}")]
    if active:
        lines.append(_fit(f"> {active[0]}: {active[1]}"))
    elif last_done:
        lines.append(_fit(f"last done: {last_done[0]}"))
    if not any(by_status.values() if by_status else []):
        lines = ["no tasks on the board"]
    return {"type": "page", "slot": 2,
            "title": f"FLEET: {'ACTIVE' if running else 'idle'}", "lines": lines}
