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

import json
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
from . import feeds as _feeds
from . import loom_bell as _loom
from . import ritual as _ritual
from . import voice as _voice
from .serial_link import SerialLink

logger = logging.getLogger("familiar")

_started = {"at": time.time()}   # gateway (module load) time, for the vitals page
_voice_enabled = True            # familiar_actions.json {"voice":{"enabled":false}} to kill
_voice_port = 8765
_voice_cfg: dict = {}            # full voice block: volume, quiet window, morning_after
_loom_cfg: dict = {}             # familiar_actions.json {"loom":{"enabled":true,"board":...}}
_ritual_cfg: dict = {}           # familiar_actions.json {"ritual":{"enabled":true,"time":"18:30"}}
_ctx: dict = {}                  # {"ctx": PluginContext} — for ctx.llm in the ritual
_pages = {"at": 0.0}
_PAGES_REFRESH_SECS = 60.0

NOTIFY_SCHEMA = {
    "name": "familiar_notify",
    "description": (
        "Ping Jason's desk familiar (the ESP32 companion device): shows a banner on "
        "its screen and plays a chirp. Use when something deserves desk attention — "
        "a long task finished, a cron job found something important, you are blocked "
        "waiting on Jason, or a reminder fires. Keep the message under ~100 chars."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Short banner text"},
            "sound": {
                "type": "string",
                "enum": ["alert", "ack", "tap", "none"],
                "description": "Chirp to play (default alert)",
            },
            "speak": {
                "type": "boolean",
                "description": "Also speak the message aloud through the device speaker (default false)",
            },
        },
        "required": ["message"],
    },
}

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
_entries: deque[str] = deque(maxlen=40)   # newest-first ticker; device shows 5, scrollback pages the rest
_msg = "Familiar linked to Hermes"
_stats = {"total": 0, "tokens_today": 0, "tools_today": 0, "at": 0.0}

_link: SerialLink | None = None
_jobs: _actions.JobManager | None = None

# Presence: last real interaction per surface, so sound/voice land where
# Jason actually is. usb/tcp origins = desk, ws = phone.
_PRESENCE_WINDOW = 300.0                  # seconds; older than this = unknown
_INTERACTION_CMDS = {"touch", "swipe", "deck", "permission", "action", "gesture", "msgs"}
_presence = {"desk": 0.0, "phone": 0.0}
_desk_quiet = False                       # device reported face-down
_telemetry: dict = {}                     # last device telemetry frame
_BATT_WARN_V = 3.5
_BATT_CLEAR_V = 3.65                      # hysteresis: re-arm above this
_batt_warned = False


def _surface_of(origin: str) -> str:
    return "phone" if origin == "ws" else "desk"


def _active_surface() -> str | None:
    """Surface with the freshest interaction inside the window; None = unknown
    (be loud everywhere — silence by guess is worse than a spare chirp)."""
    d, p = _presence["desk"], _presence["phone"]
    best = max(d, p)
    if not best or time.time() - best > _PRESENCE_WINDOW:
        return None
    return "desk" if d >= p else "phone"


def _desk_should_be_quiet() -> bool:
    return _desk_quiet or _active_surface() == "phone"


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


def _maybe_push_pages() -> None:
    """Once-a-minute housekeeping on the link thread: device pages, the Loom
    bell, and the evening ritual (which offloads to its own thread)."""
    if _link is None:
        return
    # Surface a finished deck job on EVERY tick (~5s heartbeat), not the 60s
    # page timer — otherwise the result banner lags the button going off-green
    # by up to a minute.
    try:
        _surface_job_result()
    except Exception:
        logger.exception("familiar job-result surfacing failed")
    now = time.time()
    if now - _pages["at"] < _PAGES_REFRESH_SECS:
        return
    _pages["at"] = now
    try:
        _jobs.reload_if_changed()   # hot-reload the deck when the config file changes
        bat = float(_telemetry.get("bat") or 0.0)
        link_line = f"link:{_link.transport}" + (f" bat:{bat:.2f}V" if bat else "") \
            + (" QUIET" if _desk_quiet else "")
        _link.send(_feeds.cron_page())
        _link.send(_feeds.vitals_page(_started["at"], _stats, link_line))
        _link.send(_feeds.fleet_page())
        _link.send(_actions.deck_frame(_jobs.actions))
    except Exception:
        logger.exception("familiar page feed failed")
    try:
        _loom_tick()
    except Exception:
        logger.exception("familiar loom bell failed")
    try:
        _ritual_tick()
    except Exception:
        logger.exception("familiar ritual failed")


