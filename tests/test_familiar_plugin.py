"""Plugin-mode tests: hook wiring, state frames, approvals, job control.

No pyserial, no gateway, no device — a FakeLink captures every frame the
hooks would push, and the approval resolver is monkeypatched where the real
one needs the gateway process.
"""
from __future__ import annotations

import json
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
        self.tools: dict[str, dict] = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"schema": schema, "handler": handler, **kw}


class FakeLink:
    port = "/dev/fake"
    connected = True
    transport = "usb:/dev/fake"

    def __init__(self):
        self.sent: list[dict] = []
        self.legs: list = []   # parallel to sent: None | "desk" | "phone"

    def send(self, obj, leg=None):
        self.sent.append(obj)
        self.legs.append(leg)

    def frames(self, type_, leg="any"):
        return [f for f, l in zip(self.sent, self.legs)
                if f.get("type") == type_ and (leg == "any" or l == leg)]


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Registered plugin with fake ctx + fake link, isolated HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = FakeCtx()
    familiar.register(ctx)                       # not a gateway argv -> no serial
    link = FakeLink()
    monkeypatch.setattr(familiar, "_link", link)
    monkeypatch.setattr(familiar, "_voice_enabled", False)  # no real TTS/server in tests
    monkeypatch.setattr(familiar, "_stats", {"total": 0, "tokens_today": 0,
                                             "tools_today": 0, "at": time.time()})
    familiar._turns.clear()
    familiar._pending.clear()
    familiar._entries.clear()
    familiar._presence.update({"desk": 0.0, "phone": 0.0})
    familiar._telemetry.clear()
    monkeypatch.setattr(familiar, "_desk_quiet", False)
    monkeypatch.setattr(familiar, "_batt_warned", False)
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


def test_deck_frame_shapes_buttons():
    frame = actions.deck_frame([
        {"id": "a", "label": "morning brief", "enabled": True, "color": "cyan"},
        {"id": "b", "label": "REDEPLOY SITE NOW", "enabled": True, "confirm": True, "color": "bogus"},
        {"id": "c", "label": "off", "enabled": False},
    ])
    assert frame["type"] == "deck"
    assert frame["buttons"] == [
        {"i": 0, "label": "MORNING", "color": "cyan", "confirm": False},
        {"i": 1, "label": "REDEPLOY", "color": "green", "confirm": True},
    ]


def test_deck_start_by_index_and_toggle_cancel():
    jm = actions.JobManager([
        {"id": "a", "label": "A", "enabled": True, "command": ["sleep", "5"]},
        {"id": "b", "label": "B", "enabled": True, "prompt": "do the thing"},
    ])
    assert "no action 7" in jm.start_by_index(7)["msg"]
    assert jm.start_by_index(0)["msg"] == "started: A"
    assert jm.status()["job_index"] == 0
    assert "busy" in jm.start_by_index(1)["msg"]          # one job at a time
    assert jm.start_by_index(0)["msg"] == "job cancel sent"  # same button = stop
    deadline = time.time() + 3
    while jm.active and time.time() < deadline:
        time.sleep(0.05)
    assert jm.status()["job_index"] == -1


def test_deck_hot_reloads_when_config_changes(monkeypatch, tmp_path):
    import json as _json
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path / "familiar_actions.json"
    cfg.write_text(_json.dumps({"actions": [
        {"id": "a", "label": "A", "enabled": True, "prompt": "x"}]}))
    jm = actions.JobManager()
    assert len(jm.actions) == 1
    assert jm.reload_if_changed() is False       # unchanged
    # rewrite with a newer mtime + a second button
    cfg.write_text(_json.dumps({"actions": [
        {"id": "a", "label": "A", "enabled": True, "prompt": "x"},
        {"id": "b", "label": "B", "enabled": True, "prompt": "y"}]}))
    import os as _os
    future = cfg.stat().st_mtime + 10
    _os.utime(cfg, (future, future))
    assert jm.reload_if_changed() is True
    assert len(jm.actions) == 2
    # a running job blocks reload (index must stay valid)
    jm.actions = [{"id": "s", "label": "S", "enabled": True, "command": ["sleep", "3"]}]
    jm.start_by_index(0)
    cfg.write_text(_json.dumps({"actions": []}))
    _os.utime(cfg, (future + 20, future + 20))
    assert jm.reload_if_changed() is False       # busy — deferred
    jm.cancel()


