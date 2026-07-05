"""Loom bell diffing + evening ritual gate/compose — pure filesystem tests."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugin import loom_bell, ritual


@pytest.fixture()
def board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    b = tmp_path / "board"
    (b / "HANDOFFS").mkdir(parents=True)
    (b / "JOURNAL.md").write_text(
        "# Journal\n2026-07-04 · code@alpha · did the first thing\n")
    (b / "HANDOFFS" / "_TEMPLATE.md").write_text("template")
    (b / "REGISTRY.md").write_text(
        "## Active Slices\n\n"
        "| Slice | Thread | Surface | Started | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| alpha-work | t1 | repo@Mac | 2026-07-04 | ACTIVE — building |\n"
        "\n## Completed Slices\n")
    return b


def test_first_run_seeds_silently(board):
    assert loom_bell.check(str(board)) == []


def test_detects_handoff_done_and_journal(board):
    loom_bell.check(str(board))  # seed

    with open(board / "JOURNAL.md", "a") as f:
        f.write("2026-07-04 · code@alpha · shipped the second thing\n")
    (board / "HANDOFFS" / "2026-07-04T2300Z-alpha-work-done.md").write_text("h")
    reg = (board / "REGISTRY.md").read_text().replace(
        "ACTIVE — building", "✅ DONE 2026-07-04 — shipped")
    (board / "REGISTRY.md").write_text(reg)

    events = loom_bell.check(str(board))
    kinds = {e["kind"] for e in events}
    assert kinds == {"journal", "handoff", "slice_done"}
    handoff = next(e for e in events if e["kind"] == "handoff")
    assert handoff["speak"] and "alpha-work-done" in handoff["text"]
    assert next(e for e in events if e["kind"] == "slice_done")["speak"]
    assert "shipped the second thing" in next(
        e for e in events if e["kind"] == "journal")["text"]

    assert loom_bell.check(str(board)) == []  # no re-announce


def test_ritual_due_once_per_day(board, monkeypatch):
    now = datetime(2026, 7, 4, 19, 0)
    assert ritual.due(now, "18:30") is True
    ritual.mark_done(now)
    assert ritual.due(now, "18:30") is False
    assert ritual.due(datetime(2026, 7, 5, 18, 31), "18:30") is True
    assert ritual.due(datetime(2026, 7, 5, 9, 0), "18:30") is False


def test_ritual_gather_and_template_compose(board, monkeypatch):
    closet = board / "closet.md"
    closet.write_text(
        "- [ ] **Plug in a speaker.** words (added 2026-07-04)\n"
        "- [x] **Old thing.** done (added 2026-07-04)\n"
        "- [ ] **Ancient.** (added 2026-06-01)\n")
    monkeypatch.setattr(ritual, "CLOSET", closet)
    m = ritual.gather(datetime(2026, 7, 4, 18, 30),
                      {"total": 7, "tools_today": 42, "tokens_today": 9000},
                      str(board))
    assert m["followups_filed"] == ["Plug in a speaker"]
    assert m["followups_closed_today"] == 1
    assert len(m["journal"]) == 1
    text = ritual.compose(m, llm=None)
    assert "7 Hermes sessions" in text and "Plug in a speaker" in text


def test_fleet_page_reads_kanban(monkeypatch, tmp_path):
    import sqlite3, time as _t
    from plugin import feeds
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    con = sqlite3.connect(tmp_path / "kanban.db")
    con.execute("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT,"
                " started_at INTEGER, completed_at INTEGER)")
    now = _t.time()
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)", [
        ("t1", "fix the widget", "w1", "in_progress", now - 60, None),
        ("t2", "old thing", "w2", "done", now - 7200, now - 3600),
        ("t3", "waiting task", None, "queued", None, None),
    ])
    con.commit(); con.close()
    page = feeds.fleet_page()
    assert page["slot"] == 2
    assert page["title"] == "FLEET: ACTIVE"
    assert "run:1 queue:1 blocked:0 done24h:1" in page["lines"][0]
    assert "w1: fix the widget" in page["lines"][1]


def test_fleet_page_no_db(monkeypatch, tmp_path):
    from plugin import feeds
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert feeds.fleet_page()["title"] == "FLEET: no kanban"


def test_ritual_llm_compose_preferred():
    class FakeLlm:
        def complete(self, messages, **kw):
            class R: text = "  A fine day:  three things shipped.  "
            return R()
    out = ritual.compose({"journal": [], "followups_filed": [],
                          "followups_closed_today": 0, "hermes_sessions_today": 0,
                          "hermes_tools_today": 0, "hermes_tokens_today": 0,
                          "date": "2026-07-04"}, llm=FakeLlm())
    assert out == "A fine day: three things shipped."