def _surface_job_result() -> None:
    """Reap a finished deck job and surface its result the SAME tick it ends —
    banner + ticker + speak — so the result lands together with the button
    going off-green, not up to 60s later. Sends the notify DIRECTLY (never via
    _push, which rebuilds the payload → would recurse from inside _payload).
    The ticker entry rides the state frame _payload builds right after this."""
    if _jobs is None or _link is None:
        return
    _jobs.status()                 # reap now so job_index clears this same frame
    res = _jobs.pop_result()
    if not res:
        return
    label = res.get("label", "job")
    text = _actions.compact(str(res.get("text") or ""), 140)
    ok = res.get("rc", 0) == 0
    stamp = datetime.now().strftime("%H:%M")
    with _lock:
        _entries.appendleft(f"{stamp} >{label}: {_actions.compact(text, 70)}")
    _set_msg(f"[{label}] {text}")
    _link.send({"type": "notify", "msg": f"{label}: {text}",
                "sound": "ack" if ok else "alert"})
    _say_async(text)
    logger.info("deck result [%s] rc=%s: %s", label, res.get("rc"), text[:120])


def _loom_tick() -> None:
    if not _loom_cfg.get("enabled", True):
        return
    events = _loom.check(_loom_cfg.get("board"))
    if not events:
        return
    stamp = datetime.now().strftime("%H:%M")
    with _lock:
        for ev in events[:6]:
            _entries.appendleft(f"{stamp} L: {_actions.compact(ev['text'], 70)}")
    loud = [ev for ev in events if ev["speak"]]
    if len(events) > 3 and not loud:
        _push({"type": "notify", "msg": f"Loom: {len(events)} new events", "sound": "tap"})
        return
    for ev in loud[:2]:
        _push({"type": "notify", "msg": _actions.compact(ev["text"], 120), "sound": "alert"})
        _say_async(ev["text"])
    if not loud:
        _push()


def _maybe_morning() -> None:
    """First device touch of the day (after voice.morning_after, default 05:00)
    speaks a good-morning brief — the evening ritual's mirror."""
    if not _ritual_cfg.get("morning", True) or not _voice_enabled or _quiet_now():
        return
    now = datetime.now()
    if not _ritual.morning_due(now, _voice_cfg.get("morning_after", "05:00")):
        return
    _ritual.mark_morning(now)

    def _work():
        _refresh_stats()
        material = _ritual.gather(now, _stats, _loom_cfg.get("board") or _loom.DEFAULT_BOARD,
                                  closet=_ritual_cfg.get("closet"))
        llm = getattr(_ctx["ctx"], "llm", None) if _ctx.get("ctx") else None
        digest = _ritual.compose(material, llm=llm, mood="morning")
        logger.info("morning greeting (%d chars): %s", len(digest), digest[:120])
        _push({"type": "notify", "msg": "Good morning", "sound": "none"})
        url = _say_url(digest)
        if url and _link is not None:
            _link.send({"type": "say", "url": url})

    threading.Thread(target=_work, name="familiar-morning", daemon=True).start()


