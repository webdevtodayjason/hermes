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
import time
from pathlib import Path

logger = logging.getLogger("familiar.actions")

DEFAULT_ACTIONS = {
    "actions": [
        {
            "id": "status_brief",
            "label": "Status brief",
            "enabled": True,
            "command": [
                "hermes", "chat", "-q",
                "Give me a concise current Hermes work/status brief. Include active "
                "tasks, blockers, and next best action. Keep it under 120 words.",
            ],
        },
    ],
}


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


def _resolve_approval(session_key: str, choice: str) -> int:
    """Unblock a pending gateway approval. Returns count resolved (0 = none)."""
    from tools.approval import resolve_gateway_approval  # gateway process only
    return resolve_gateway_approval(session_key, choice)


class JobManager:
    """At most one device-started subprocess job at a time (matches the
    single START/PAUSE/CANCEL band on the device)."""

    def __init__(self, actions: list[dict] | None = None):
        self.actions = actions if actions is not None else load_config().get("actions", [])
        self.proc: subprocess.Popen | None = None
        self.label = ""
        self.paused = False
        self.started_at = 0.0

    # -- state -------------------------------------------------------------

    def status(self) -> dict:
        """Job fields for the device state payload. Reaps finished jobs."""
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            self.paused = False
        if self.proc is None:
            enabled = [a for a in self.actions if a.get("enabled")]
            label = enabled[0].get("label", "No action") if enabled else "No enabled action"
            return {"job_state": "idle", "job_label": label}
        return {
            "job_state": "paused" if self.paused else "running",
            "job_label": self.label,
            "job_age": int(time.time() - self.started_at),
        }

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- device commands -----------------------------------------------------

    def start_first_enabled(self) -> dict:
        if self.active:
            return {"type": "ack", "msg": f"job already {'paused' if self.paused else 'running'}"}
        action = next((a for a in self.actions if a.get("enabled")), None)
        if not action:
            return {"type": "ack", "msg": "no enabled action"}
        cmd = action.get("command")
        # ponytail: subprocess-only in plugin mode; cron_job/API action types are
        # the standalone bridge's job (scripts/hermes_serial_bridge.py --api-url).
        if not (isinstance(cmd, list) and all(isinstance(x, str) for x in cmd)):
            return {"type": "ack", "msg": "action has no command"}
        try:
            self.proc = subprocess.Popen(
                cmd, text=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.label = action.get("label", "Action")
            self.paused = False
            self.started_at = time.time()
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
