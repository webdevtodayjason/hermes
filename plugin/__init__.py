"""familiar — Hermes gateway plugin driving the ESP32-S3 Familiar over USB.

The desk companion, made native. Instead of a hand-run bridge polling
``state.db`` once a second, gateway hooks push live agent state to the device
the moment it changes, and the device's touch controls act on the real gateway:

    on_session_start / on_session_end  -> presence
    pre_llm_call                       -> "thinking…" (running=1)
    pre_tool_call / post_tool_call     -> "searching the web…" etc.
    post_llm_call                      -> reply line on the message ticker
    pre_approval_request               -> device pulses ALLOW/DENY (waiting=1)
    post_approval_response             -> waiting cleared (however it resolved)

Device -> host:
    action start/pause/cancel  -> JobManager subprocess (``hermes chat -q …``)
    permission once/deny       -> tools.approval.resolve_gateway_approval()
                                  (the same call /approve and /deny make)

Unlike embody (a voice-only face), the familiar shows ALL gateway activity —
cron, telegram, slack, kanban workers — that's its job as a desk companion.

The serial link starts ONLY in the gateway process ("gateway" in argv):
``hermes chat`` subprocesses load plugins too, and must not fight over the
port. Their hook callbacks see ``_link is None`` and no-op. The port is also
opened with ``exclusive=True`` as a second fence.

All hook callbacks are **kwargs-tolerant and never raise.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

from . import actions as _actions
from .serial_link import SerialLink

logger = logging.getLogger("familiar")

# tool name -> friendly activity label on the device (same idea as embody's)
_TOOL_STATUS = {
    "web_search":     "searching the web…",
    "search":         "searching the web…",
    "execute_code":   "running code…",
    "code_execution": "running code…",
    "computer_use":   "browsing…",
    "cronjob":        "scheduling…",
    "delegate_task":  "delegating…",
    "read_file":      "working with files…",
    "write_file":     "working with files…",
}

_STATS_REFRESH_SECS = 60.0

_lock = threading.Lock()
_turns: set[str] = set()                  # session_ids with an LLM call in flight
_pending: OrderedDict[str, str] = OrderedDict()   # approval session_key -> text
_entries: deque[str] = deque(maxlen=5)    # newest-first message ticker (page 1)
_msg = "Familiar linked to Hermes"
_stats = {"total": 0, "tokens_today": 0, "tools_today": 0, "at": 0.0}

_link: SerialLink | None = None
_jobs: _actions.JobManager | None = None


def _tool_status(tool_name: str) -> str:
    if not tool_name:
        return "working…"
    key = str(tool_name).lower()
    if key in _TOOL_STATUS:
        return _TOOL_STATUS[key]
    if key.startswith("browser"):
        return "browsing…"
    return f"{tool_name}…"


# --------------------------------------------------------------------------
# state payload
# --------------------------------------------------------------------------

def _refresh_stats() -> None:
    """Daily aggregates for page 0, read-only from state.db at most 1/min.
    Best-effort: on any failure the last values just go stale."""
    now = time.time()
    if now - _stats["at"] < _STATS_REFRESH_SECS:
        return
    _stats["at"] = now
    db = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            row = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(output_tokens),0),"
                " COALESCE(SUM(tool_call_count),0)"
                " FROM sessions WHERE started_at >= ?",
                (now - 24 * 3600,),
            ).fetchone()
        finally:
            con.close()
        _stats["total"], _stats["tokens_today"], _stats["tools_today"] = (
            int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))
    except Exception:
        pass


def _payload() -> dict:
    """Full device state frame (also the heartbeat, sent every 5s)."""
    _refresh_stats()
    jobs = _jobs.status() if _jobs else {"job_state": "idle", "job_label": ""}
    with _lock:
        running = 1 if _turns or jobs.get("job_state") in ("running", "paused") else 0
        return {
            "type": "state",
            "total": _stats["total"],
            "running": running,
            "waiting": len(_pending),
            "msg": _msg,
            "entries": list(_entries),
            "tokens_today": _stats["tokens_today"],
            "tools_today": _stats["tools_today"],
            **jobs,
        }


def _push(extra: dict | None = None) -> None:
    if _link is None:
        return
    if extra:
        _link.send(extra)
    _link.send(_payload())


def _set_msg(text: str) -> None:
    global _msg
    with _lock:
        _msg = text


# --------------------------------------------------------------------------
# gateway hooks (all **kwargs-tolerant, never raise)
# --------------------------------------------------------------------------

def _on_session_start(**kw):
    _push()


def _on_session_end(session_id="", **kw):
    with _lock:
        _turns.discard(str(session_id or "?"))
    _push()


def _on_pre_llm(platform="", session_id="", **kw):
    with _lock:
        _turns.add(str(session_id or "?"))
    _set_msg(f"thinking… ({platform})" if platform else "thinking…")
    if _link is not None:
        logger.info("push: thinking platform=%s connected=%s", platform, _link.connected)
    _push()


def _on_pre_tool(tool_name="", **kw):
    _set_msg(_tool_status(tool_name))
    _push()


def _on_post_tool(**kw):
    _set_msg("thinking…")
    _push()


def _on_post_llm(assistant_response="", platform="", session_id="", **kw):
    with _lock:
        _turns.discard(str(session_id or "?"))
    text = _actions.compact(str(assistant_response or ""), 140)
    if text:
        stamp = datetime.now().strftime("%H:%M")
        src = str(platform or "").strip()
        with _lock:
            _entries.appendleft(f"{stamp} a: {_actions.compact(text, 70)}")
        _set_msg(text if not src else f"[{src}] {text}"[:140])
        if _link is not None:
            logger.info("push: reply platform=%s len=%d connected=%s",
                        src, len(text), _link.connected)
        # event=message makes the portrait blink at a fresh reply
        _push({"type": "event", "event": "message", "role": "assistant", "msg": text})
        return
    _push()


def _on_pre_approval(command="", description="", session_key="", surface="", **kw):
    text = _actions.compact(str(description or command or "Hermes needs approval"), 140)
    with _lock:
        _pending[str(session_key)] = text
    _set_msg(text)
    _push({"type": "permission", "id": str(session_key), "text": text,
           "choices": ["once", "deny"]})


def _on_post_approval(session_key="", choice="", **kw):
    with _lock:
        _pending.pop(str(session_key), None)
    _set_msg(f"approval: {choice or 'resolved'}")
    _push()


# --------------------------------------------------------------------------
# device -> host
# --------------------------------------------------------------------------

def _handle_device_line(evt: dict) -> None:
    cmd = evt.get("cmd") or evt.get("event")
    if cmd == "action":
        action = evt.get("action")
        if action == "start":
            _push(_jobs.start_first_enabled())
        elif action == "pause":
            _push(_jobs.toggle_pause())
        elif action == "cancel":
            _push(_jobs.cancel())
        return
    if cmd == "permission":
        decision = str(evt.get("decision") or "once")
        want = str(evt.get("id") or "")
        with _lock:
            key = want if want in _pending else (next(iter(_pending), ""))
        if not key:
            _push({"type": "ack", "msg": "no pending approval", "waiting": 0})
            return
        try:
            n = _actions._resolve_approval(key, decision)
        except Exception as e:
            logger.exception("familiar approval resolve failed")
            _push({"type": "ack", "msg": f"approval failed: {_actions.compact(str(e), 60)}"})
            return
        # post_approval_response pops _pending and pushes fresh state
        _push({"type": "ack", "msg": f"sent: {decision}" if n else "approval already resolved"})
        return
    if cmd == "gesture" and evt.get("gesture") == "shake":
        _push({"type": "event", "event": "gesture", "msg": "shake — Familiar is awake"})
        return
    # touch/swipe/local_* are device-local; ping acks need no reply


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def _slash_familiar(raw_args: str = "") -> str:
    if _link is None:
        return "familiar: serial link inactive in this process (gateway-only)"
    with _lock:
        pend = len(_pending)
    port = _link.port if _link.connected else "searching…"
    jobs = _jobs.status() if _jobs else {}
    return (f"familiar: {'connected ' + str(port) if _link.connected else 'no device (' + str(port) + ')'}"
            f" | pending approvals: {pend}"
            f" | job: {jobs.get('job_state', '?')} ({jobs.get('job_label', '')})")


def _is_gateway_process() -> bool:
    return any(str(a) == "gateway" for a in sys.argv)


def register(ctx) -> None:
    global _link, _jobs
    cfg = _actions.load_config()
    _jobs = _actions.JobManager(cfg.get("actions"))

    if _is_gateway_process():
        serial_cfg = cfg.get("serial") or {}
        _link = SerialLink(
            _handle_device_line,
            port=serial_cfg.get("port"),
            baud=int(serial_cfg.get("baud", 115200)),
            heartbeat=5.0,
            make_heartbeat=_payload,
        )
        _link.start()
        logger.info("familiar serial link started (gateway process)")

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm)
    ctx.register_hook("pre_tool_call", _on_pre_tool)
    ctx.register_hook("post_tool_call", _on_post_tool)
    ctx.register_hook("post_llm_call", _on_post_llm)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_approval_request", _on_pre_approval)
    ctx.register_hook("post_approval_response", _on_post_approval)
    ctx.register_command(
        "familiar",
        handler=_slash_familiar,
        description="Familiar device link status",
    )
