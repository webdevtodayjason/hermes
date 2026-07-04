"""feeds — host-formatted info pages for the device.

The firmware renders host pages dumbly (title + 2 lines, 34 chars each), so
all formatting intelligence lives here and improves without a reflash.

    slot 0: cron jobs   — next runs + last result, from ~/.hermes/cron/jobs.json
    slot 1: gateway     — uptime, platform health, sessions/tools/tokens today
"""
from __future__ import annotations

import json
import os
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


def vitals_page(started_at: float, stats: dict) -> dict:
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
    return {"type": "page", "slot": 1, "title": "GATEWAY", "lines": [
        _fit(f"up {upstr}  {plat}"),
        _fit(f"S:{stats.get('total', 0)} tools:{stats.get('tools_today', 0)} tok:{tokstr}"),
    ]}
