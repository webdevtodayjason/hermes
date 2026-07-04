"""Plugin-mode tests: hook wiring, state frames, approvals, job control.

No pyserial, no gateway, no device — a FakeLink captures every frame the
hooks would push, and the approval resolver is monkeypatched where the real
one needs the gateway process.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin as familiar
from plugin import actions


class FakeCtx:
    def __init__(self):
        self.hooks: dict[str, object] = {}
        self.commands: dict[str, object] = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler


class FakeLink:
    port = "/dev/fake"
    connected = True

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, obj):
        self.sent.append(obj)

    def frames(self, type_):
        return [f for f in self.sent if f.get("type") == type_]


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Registered plugin with fake ctx + fake link, isolated HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = FakeCtx()
    familiar.register(ctx)                       # not a gateway argv -> no serial
    link = FakeLink()
    monkeypatch.setattr(familiar, "_link", link)
    monkeypatch.setattr(familiar, "_stats", {"total": 0, "tokens_today": 0,
                                             "tools_today": 0, "at": time.time()})
    familiar._turns.clear()
    familiar._pending.clear()
    familiar._entries.clear()
    yield ctx, link


def test_register_wires_hooks_and_command(wired):
    ctx, _ = wired
    for hook in ("pre_llm_call", "post_llm_call", "pre_tool_call", "post_tool_call",
                 "on_session_start", "on_session_end",
                 "pre_approval_request", "post_approval_response"):
        assert hook in ctx.hooks
    assert "familiar" in ctx.commands
    assert familiar._link is not None  # fake injected; real link is gateway-only


def test_turn_lifecycle_drives_running_flag(wired):
    ctx, link = wired
    ctx.hooks["pre_llm_call"](platform="telegram", session_id="s1")
    assert link.frames("state")[-1]["running"] == 1
    assert "thinking" in link.frames("state")[-1]["msg"]

    ctx.hooks["pre_tool_call"](tool_name="web_search", session_id="s1")
    assert link.frames("state")[-1]["msg"] == "searching the web…"

    ctx.hooks["post_llm_call"](assistant_response="All done, boss.",
                               platform="telegram", session_id="s1")
    last = link.frames("state")[-1]
    assert last["running"] == 0
    assert any("All done" in e for e in last["entries"])
    assert link.frames("event")[-1]["event"] == "message"


def test_approval_flow_pushes_permission_and_resolves(wired, monkeypatch):
    ctx, link = wired
    ctx.hooks["pre_approval_request"](command="rm -rf /tmp/x",
                                      description="Recursive delete",
                                      session_key="sess-9", surface="gateway")
    perm = [f for f in link.sent if f.get("type") == "permission"][-1]
    assert perm["id"] == "sess-9"
    assert perm["choices"] == ["once", "deny"]
    assert link.frames("state")[-1]["waiting"] == 1

    calls = []
    monkeypatch.setattr(actions, "_resolve_approval",
                        lambda key, choice: calls.append((key, choice)) or 1)
    familiar._handle_device_line({"cmd": "permission", "decision": "deny", "id": "sess-9"})
    assert calls == [("sess-9", "deny")]

    ctx.hooks["post_approval_response"](session_key="sess-9", choice="deny")
    assert link.frames("state")[-1]["waiting"] == 0


def test_device_permission_with_no_pending_is_acked(wired):
    _, link = wired
    familiar._handle_device_line({"cmd": "permission", "decision": "once"})
    assert link.sent[-2]["msg"] == "no pending approval"


def test_job_manager_start_pause_cancel():
    jm = actions.JobManager([{"id": "t", "label": "Sleeper", "enabled": True,
                              "command": ["sleep", "5"]}])
    assert jm.start_first_enabled()["msg"] == "started: Sleeper"
    assert jm.active and jm.status()["job_state"] == "running"
    assert jm.toggle_pause()["msg"] == "job paused"
    assert jm.status()["job_state"] == "paused"
    assert jm.cancel()["msg"] == "job cancel sent"
    deadline = time.time() + 3
    while jm.active and time.time() < deadline:
        time.sleep(0.05)
    assert jm.status()["job_state"] == "idle"


def test_default_config_written_and_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = actions.load_config()
    assert cfg["actions"][0]["id"] == "status_brief"
    assert (tmp_path / "familiar_actions.json").exists()
