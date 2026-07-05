"""actions — device-initiated work: action slots, job control, approvals.

Action slots come from ``~/.hermes/familiar_actions.json`` (same file the
standalone bridge uses, so both modes share one config). In plugin mode an
action runs as a ``hermes chat -q`` (or arbitrary ``command``) subprocess —
its lifecycle hooks fire in that child, while THIS gateway still sees the
device's job state via JobManager.

Approvals are the native path: when the gateway blocks on a dangerous-command
approval, ``pre_approval_request`` gives us the ``session_key``; the device's
ALLOW/DENY tap resolves it with ``tools.approval.resolve_gateway_approval`` —
the exact call the /approve and /deny slash commands make. Import is lazy so
this module also loads outside the gateway (tests, bridge reuse).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("familiar.actions")

DEFAULT_ACTIONS = {
    "actions": [
        {
            "id": "status_brief",
            "label": "BRIEF",
            "enabled": True,
            "color": "green",
            "prompt": "Give me a concise current Hermes work/status brief. Include active "
                      "tasks, blockers, and next best action. Keep it under 120 words.",
        },
    ],
}

_DECK_COLORS = ("green", "amber", "red", "cyan")


def deck_frame(actions: list[dict]) -> dict:
    """Device deck layout: up to 6 enabled actions -> OPS grid buttons.

    Each button: {"i", "label" (<=8 chars), "color", "confirm"}.
    """
    buttons = []
    for i, a in enumerate([a for a in actions if a.get("enabled")][:6]):
        color = str(a.get("color", "green")).lower()
        label = " ".join(str(a.get("label", f"BTN{i}")).split()).upper()[:8].strip()
        buttons.append({
            "i": i,
            "label": label or f"BTN{i}",
            "color": color if color in _DECK_COLORS else "green",
            "confirm": bool(a.get("confirm")),
        })
    return {"type": "deck", "buttons": buttons}


def _action_command(action: dict) -> list[str] | None:
    """An action runs its explicit command, or its prompt via `hermes chat -q`."""
    cmd = action.get("command")
    if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
        return cmd
    prompt = action.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return ["hermes", "chat", "-q", prompt]
    return None


def config_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "familiar_actions.json"


def load_config() -> dict:
    """Full familiar config: ``actions`` list plus optional ``serial`` block
    (``{"serial": {"port": "/dev/cu.usbmodemXXX", "baud": 115200}}``)."""
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_ACTIONS, indent=2) + "\n")
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else dict(DEFAULT_ACTIONS)
    except Exception as e:
        logger.warning("bad %s (%s); using defaults", path, e)
        return dict(DEFAULT_ACTIONS)


def compact(s: str, n: int = 120) -> str:
    s = " ".join((s or "").replace("\x00", "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


_NOISE_PREFIXES = ("Warning:", "Query:", "Initializing", "Resume this",
                   "Session:", "Duration:", "Messages:", "hermes --resume", "─")


def clean_output(text: str) -> str:
    """Pull the real answer out of `hermes chat -q` output (which wraps the
    reply in a ╭─ Hermes ─╮ … ╰──╯ box amid warnings + a query echo). Plain
    command output (no box) just has its noise lines stripped."""
    lines = (text or "").splitlines()
    box, in_box = [], False
    for ln in lines:
        s = ln.strip()
        if not in_box and s.startswith("╭") and "Hermes" in s:
            in_box = True
            continue
        if in_box and s.startswith("╰"):
            break
        if in_box:
            content = s.lstrip("│").strip()
            if content:
                box.append(content)
    if box:
        return " ".join(box)
    keep = [s for ln in lines
            if (s := ln.strip()) and not s.startswith(_NOISE_PREFIXES)]
    return " ".join(keep)


def _resolve_approval(session_key: str, choice: str) -> int:
    """Unblock a pending gateway approval. Returns count resolved (0 = none)."""
    from tools.approval import resolve_gateway_approval  # gateway process only
    return resolve_gateway_approval(session_key, choice)


class JobManager:
    """At most one device-started subprocess job at a time (matches the
    single START/PAUSE/CANCEL band on the device)."""

    def __init__(self, actions: list[dict] | None = None):
        self.actions = actions if actions is not None else load_config().get("actions", [])
        self._cfg_mtime = self._config_mtime()
        self.proc: subprocess.Popen | None = None
        self.label = ""
        self.paused = False
        self.started_at = 0.0
        self.running_index = -1   # index into enabled actions, -1 = none
        self._result: dict | None = None   # last finished job's captured output
        self._out_buf = ""                  # drained live by the reader thread

    @staticmethod
    def _config_mtime() -> float:
        try:
            return config_path().stat().st_mtime
        except OSError:
            return 0.0

    def reload_if_changed(self) -> bool:
        """Re-read familiar_actions.json when it changed on disk AND no job is
        running (so a live job's index stays valid). Returns True if reloaded."""
        if self.active:
            return False
        mt = self._config_mtime()
        if mt == self._cfg_mtime:
            return False
        self._cfg_mtime = mt
        self.actions = load_config().get("actions", [])
        return True

    # -- state -------------------------------------------------------------

    def status(self) -> dict:
        """Job fields for the device state payload. Reaps finished jobs and
        captures their output into ``self._result`` for the device to surface."""
        if self.proc is not None and self.proc.poll() is not None:
            rc = self.proc.returncode
            out = self._out_buf or ""   # drained by the reader thread — no pipe deadlock
            self._result = {"label": self.label, "rc": rc,
                            "text": compact(clean_output(out), 300) or f"{self.label} done (rc={rc})"}
            self.proc = None
            self.paused = False
            self.running_index = -1
        if self.proc is None:
            enabled = [a for a in self.actions if a.get("enabled")]
            label = enabled[0].get("label", "No action") if enabled else "No enabled action"
            return {"job_state": "idle", "job_label": label, "job_index": -1}
        return {
            "job_state": "paused" if self.paused else "running",
            "job_label": self.label,
            "job_index": self.running_index,
            "job_age": int(time.time() - self.started_at),
        }

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def pop_result(self) -> dict | None:
        """Return + clear the last finished job's captured output, or None."""
        r, self._result = self._result, None
        return r

    def _start_reader(self, proc) -> None:
        """Drain the job's combined stdout+stderr live so a chatty command
        can never deadlock on a full pipe. Keeps only the tail we'll show."""
        def _read():
            buf = []
            try:
                for line in proc.stdout:
                    buf.append(line)
                    if len(buf) > 200:      # keep the tail; we only surface ~300 chars
                        buf.pop(0)
            except Exception:
                pass
            self._out_buf = "".join(buf)
        threading.Thread(target=_read, name="familiar-job-out", daemon=True).start()

    # -- device commands -----------------------------------------------------

    def start_first_enabled(self) -> dict:
        return self.start_by_index(0)

    def start_by_index(self, i: int) -> dict:
        """Deck button i (index into ENABLED actions). One job at a time;
        pressing the running action's button again cancels it (toggle)."""
        enabled = [a for a in self.actions if a.get("enabled")]
        if not (0 <= i < len(enabled)):
            return {"type": "ack", "msg": f"no action {i}"}
        action = enabled[i]
        if self.active:
            if self.running_index == i:
                return self.cancel()
            return {"type": "ack", "msg": f"busy: {self.label}"}
        cmd = _action_command(action)
        # ponytail: subprocess-only in plugin mode; cron_job/API action types are
        # the standalone bridge's job (scripts/hermes_serial_bridge.py --api-url).
        if cmd is None:
            return {"type": "ack", "msg": "action has no command or prompt"}
        try:
            self.proc = subprocess.Popen(
                cmd, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.label = action.get("label", "Action")
            self.running_index = i
            self.paused = False
            self.started_at = time.time()
            self._out_buf = ""
            self._start_reader(self.proc)
            return {"type": "ack", "msg": f"started: {self.label}"}
        except Exception as e:
            return {"type": "ack", "msg": f"start failed: {compact(str(e), 60)}"}

    def toggle_pause(self) -> dict:
        if not self.active:
            return {"type": "ack", "msg": "no running job"}
        try:
            sig = signal.SIGCONT if self.paused else signal.SIGSTOP
            os.killpg(self.proc.pid, sig)
            self.paused = not self.paused
            return {"type": "ack", "msg": "job paused" if self.paused else "job resumed"}
        except Exception as e:
            return {"type": "ack", "msg": f"pause failed: {compact(str(e), 60)}"}

    def cancel(self) -> dict:
        if not self.active:
            return {"type": "ack", "msg": "no running job"}
        try:
            if self.paused:  # a stopped process can't handle SIGTERM
                os.killpg(self.proc.pid, signal.SIGCONT)
                self.paused = False
            os.killpg(self.proc.pid, signal.SIGTERM)
            return {"type": "ack", "msg": "job cancel sent"}
        except Exception as e:
            return {"type": "ack", "msg": f"cancel failed: {compact(str(e), 60)}"}
