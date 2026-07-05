"""ritual — the end-of-day voice: one spoken paragraph of what the fleet did.

At the configured hour (familiar_actions.json ``{"ritual": {"time": "18:30"}}``)
the familiar gathers the day's raw material — Loom journal entries, follow-ups
filed in the closet, Hermes session/token counts — and speaks a short digest.
Composition uses the gateway's own model via ``ctx.llm`` when available, with
a plain template fallback so the ritual never silently skips.

No thread ever dies in silence.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("familiar.ritual")

CLOSET = Path("/Users/sem/Obsidian Vault/Follow-Ups & To-Dos.md")
DEFAULT_TIME = "18:30"


def _state_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "familiar_ritual_state.json"


def due(now: datetime, at: str = DEFAULT_TIME) -> bool:
    """True once per day, at/after the configured HH:MM."""
    try:
        hh, mm = (int(x) for x in str(at).split(":", 1))
    except Exception:
        hh, mm = 18, 30
    if (now.hour, now.minute) < (hh, mm):
        return False
    try:
        last = json.loads(_state_path().read_text()).get("last", "")
    except Exception:
        last = ""
    return last != now.strftime("%Y-%m-%d")


def mark_done(now: datetime) -> None:
    try:
        _state_path().write_text(json.dumps({"last": now.strftime("%Y-%m-%d")}))
    except Exception:
        logger.exception("ritual state save failed")


def morning_due(now: datetime, after: str = "05:00") -> bool:
    """True once per day, at/after `after`, gated separately from the evening."""
    try:
        hh, mm = (int(x) for x in str(after).split(":", 1))
    except Exception:
        hh, mm = 5, 0
    if (now.hour, now.minute) < (hh, mm):
        return False
    try:
        last = json.loads(_state_path().read_text()).get("morning_last", "")
    except Exception:
        last = ""
    return last != now.strftime("%Y-%m-%d")


def mark_morning(now: datetime) -> None:
    try:
        state = {}
        try:
            state = json.loads(_state_path().read_text())
        except Exception:
            pass
        state["morning_last"] = now.strftime("%Y-%m-%d")
        _state_path().write_text(json.dumps(state))
    except Exception:
        logger.exception("morning state save failed")


def gather(now: datetime, stats: dict, board_dir: str, closet: str | None = None) -> dict:
    day = now.strftime("%Y-%m-%d")
    journal = []
    try:
        from . import loom_bell
        journal = [l for l in loom_bell._journal_lines(Path(board_dir))
                   if l.startswith(day)]
    except Exception:
        pass
    filed, closed = [], 0
    try:
        for line in (Path(closet) if closet else CLOSET).read_text().splitlines():
            if f"(added {day})" not in line:
                continue
            if line.lstrip().startswith("- [x]"):
                closed += 1
            elif line.lstrip().startswith("- [ ]"):
                title = re.sub(r"[*#\[\]]", "", line.split("**")[1] if "**" in line else line)
                filed.append(title.strip().rstrip("."))
    except Exception:
        pass
    return {
        "date": day,
        "journal": [" ".join(l.split())[:300] for l in journal[-8:]],
        "followups_filed": filed[:8],
        "followups_closed_today": closed,
        "hermes_sessions_today": stats.get("total", 0),
        "hermes_tools_today": stats.get("tools_today", 0),
        "hermes_tokens_today": stats.get("tokens_today", 0),
    }


_SYSTEM = {
    "evening": (
        "You are the evening voice of a desk familiar — a small companion device. "
        "Compose the day's digest to be SPOKEN aloud: under 90 words, plain "
        "sentences, no markdown, no lists. Warm, concrete, zero filler. Cover: "
        "what shipped/landed today, what's blocked or waiting on the operator, "
        "and what tomorrow inherits. If the day was quiet, say so briefly."),
    "morning": (
        "You are the morning voice of a desk familiar — a small companion device. "
        "The operator just sat down. Compose a good-morning brief to be SPOKEN "
        "aloud: under 70 words, plain sentences, no markdown. Lead with anything "
        "that happened overnight or is waiting on them, then what today inherits. "
        "If nothing happened, a one-line good morning is perfect."),
}


def compose(material: dict, llm=None, mood: str = "evening") -> str:
    """LLM digest when the host offers one; honest template otherwise."""
    if llm is not None:
        try:
            res = llm.complete(
                [
                    {"role": "system", "content": _SYSTEM.get(mood, _SYSTEM["evening"])},
                    {"role": "user", "content": json.dumps(material, indent=1)},
                ],
                max_tokens=400,
                timeout=90,
                purpose="familiar evening digest",
            )
            text = " ".join(str(res.text or "").split())
            if text:
                return text[:900]
        except Exception:
            logger.exception("ritual llm compose failed; using template")
    j, f = len(material["journal"]), len(material["followups_filed"])
    parts = [f"Day's end. {material['hermes_sessions_today']} Hermes sessions, "
             f"{material['hermes_tools_today']} tool calls."]
    if j:
        parts.append(f"{j} Loom journal entr{'y' if j == 1 else 'ies'} today.")
    if f:
        parts.append(f"{f} follow-up{'s' if f != 1 else ''} filed: "
                     + "; ".join(material["followups_filed"][:3]) + ".")
    if material["followups_closed_today"]:
        parts.append(f"{material['followups_closed_today']} closed.")
    if not (j or f):
        parts.append("A quiet day on the board.")
    return " ".join(parts)