def test_job_output_is_captured_and_popped():
    import time as _t
    jm = actions.JobManager([{"id":"e","label":"ECHO","enabled":True,
                              "command":["/bin/echo","hello from the deck"]}])
    jm.start_by_index(0)
    deadline = _t.time() + 3
    while jm.active and _t.time() < deadline:
        _t.sleep(0.05)
    jm.status()   # reaps the finished job, captures output
    res = jm.pop_result()
    assert res is not None
    assert res["rc"] == 0
    assert "hello from the deck" in res["text"]
    assert jm.pop_result() is None   # cleared after pop


def test_clean_output_extracts_hermes_answer():
    raw = ("Warning: Unknown toolsets: mcp-codegraph\n"
           "Query: brief please\nInitializing agent...\n───────\n\n"
           "╭─ ⚕ Hermes ─────╮\n    The real answer line one.\n\n"
           "    And line two.\n╰──────╯\n\nResume this session with:\n"
           "  hermes --resume 123\nSession: 123")
    out = actions.clean_output(raw)
    assert out == "The real answer line one. And line two."
    assert "Warning" not in out and "Query" not in out
    # plain command output (no box) passes through, noise stripped
    assert actions.clean_output("goodnight Sat Jul 5") == "goodnight Sat Jul 5"


def test_prompt_actions_become_hermes_chat_commands():
    cmd = actions._action_command({"prompt": "say hi"})
    assert cmd == ["hermes", "chat", "-Q", "-q", "say hi"]
    assert actions._action_command({"label": "empty"}) is None


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


def test_user_message_lands_on_ticker(wired):
    ctx, link = wired
    ctx.hooks["pre_llm_call"](platform="telegram", session_id="s2",
                              user_message="What's my schedule?")
    assert any("u: What's my schedule?" in e
               for e in link.frames("state")[-1]["entries"])


def test_notify_tool_sends_banner_frame(wired):
    ctx, link = wired
    out = json.loads(familiar._tool_notify({"message": "Backup finished", "sound": "ack"}))
    assert out["success"] is True
    frame = [f for f in link.sent if f.get("type") == "notify"][-1]
    assert frame == {"type": "notify", "msg": "Backup finished", "sound": "ack"}
    assert "familiar_notify" in ctx.tools


def test_notify_tool_speak_attaches_clip_url(wired, monkeypatch):
    _, link = wired
    monkeypatch.setattr(familiar, "_say_url", lambda t: "http://10.0.0.5:8765/x.pcm")
    out = json.loads(familiar._tool_notify({"message": "Build done", "speak": True}))
    assert out["spoke"] is True
    frame = [f for f in link.sent if f.get("type") == "notify"][-1]
    assert frame["say"] == "http://10.0.0.5:8765/x.pcm"
    assert frame["sound"] == "none"   # voice replaces the chirp


def test_notify_tool_errors_without_device(wired, monkeypatch):
    monkeypatch.setattr(familiar, "_link", None)
    out = json.loads(familiar._tool_notify({"message": "hello"}))
    assert "not connected" in out["error"]


def test_kanban_blocked_raises_desk_alert(wired):
    ctx, link = wired
    ctx.hooks["kanban_task_blocked"](task_id="T-9", reason="needs prod creds")
    frame = [f for f in link.sent if f.get("type") == "notify"][-1]
    assert "needs prod creds" in frame["msg"]
    ctx.hooks["kanban_task_completed"](task_id="T-8", summary="deployed ok")
    assert any("k: done deployed ok" in e
               for e in link.frames("state")[-1]["entries"])


def test_cron_page_formats_jobs(monkeypatch, tmp_path):
    from plugin import feeds
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "Mail watcher", "enabled": True, "state": "active",
         "last_status": "ok", "next_run_at": time.time() + 3600},
        {"name": "Paused thing", "enabled": True, "state": "paused"},
        {"name": "Disabled thing", "enabled": False},
    ]}))
    page = feeds.cron_page()
    assert page["slot"] == 0
    assert page["title"] == "CRON: 1 ACTIVE"
    assert len(page["lines"]) == 1
    assert "Mail watcher ok" in page["lines"][0]