def _ritual_tick() -> None:
    if not _ritual_cfg.get("enabled", True):
        return
    now = datetime.now()
    if not _ritual.due(now, _ritual_cfg.get("time", _ritual.DEFAULT_TIME)):
        return
    _ritual.mark_done(now)   # mark first — a failed compose must not retry-loop

    def _work():
        _refresh_stats()
        material = _ritual.gather(now, _stats, _loom_cfg.get("board") or _loom.DEFAULT_BOARD,
                                  closet=_ritual_cfg.get("closet"))
        llm = getattr(_ctx["ctx"], "llm", None) if _ctx.get("ctx") else None
        digest = _ritual.compose(material, llm=llm)
        logger.info("evening digest (%d chars): %s", len(digest), digest[:160])
        _push({"type": "notify", "msg": "Evening digest — listen", "sound": "none"})
        with _lock:
            _entries.appendleft(f"{now.strftime('%H:%M')} *: evening digest")
        url = _say_url(digest)
        if url and _link is not None:
            _link.send({"type": "say", "url": url})

    threading.Thread(target=_work, name="familiar-ritual", daemon=True).start()


def _payload() -> dict:
    """Full device state frame (also the heartbeat, sent every 5s)."""
    _refresh_stats()
    _maybe_push_pages()
    jobs = _jobs.status() if _jobs else {"job_state": "idle", "job_label": ""}
    with _lock:
        running = 1 if _turns or jobs.get("job_state") in ("running", "paused") else 0
        return {
            "type": "state",
            "total": _stats["total"],
            "running": running,
            "waiting": len(_pending),
            "msg": _msg,
            "entries": list(_entries)[:5],
            "tokens_today": _stats["tokens_today"],
            "tools_today": _stats["tools_today"],
            "battery_v": round(float(_telemetry.get("bat") or 0.0), 2),
            "transport": getattr(_link, "transport", "none") if _link is not None else "none",
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


def _on_pre_llm(platform="", session_id="", user_message="", **kw):
    um = _actions.compact(str(user_message or ""), 70)
    with _lock:
        _turns.add(str(session_id or "?"))
        if um:
            _entries.appendleft(f"{datetime.now().strftime('%H:%M')} u: {um}")
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
    text = _actions.compact(str(description or command or "Hermes needs approval"), 100)
    # 360 fills the device's detail page (8 wrapped rows); RX buffer is 2048
    detail = _actions.compact(str(command or ""), 360) if description and command else ""
    with _lock:
        _pending[str(session_key)] = text
    _set_msg(text)
    frame = {"type": "permission", "id": str(session_key), "text": text,
             "detail": detail, "choices": ["once", "deny"]}
    desk_quiet = _desk_should_be_quiet()
    if _link is not None:
        desk_frame = dict(frame)
        if desk_quiet:
            desk_frame["quiet"] = True   # device shows it but skips the chirp+nag
        _link.send(desk_frame, leg="desk")
        _link.send(frame, leg="phone")
    logger.info("familiar: approval route=%s desk_quiet=%s",
                _active_surface() or "everywhere", desk_quiet)
    _push()
    if not desk_quiet:
        _say_async(f"Hermes needs an approval: {text}", only_while_pending=str(session_key))


def _on_post_approval(session_key="", choice="", **kw):
    with _lock:
        _pending.pop(str(session_key), None)
    _set_msg(f"approval: {choice or 'resolved'}")
    _push()


# Kanban worker fleet on the ticker; a blocked task is a real alert.

def _on_kanban_claimed(task_id="", assignee="", **kw):
    with _lock:
        _entries.appendleft(f"{datetime.now().strftime('%H:%M')} k: {assignee or 'worker'} claimed {task_id}")
    _push()


def _on_kanban_completed(task_id="", summary="", **kw):
    note = _actions.compact(str(summary or task_id), 60)
    with _lock:
        _entries.appendleft(f"{datetime.now().strftime('%H:%M')} k: done {note}")
    _push()


def _on_kanban_blocked(task_id="", reason="", **kw):
    msg = _actions.compact(f"kanban blocked: {reason or task_id}", 120)
    _set_msg(msg)
    _push({"type": "notify", "msg": msg, "sound": "alert"})
    _say_async(f"A kanban task is blocked. {reason or ''}")


# --------------------------------------------------------------------------
# device -> host
# --------------------------------------------------------------------------

def _handle_device_line(evt: dict, origin: str = "usb") -> None:
    global _desk_quiet, _batt_warned
    cmd = evt.get("cmd") or evt.get("event")
    if cmd in _INTERACTION_CMDS:
        _presence[_surface_of(origin)] = time.time()
    if cmd == "diag":
        logger.info("familiar: diag %s", evt)
        return
    if cmd in ("stats", "net") and _link is not None:
        # transient drill-down pages, replied only to the surface that asked
        leg = "phone" if origin == "ws" else "desk"
        if cmd == "stats":
            page = _feeds.stats_page(_stats, _jobs.status() if _jobs else {},
                                     _telemetry, getattr(_link, "transport", "none"),
                                     _started["at"])
        else:
            with _lock:
                pres = dict(_presence)
            page = _feeds.surfaces_page(pres, _link.peers(),
                                        getattr(_link, "transport", "none"),
                                        _active_surface())
        _link.send(page, leg=leg)
        logger.info("familiar: %s page -> %s", cmd, leg)
        return
    if cmd == "telemetry":
        logger.info("familiar: telemetry %s", evt)   # once/min; remote calibration eyes
        _telemetry.update(evt)
        bat = float(evt.get("bat") or 0.0)
        on_usb = bool(evt.get("usb", True))
        if _desk_quiet != bool(evt.get("quiet")):
            _desk_quiet = bool(evt.get("quiet"))   # resync if a gesture frame was missed
        if not on_usb and 0.0 < bat < _BATT_WARN_V and not _batt_warned:
            _batt_warned = True
            logger.info("familiar: LOW BATTERY %.2fV (untethered) — warning once", bat)
            _push({"type": "notify", "msg": f"Familiar battery low: {bat:.2f}V — plug me in",
                   "sound": "alert"})
        elif (on_usb or bat >= _BATT_CLEAR_V) and _batt_warned:
            _batt_warned = False   # crossing cleared; re-arm
        return
    if cmd == "deck":
        try:
            i = int(evt.get("i", -1))
        except (TypeError, ValueError):
            i = -1
        res = _jobs.start_by_index(i)
        logger.info("deck: button %d -> %s", i, res.get("msg"))
        _push(res)
        return
    if cmd == "msgs":
        try:
            off = max(0, int(evt.get("off", 0)))
        except (TypeError, ValueError):
            off = 0
        with _lock:
            total = len(_entries)
            off = min(off, max(0, total - 1))
            lines = list(_entries)[off:off + 5]
        if _link is not None:
            _link.send({"type": "msgs", "off": off, "total": total,
                        "lines": [_actions.compact(l, 80) for l in lines]})
        return
    if cmd == "hello" or "hello" in evt:
        # device (re)booted — teach it where home is (for untethered TCP
        # reconnects) and resend the deck layout
        if _link is not None:
            ip = _voice.lan_ip()
            if ip:
                tcp_cfg = _actions.load_config().get("transport") or {}
                host = {"ip": ip, "port": int(tcp_cfg.get("port", 8767))}
                tok = str(tcp_cfg.get("token") or "").strip()
                if tok:
                    # device needs it to dial home now that TCP is authed
                    host["token"] = tok
                _link.send({"type": "config", "host": host})
            _link.send(_actions.deck_frame(_jobs.actions))
        logger.info("device: %s", evt)
        return
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
    if cmd == "touch":
        _maybe_morning()
        return
    if cmd == "gesture":
        g = str(evt.get("gesture") or "")
        if g == "shake":
            _push({"type": "event", "event": "gesture", "msg": "shake — Familiar is awake"})
        elif g in ("facedown", "upright"):
            _desk_quiet = bool(evt.get("quiet"))
            logger.info("familiar: desk %s — quiet=%s", g, _desk_quiet)
            # phones see the mode flip in their feed; desk shows nothing extra
            if _link is not None:
                _link.send({"type": "event", "event": "message", "role": "assistant",
                            "msg": "desk muted (face-down)" if _desk_quiet else "desk live again"},
                           leg="phone")
        elif g == "tap2":
            logger.info("familiar: desk tap2 — toast/nag acknowledged")
        elif g == "pickup":
            logger.info("familiar: desk pickup — Jason is at the desk")
        return
    if cmd is None:
        # boot lines, wifi status, say errors ({"say":"http--1"}) — surface them
        logger.info("device: %s", evt)
        return
    # touch/swipe/local_* are device-local; ping acks need no reply


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def _quiet_now() -> bool:
    """True inside the configured quiet window (voice.quiet: ["22:00","07:00"]),
    which may cross midnight. No window configured = never quiet."""
    win = (_voice_cfg.get("quiet") or [])
    if not (isinstance(win, (list, tuple)) and len(win) == 2):
        return False
    try:
        s = tuple(int(x) for x in str(win[0]).split(":", 1))
        e = tuple(int(x) for x in str(win[1]).split(":", 1))
    except (TypeError, ValueError):
        return False
    now = datetime.now()
    cur = (now.hour, now.minute)
    if s <= e:
        return s <= cur < e
    return cur >= s or cur < e


def _say_url(text: str) -> str | None:
    """Best-effort TTS clip URL; None when voice is off/unavailable/quiet."""
    if not _voice_enabled or _quiet_now():
        return None
    try:
        return _voice.say_url(text, port=_voice_port)
    except Exception:
        logger.exception("familiar say_url failed")
        return None


def _say_async(text: str, only_while_pending: str | None = None) -> None:
    """Render + send speech without blocking the calling (agent) thread.

    ``only_while_pending``: skip the send if that approval resolved while the
    clip was rendering — nobody wants a voice announcing a settled question.
    """
    if not _voice_enabled or _link is None:
        return
    if _desk_should_be_quiet():
        return   # face-down, or Jason is on the phone — no desk voice

    def _work():
        url = _say_url(text)
        if not url:
            return
        if only_while_pending is not None:
            with _lock:
                if only_while_pending not in _pending:
                    return
        _link.send({"type": "say", "url": url})

    threading.Thread(target=_work, name="familiar-say", daemon=True).start()


def _tool_notify(args=None, **kw) -> str:
    """familiar_notify tool handler — banner + chirp (+ speech) on the desk.

    Registry calling convention is ``handler(args_dict, **kwargs)``.
    """
    try:
        from tools.registry import tool_error, tool_result
    except Exception:  # outside/incompatible hermes runtime (tests on system Python)
        tool_result = lambda **k: json.dumps(k)          # noqa: E731
        tool_error = lambda m, **k: json.dumps({"error": str(m), **k})  # noqa: E731
    args = args if isinstance(args, dict) else {}
    if _link is None or not _link.connected:
        return tool_error("familiar device not connected")
    msg = _actions.compact(str(args.get("message") or ""), 120)
    if not msg:
        return tool_error("message is required")
    sound = args.get("sound", "alert")
    if sound not in ("alert", "ack", "tap", "none"):
        sound = "alert"
    if _quiet_now():
        sound = "none"   # quiet hours: banner still lands, silently
    frame = {"type": "notify", "msg": msg, "sound": sound}
    spoke = False
    desk_quiet = _desk_should_be_quiet()
    if args.get("speak") and not desk_quiet:
        url = _say_url(msg)
        if url:
            frame["say"] = url
            frame["sound"] = "none"   # the voice replaces the chirp
            spoke = True
    # presence routing: loud only where Jason is; other surfaces get the
    # banner silently. Unknown presence = loud everywhere.
    desk_frame = dict(frame)
    if desk_quiet:
        desk_frame["sound"] = "none"
        desk_frame.pop("say", None)
    _link.send(desk_frame, leg="desk")
    _link.send(frame, leg="phone")
    with _lock:
        _entries.appendleft(f"{datetime.now().strftime('%H:%M')} !: {_actions.compact(msg, 70)}")
    logger.info("push: notify len=%d sound=%s say=%s route=%s desk_quiet=%s",
                len(msg), frame["sound"], frame.get("say", "-"),
                _active_surface() or "everywhere", desk_quiet)
    return tool_result(success=True, delivered=msg, spoke=spoke)


def _slash_familiar(raw_args: str = "") -> str:
    """/familiar [status] | say <text> | ping <text> — deterministic device tests."""
    if _link is None:
        return "familiar: serial link inactive in this process (gateway-only)"
    args = str(raw_args or "").strip()
    if args.lower().startswith("say "):
        text = args[4:].strip() or "Hello from the familiar."
        if not _link.connected:
            return "familiar: no device connected"
        _say_async(text)
        return f"familiar: speaking ({len(text)} chars) — rendering, ~3s"
    if args.lower().startswith("ping"):
        msg = args[4:].strip() or "ping from /familiar"
        _link.send({"type": "notify", "msg": _actions.compact(msg, 120), "sound": "alert"})
        return "familiar: banner + chirp sent"
    with _lock:
        pend = len(_pending)
    port = _link.transport if _link.connected else "searching…"
    jobs = _jobs.status() if _jobs else {}
    return (f"familiar: {'connected ' + str(port) if _link.connected else 'no device (' + str(port) + ')'}"
            f" | pending approvals: {pend}"
            f" | job: {jobs.get('job_state', '?')} ({jobs.get('job_label', '')})"
            f" | try: /familiar say hello · /familiar ping")


def _is_gateway_process() -> bool:
    return any(str(a) == "gateway" for a in sys.argv)


def register(ctx) -> None:
    global _link, _jobs, _voice_enabled, _voice_port
    cfg = _actions.load_config()
    _jobs = _actions.JobManager(cfg.get("actions"))
    vc = cfg.get("voice") or {}
    _voice_enabled = bool(vc.get("enabled", True))
    _voice_port = int(vc.get("port", 8765))
    _voice_cfg.update(vc)
    _voice.set_volume(vc.get("volume", 1.0))
    _loom_cfg.update(cfg.get("loom") or {})
    _ritual_cfg.update(cfg.get("ritual") or {})
    _ctx["ctx"] = ctx

    if _is_gateway_process():
        serial_cfg = cfg.get("serial") or {}
        _link = SerialLink(
            _handle_device_line,
            port=serial_cfg.get("port"),
            baud=int(serial_cfg.get("baud", 115200)),
            heartbeat=2.0,   # also the deck-result detection cadence (was 5s)
            make_heartbeat=_payload,
        )
        def _welcome():
            """Snapshot for a fresh network client (phone/PWA): the broadcast
            only carries what happens after it joins, so hand it the deck,
            any pending approvals, and current state up front."""
            frames = [_actions.deck_frame(_jobs.actions if _jobs else [])]
            with _lock:
                pend = dict(_pending)
            for key, text in pend.items():
                frames.append({"type": "permission", "id": key, "text": text,
                               "choices": ["once", "deny"]})
            frames.append(_payload())
            return frames

        _link.on_client = _welcome
        _link.start()
        tcp_cfg = cfg.get("transport") or {}
        if tcp_cfg.get("enabled", True):
            _tok = str(tcp_cfg.get("token") or "").strip() or None
            if not _tok:
                logger.warning("familiar transport.token not set — tcp/ws legs are OPEN; "
                               "add transport.token to familiar_actions.json")
            _link.start_tcp(int(tcp_cfg.get("port", 8767)), token=_tok)
            _link.start_ws(int(tcp_cfg.get("ws_port", 8768)), token=_tok)
        logger.info("familiar serial link started (gateway process)")

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm)
    ctx.register_hook("pre_tool_call", _on_pre_tool)
    ctx.register_hook("post_tool_call", _on_post_tool)
    ctx.register_hook("post_llm_call", _on_post_llm)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_approval_request", _on_pre_approval)
    ctx.register_hook("post_approval_response", _on_post_approval)
    ctx.register_hook("kanban_task_claimed", _on_kanban_claimed)
    ctx.register_hook("kanban_task_completed", _on_kanban_completed)
    ctx.register_hook("kanban_task_blocked", _on_kanban_blocked)
    ctx.register_command(
        "familiar",
        handler=_slash_familiar,
        description="Familiar device link status",
    )
    ctx.register_tool(
        name="familiar_notify",
        toolset="familiar",
        schema=NOTIFY_SCHEMA,
        handler=_tool_notify,
        check_fn=lambda: _link is not None and _link.connected,
        description="Ping the desk familiar: screen banner + chirp",
        emoji="🐦",
    )
