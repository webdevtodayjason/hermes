#!/usr/bin/env python3
"""Hermes Familiar USB bridge.

Bidirectional bridge between Hermes state and the ESP32 Hermes Familiar.

What it does now:
- Polls Hermes' SQLite state DB read-only for recent activity.
- Streams compact state/events to the ESP32 over newline-delimited JSON.
- Consumes device commands from touch zones.
- Maintains a small host-side job runner so the device can start/pause/resume/cancel
  a predefined Hermes task.

This is intentionally safe-by-default: actions come from a JSON config file and
only the first action slot is enabled by default. The bridge never writes to the
Hermes state DB.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any


ATTENTION_WORDS = ("?", "approve", "confirm", "permission", "choose", "which", "should i", "do you want")
DEFAULT_ACTIONS = {
    "actions": [
        {
            "id": "status_brief",
            "label": "Status brief",
            "enabled": True,
            "type": "run",
            "prompt": "Give me a concise current Hermes work/status brief. Include active tasks, blockers, and next best action. Keep it under 120 words.",
            "command": [
                "hermes",
                "chat",
                "-q",
                "Give me a concise current Hermes work/status brief. Include active tasks, blockers, and next best action. Keep it under 120 words.",
            ],
        },
        {
            "id": "project_health",
            "label": "Project health",
            "enabled": False,
            "command": ["hermes", "chat", "-q", "Review the current project status and summarize health risks."],
        },
        {
            "id": "daily_brief",
            "label": "Daily brief",
            "enabled": False,
            "command": ["hermes", "chat", "-q", "Prepare a short daily operational brief."],
        },
    ]
}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def default_state_db() -> Path:
    return hermes_home() / "state.db"


def default_config_path() -> Path:
    return hermes_home() / "familiar_actions.json"


def compact(s: str, n: int = 120) -> str:
    s = " ".join((s or "").replace("\x00", "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def is_attention_text(role: str, content: str) -> bool:
    if role != "assistant" or not content:
        return False
    low = content.lower()
    return any(w in low for w in ATTENTION_WORDS)


@dataclass
class Snapshot:
    payload: dict[str, Any]
    latest_message_id: int | None
    latest_role: str | None
    latest_content: str


class ApiRunClient:
    def __init__(self, base_url: str | None, api_key: str | None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}

    def stream_events(self, run_id: str):
        req = urllib.request.Request(
            self.base_url + f"/v1/runs/{run_id}/events",
            method="GET",
            headers={"Accept": "text/event-stream", "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            event_name = "message"
            data_lines: list[str] = []
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        data = "\n".join(data_lines)
                        try:
                            payload: Any = json.loads(data)
                        except Exception:
                            payload = {"text": data}
                        yield event_name, payload
                    event_name = "message"
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())

    def capabilities(self) -> dict[str, Any]:
        return self.request("GET", "/v1/capabilities")

    def start(self, prompt: str, session_id: str | None = None) -> str:
        payload: dict[str, Any] = {"input": prompt}
        if session_id:
            payload["session_id"] = session_id
        res = self.request("POST", "/v1/runs", payload)
        return str(res.get("run_id") or "")

    def status(self, run_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/runs/{run_id}")

    def stop(self, run_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/runs/{run_id}/stop", {})

    def approve(self, run_id: str, choice: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/runs/{run_id}/approval", {"choice": choice})

    def list_jobs(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        suffix = "?include_disabled=true" if include_disabled else ""
        res = self.request("GET", f"/api/jobs{suffix}")
        return list(res.get("jobs") or res.get("data") or [])

    def run_job(self, job_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/jobs/{job_id}/run", {})

    def pause_job(self, job_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/jobs/{job_id}/pause", {})

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/jobs/{job_id}/resume", {})


@dataclass
class ApiRunJob:
    action_id: str
    label: str
    run_id: str
    client: ApiRunClient
    events: Queue[dict[str, Any]] = field(default_factory=Queue)
    started_at: float = field(default_factory=time.time)
    last_status: str = "started"

    def start_event_stream(self) -> None:
        def _worker() -> None:
            try:
                for name, payload in self.client.stream_events(self.run_id):
                    ev = self._device_event(name, payload if isinstance(payload, dict) else {})
                    if ev:
                        self.events.put(ev)
            except Exception as e:
                self.events.put({"type": "event", "event": "stream_error", "msg": compact(str(e), 120)})

        threading.Thread(target=_worker, name=f"hermes-run-events-{self.run_id}", daemon=True).start()

    @staticmethod
    def _device_event(name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if name in {"assistant.delta", "response.output_text.delta"}:
            delta = payload.get("delta") or payload.get("text") or ""
            if delta:
                return {"type": "event", "event": "delta", "msg": compact(str(delta), 120)}
        if name in {"tool.started", "tool.completed", "tool.failed", "hermes.tool.progress"} or str(name).startswith("tool."):
            tool = payload.get("tool_name") or payload.get("tool") or payload.get("name") or "tool"
            return {"type": "event", "event": name.replace(".", "_"), "msg": compact(str(tool), 80)}
        if "approval" in name or payload.get("approval") or payload.get("pending_approval"):
            text = payload.get("text") or payload.get("message") or payload.get("preview") or "Hermes needs approval"
            return {"type": "permission", "id": str(payload.get("id") or payload.get("approval_id") or ""), "text": compact(str(text), 140), "choices": ["once", "deny"]}
        if name in {"run.started", "run.completed", "run.failed", "run.cancelled", "error"}:
            msg = payload.get("message") or payload.get("output") or name
            return {"type": "event", "event": name.replace(".", "_"), "msg": compact(str(msg), 140)}
        return None

    def poll(self) -> int | None:
        try:
            st = self.client.status(self.run_id)
            self.last_status = str(st.get("status") or self.last_status)
            if self.last_status in {"completed"}:
                return 0
            if self.last_status in {"failed", "cancelled", "stopped"}:
                return 1
        except Exception:
            return None
        return None

    def read_tail(self) -> None:
        return

    @property
    def output_tail(self) -> str:
        return compact(f"api run {self.run_id} {self.last_status}", 180)


@dataclass
class ActionJob:
    action_id: str
    label: str
    proc: subprocess.Popen[str]
    started_at: float = field(default_factory=time.time)
    paused: bool = False
    output_tail: str = ""

    def poll(self) -> int | None:
        return self.proc.poll()

    def read_tail(self) -> None:
        # We launch stderr into stdout. Nonblocking reads from PIPE are awkward
        # without selectors on macOS; defer full log display to process exit.
        if self.proc.poll() is not None and self.proc.stdout:
            try:
                out = self.proc.stdout.read() or ""
                self.output_tail = compact(out, 180)
            except Exception:
                pass


class JobManager:
    def __init__(self, config_path: Path, workdir: Path | None = None, api_client: ApiRunClient | None = None):
        self.config_path = config_path
        self.workdir = workdir
        self.api_client = api_client
        self.actions = self._load_actions()
        self.job: ActionJob | ApiRunJob | None = None
        self.last_event: dict[str, Any] | None = None
        self.event_queue: Queue[dict[str, Any]] = Queue()

    def _load_actions(self) -> list[dict[str, Any]]:
        if not self.config_path.exists():
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(DEFAULT_ACTIONS, indent=2) + "\n")
        try:
            data = json.loads(self.config_path.read_text())
            actions = data.get("actions", [])
            return [a for a in actions if isinstance(a, dict)]
        except Exception as e:
            print(f"failed to read action config {self.config_path}: {e}", file=sys.stderr)
            return DEFAULT_ACTIONS["actions"]

    def status(self) -> dict[str, Any]:
        if self.job:
            rc = self.job.poll()
            if rc is not None:
                self.job.read_tail()
                self.last_event = {
                    "type": "event",
                    "event": "job_done" if rc == 0 else "job_failed",
                    "msg": f"{self.job.label} rc={rc} {self.job.output_tail}",
                }
                self.job = None
        if not self.job:
            enabled = [a for a in self.actions if a.get("enabled")]
            label = enabled[0].get("label", "No action") if enabled else "No enabled action"
            return {"job_state": "idle", "job_label": label}
        state = getattr(self.job, "last_status", None) if isinstance(self.job, ApiRunJob) else ("paused" if self.job.paused else "running")
        return {
            "job_state": str(state or "running"),
            "job_label": self.job.label,
            "job_age": int(time.time() - self.job.started_at),
        }

    def pop_event(self) -> dict[str, Any] | None:
        try:
            return self.event_queue.get_nowait()
        except Empty:
            pass
        if isinstance(self.job, ApiRunJob):
            try:
                return self.job.events.get_nowait()
            except Empty:
                pass
        ev, self.last_event = self.last_event, None
        return ev

    @staticmethod
    def _action_type(action: dict[str, Any]) -> str:
        kind = str(action.get("type") or action.get("kind") or "").lower().strip()
        if kind:
            return kind
        if action.get("job_id") or action.get("cron_job_id"):
            return "cron_job"
        if action.get("prompt"):
            return "run"
        cmd = action.get("command")
        if isinstance(cmd, list) and "-q" in cmd:
            return "run"
        return "command"

    @staticmethod
    def _prompt_from_action(action: dict[str, Any]) -> str | None:
        prompt = action.get("prompt")
        cmd = action.get("command")
        if not prompt and isinstance(cmd, list) and "-q" in cmd:
            idx = cmd.index("-q")
            if idx + 1 < len(cmd):
                prompt = cmd[idx + 1]
        return prompt if isinstance(prompt, str) and prompt.strip() else None

    @staticmethod
    def _job_id_from_action(action: dict[str, Any]) -> str | None:
        job_id = action.get("job_id") or action.get("cron_job_id")
        return str(job_id).strip() if job_id else None

    def _first_enabled(self, *types: str) -> dict[str, Any] | None:
        wanted = set(types)
        for action in self.actions:
            if action.get("enabled") and (not wanted or self._action_type(action) in wanted):
                return action
        return None

    def start_first_enabled(self) -> dict[str, Any]:
        st = self.status()
        if st.get("job_state") in ("running", "paused"):
            return {"type": "ack", "msg": f"job already {st['job_state']}"}
        action = self._first_enabled()
        if not action:
            return {"type": "ack", "msg": "no enabled action"}
        cmd = action.get("command")
        action_type = self._action_type(action)
        if self.api_client and self.api_client.enabled and action_type in {"run", "api_run"}:
            prompt = self._prompt_from_action(action)
            if not prompt:
                return {"type": "ack", "msg": "no API prompt for action"}
            try:
                run_id = self.api_client.start(prompt, session_id=f"familiar_{action.get('id', 'action')}_{int(time.time())}")
                if not run_id:
                    return {"type": "ack", "msg": "API run missing id"}
                self.job = ApiRunJob(action.get("id", "action"), action.get("label", "Action"), run_id, self.api_client, events=self.event_queue)
                self.job.start_event_stream()
                return {"type": "ack", "msg": f"started API: {self.job.label}"}
            except Exception as e:
                return {"type": "ack", "msg": f"API start failed: {compact(str(e), 60)}"}
        if self.api_client and self.api_client.enabled and action_type in {"cron_job", "job", "api_job"}:
            job_id = self._job_id_from_action(action)
            if not job_id:
                return {"type": "ack", "msg": "no cron job_id for action"}
            try:
                res = self.api_client.run_job(job_id)
                job = res.get("job") if isinstance(res, dict) else None
                label = (job or {}).get("name") or action.get("label") or job_id
                self.last_event = {"type": "event", "event": "cron_job_run", "msg": f"triggered {compact(str(label), 80)}"}
                return {"type": "ack", "msg": f"triggered job: {compact(str(label), 80)}"}
            except Exception as e:
                return {"type": "ack", "msg": f"job run failed: {compact(str(e), 60)}"}
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            return {"type": "ack", "msg": "bad action command"}
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.workdir) if self.workdir else None,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.job = ActionJob(action.get("id", "action"), action.get("label", "Action"), proc)
            return {"type": "ack", "msg": f"started: {self.job.label}"}
        except Exception as e:
            return {"type": "ack", "msg": f"start failed: {compact(str(e), 60)}"}

    def toggle_pause(self) -> dict[str, Any]:
        self.status()
        if not self.job or self.job.poll() is not None:
            return {"type": "ack", "msg": "no running job"}
        try:
            if isinstance(self.job, ApiRunJob):
                return {"type": "ack", "msg": "API pause unavailable; use cancel"}
            if self.job.paused:
                os.kill(self.job.proc.pid, signal.SIGCONT)
                self.job.paused = False
                return {"type": "ack", "msg": "job resumed"}
            os.kill(self.job.proc.pid, signal.SIGSTOP)
            self.job.paused = True
            return {"type": "ack", "msg": "job paused"}
        except Exception as e:
            return {"type": "ack", "msg": f"pause failed: {compact(str(e), 60)}"}

    def pause_or_resume_configured_job(self) -> dict[str, Any]:
        action = self._first_enabled("cron_job", "job", "api_job")
        if not action or not self.api_client or not self.api_client.enabled:
            return self.toggle_pause()
        job_id = self._job_id_from_action(action)
        if not job_id:
            return {"type": "ack", "msg": "no cron job_id for action"}
        try:
            jobs = self.api_client.list_jobs(include_disabled=True)
            job = next((j for j in jobs if str(j.get("id")) == job_id), None)
            enabled = True if job is None else bool(job.get("enabled", True))
            res = self.api_client.pause_job(job_id) if enabled else self.api_client.resume_job(job_id)
            updated = res.get("job") if isinstance(res, dict) else None
            label = (updated or job or {}).get("name") or action.get("label") or job_id
            return {"type": "ack", "msg": f"{'paused' if enabled else 'resumed'} job: {compact(str(label), 70)}"}
        except Exception as e:
            return {"type": "ack", "msg": f"job pause/resume failed: {compact(str(e), 60)}"}

    def cancel(self) -> dict[str, Any]:
        self.status()
        if not self.job or self.job.poll() is not None:
            return {"type": "ack", "msg": "no running job"}
        try:
            if isinstance(self.job, ApiRunJob):
                self.job.client.stop(self.job.run_id)
                return {"type": "ack", "msg": "API stop sent"}
            self.job.proc.terminate()
            return {"type": "ack", "msg": "job cancel sent"}
        except Exception as e:
            return {"type": "ack", "msg": f"cancel failed: {compact(str(e), 60)}"}


def read_snapshot(db_path: Path, jobs: JobManager | None = None) -> Snapshot:
    now = time.time()
    since = now - 24 * 3600
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    try:
        sessions = con.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(output_tokens), 0) AS tokens_today,
                   COALESCE(SUM(tool_call_count), 0) AS tools_today
            FROM sessions
            WHERE started_at >= ?
            """,
            (since,),
        ).fetchone()
        rows = con.execute(
            """
            SELECT m.id, m.timestamp, m.role, m.content, m.tool_name,
                   m.tool_calls, s.title, s.source
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.timestamp >= ?
              AND m.role IN ('user', 'assistant')
              AND m.content IS NOT NULL
              AND length(trim(m.content)) > 0
              AND COALESCE(m.content, '') NOT LIKE '[IMPORTANT: You are running as a scheduled cron job.%'
            ORDER BY m.timestamp DESC
            LIMIT 12
            """,
            (since,),
        ).fetchall()
    finally:
        con.close()

    entries: list[str] = []
    latest = rows[0] if rows else None
    latest_id = int(latest["id"]) if latest else None
    latest_role = latest["role"] if latest else None
    latest_content = ""
    latest_user_ts = 0.0
    latest_assistant_ts = 0.0
    for r in rows:
        tsf = float(r["timestamp"] or 0)
        ts = datetime.fromtimestamp(tsf).strftime("%H:%M")
        role = r["role"]
        body = r["content"] or ""
        if role == "user" and not latest_user_ts:
            latest_user_ts = tsf
        if role == "assistant" and not latest_assistant_ts:
            latest_assistant_ts = tsf
        entries.append(f"{ts} {role[0]}: {compact(body, 70)}")
        if r is latest:
            latest_content = compact(body, 140)

    running = int(latest_user_ts > latest_assistant_ts and (now - latest_user_ts) < 120)
    waiting = int(bool(latest and is_attention_text(latest_role or "", latest["content"] or "")))
    job_status = jobs.status() if jobs else {"job_state": "idle", "job_label": ""}
    if job_status.get("job_state") in ("running", "paused"):
        running = 1

    mood = "waiting" if waiting else ("thinking" if running else "idle")
    msg = latest_content or "Hermes is idle"
    payload = {
        "type": "state",
        "total": int(sessions["total"] or 0),
        "running": running,
        "waiting": waiting,
        "mood": mood,
        "msg": msg,
        "entries": entries[:5],
        "tokens_today": int(sessions["tokens_today"] or 0),
        "tools_today": int(sessions["tools_today"] or 0),
        "latest_id": latest_id or 0,
        **job_status,
    }
    return Snapshot(payload, latest_id, latest_role, latest_content)