def test_vitals_page_reads_gateway_state(monkeypatch, tmp_path):
    from plugin import feeds
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "gateway_state.json").write_text(json.dumps({
        "platforms": {"telegram": {"state": "connected"},
                      "slack": {"state": "error"}}}))
    page = feeds.vitals_page(time.time() - 7500,
                             {"total": 12, "tokens_today": 48200, "tools_today": 7})
    assert page["slot"] == 1
    assert "up 2h05m" in page["lines"][0]
    assert "te:ok" in page["lines"][0] and "sl:ER" in page["lines"][0]
    assert "S:12 tools:7 tok:48k" == page["lines"][1]


# -- presence routing + telemetry (hardware pass, 2026-07-06) ---------------

def _notify(link, msg="ping"):
    return familiar._tool_notify({"message": msg})


def test_notify_loud_everywhere_when_presence_unknown(wired):
    _, link = wired
    _notify(link)
    desk = link.frames("notify", leg="desk")
    phone = link.frames("notify", leg="phone")
    assert desk and phone
    assert desk[0]["sound"] == "alert" and phone[0]["sound"] == "alert"


def test_notify_desk_silent_when_phone_active(wired):
    _, link = wired
    familiar._handle_device_line({"cmd": "deck", "i": 99}, origin="ws")  # phone interaction
    _notify(link)
    assert link.frames("notify", leg="desk")[0]["sound"] == "none"
    assert link.frames("notify", leg="phone")[0]["sound"] == "alert"


def test_notify_desk_loud_when_desk_freshest(wired):
    _, link = wired
    familiar._handle_device_line({"cmd": "deck", "i": 99}, origin="ws")
    familiar._handle_device_line({"cmd": "touch", "x": 1, "y": 1}, origin="usb")  # desk wins
    _notify(link)
    assert link.frames("notify", leg="desk")[0]["sound"] == "alert"


def test_facedown_gesture_mutes_desk(wired):
    _, link = wired
    familiar._handle_device_line({"cmd": "gesture", "gesture": "facedown", "quiet": True})
    assert familiar._desk_quiet is True
    _notify(link)
    assert link.frames("notify", leg="desk")[0]["sound"] == "none"
    familiar._handle_device_line({"cmd": "gesture", "gesture": "upright", "quiet": False})
    assert familiar._desk_quiet is False


def test_approval_quiet_flag_when_phone_active(wired):
    ctx, link = wired
    familiar._handle_device_line({"cmd": "deck", "i": 99}, origin="ws")
    ctx.hooks["pre_approval_request"](command="rm -rf /x", session_key="sk1")
    desk = link.frames("permission", leg="desk")
    phone = link.frames("permission", leg="phone")
    assert desk[0].get("quiet") is True
    assert "quiet" not in phone[0]


def test_approval_loud_by_default(wired):
    ctx, link = wired
    ctx.hooks["pre_approval_request"](command="rm -rf /x", session_key="sk2")
    assert "quiet" not in link.frames("permission", leg="desk")[0]


def test_low_battery_warns_once_per_crossing(wired):
    _, link = wired
    tele = {"cmd": "telemetry", "bat": 3.4, "quiet": False, "usb": False}
    familiar._handle_device_line(dict(tele))
    familiar._handle_device_line(dict(tele))          # still low -> no second warn
    warns = [f for f in link.frames("notify") if "battery low" in f["msg"]]
    assert len(warns) == 1
    familiar._handle_device_line({"cmd": "telemetry", "bat": 4.0, "quiet": False, "usb": True})
    familiar._handle_device_line(dict(tele))          # new crossing -> warn again
    warns = [f for f in link.frames("notify") if "battery low" in f["msg"]]
    assert len(warns) == 2


def test_state_frame_carries_battery_and_transport(wired):
    _, link = wired
    familiar._handle_device_line({"cmd": "telemetry", "bat": 4.02, "quiet": False, "usb": True})
    frame = familiar._payload()
    assert frame["battery_v"] == 4.02
    assert frame["transport"] == "usb:/dev/fake"