def send_json(ser, obj: dict[str, Any]) -> None:
    ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    ser.flush()


def handle_device_line(ser, jobs: JobManager, text: str) -> None:
    print("device>", text)
    try:
        evt = json.loads(text)
    except Exception:
        return

    cmd = evt.get("cmd") or evt.get("event")
    if cmd == "touch":
        x, y = evt.get("x"), evt.get("y")
        send_json(ser, {"type": "ack", "msg": f"touch {x},{y}"})
    elif cmd == "swipe":
        dx, dy = evt.get("dx"), evt.get("dy")
        send_json(ser, {"type": "ack", "msg": f"swipe {dx},{dy}"})
    elif cmd == "gesture":
        gesture = evt.get("gesture", "")
        if gesture == "shake":
            send_json(ser, {"type": "ack", "msg": "shake: status refresh"})
            send_json(ser, {"type": "event", "event": "gesture", "msg": "Device shake received — Hermes Familiar is awake"})
        else:
            send_json(ser, {"type": "ack", "msg": f"gesture {gesture}"})
    elif cmd == "action":
        action = evt.get("action")
        if action == "start":
            send_json(ser, jobs.start_first_enabled())
        elif action == "pause":
            send_json(ser, jobs.pause_or_resume_configured_job())
        elif action == "cancel":
            send_json(ser, jobs.cancel())
        else:
            send_json(ser, {"type": "ack", "msg": f"unknown action {action}"})
    elif cmd == "permission":
        decision = evt.get("decision", "once")
        if jobs.job and isinstance(jobs.job, ApiRunJob):
            try:
                jobs.job.client.approve(jobs.job.run_id, str(decision))
                send_json(ser, {"type": "ack", "msg": f"approval sent: {decision}", "waiting": 0})
            except Exception as e:
                send_json(ser, {"type": "ack", "msg": f"approval failed: {compact(str(e), 60)}"})
        else:
            # CLI fallback cannot resolve native Hermes approvals; record intent only.
            send_json(ser, {"type": "ack", "msg": f"decision seen: {decision}", "waiting": 0})
    elif isinstance(cmd, str) and cmd.startswith("local_"):
        pass
    elif cmd:
        send_json(ser, {"type": "ack", "msg": f"cmd seen: {cmd}"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbmodem101")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--db", type=Path, default=default_state_db())
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--actions", type=Path, default=default_config_path())
    ap.add_argument("--workdir", type=Path, default=Path.cwd())
    ap.add_argument("--api-url", default=os.environ.get("HERMES_API_URL", ""), help="Optional Hermes API server base URL, e.g. http://127.0.0.1:8642")
    ap.add_argument("--api-key", default=os.environ.get("API_SERVER_KEY", ""), help="Optional API server bearer key; enables native /v1/runs start/stop/approval")
    args = ap.parse_args()

    try:
        import serial
    except ImportError:
        print("pyserial is required: python3 -m pip install pyserial", file=sys.stderr)
        return 2

    if not args.db.exists():
        print(f"Hermes state DB not found: {args.db}", file=sys.stderr)
        return 1

    api_client = ApiRunClient(args.api_url, args.api_key)
    jobs = JobManager(args.actions, args.workdir, api_client)
    last_latest_id: int | None = None
    with serial.Serial(args.port, args.baud, timeout=0.05) as ser:
        mode = "api" if api_client.enabled else "cli"
        print(f"connected to {args.port}; reading {args.db}; actions {args.actions}; mode {mode}")
        send_json(ser, {"cmd": "ping"})
        while True:
            snap = read_snapshot(args.db, jobs)
            send_json(ser, snap.payload)
            ev = jobs.pop_event()
            if ev:
                send_json(ser, ev)
            if snap.latest_message_id and snap.latest_message_id != last_latest_id:
                last_latest_id = snap.latest_message_id
                send_json(
                    ser,
                    {
                        "type": "event",
                        "event": "message",
                        "role": snap.latest_role or "",
                        "msg": compact(snap.latest_content, 120),
                    },
                )

            deadline = time.time() + args.interval
            while time.time() < deadline:
                rx = ser.readline()
                if rx:
                    handle_device_line(ser, jobs, rx.decode("utf-8", "replace").rstrip())
                time.sleep(0.02)


if __name__ == "__main__":
    raise SystemExit(main())
