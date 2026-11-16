from datetime import UTC, datetime, timedelta

import pytest

from agentc import store
from agentc.options import Change, Proposal

NOW = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
DESK = Proposal(
    plug_mac="AA", device="Desk 2",
    changes=(Change(9, "off", "01:30", "1111111"), Change(10, "on", "07:00", "1111111")),
    reason="Desk 2 was left on overnight at 5 W.", kwh=0.031, lkr=3.2, kg=0.013,
    evidence="event 3; readings 2 to 16 Nov", source="responder", expires=NOW + timedelta(hours=24),
)  # fmt: skip


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "app.db")
    store.add_site(db, name="home", tz="Asia/Colombo")
    return db


def add(db, proposal=DESK, status="open", now=NOW):
    return store.add_proposal(db, site_id=1, event_id=None, proposal=proposal, status=status,
                              now=now)  # fmt: skip


def test_a_proposal_is_saved_and_read_back(db):
    assert add(db) == 1
    assert store.load_proposals(db, site_id=1) == [(1, "open", DESK)]


def test_each_proposal_gets_the_next_id(db):
    add(db)
    assert add(db, status="blocked") == 2


def test_proposals_by_status(db):
    add(db, status="blocked")
    add(db)
    assert [pid for pid, _, _ in store.load_proposals(db, site_id=1, status="open")] == [2]


def test_rejected_lately_for_one_plug(db):
    other = Proposal(**{**DESK.__dict__, "plug_mac": "BB"})
    add(db, status="rejected", now=NOW - timedelta(days=15))  # too long ago
    add(db, status="rejected")
    add(db, other, status="rejected")  # another plug
    add(db, status="open")  # not rejected
    assert store.rejected(db, site_id=1, mac="AA", since=NOW - timedelta(days=14)) == [DESK]


def test_why_a_draft_was_blocked_goes_in_the_audit(db):
    store.add_audit(db, site_id=1, at=NOW, actor="code", action="draft_blocked", ref="proposal:1",
                    detail={"check": "numbers: not in the facts: 40"})  # fmt: skip
    row = db.execute("SELECT ts_utc, actor, action, ref, detail FROM audit").fetchone()
    assert row == (
        "2026-11-16T04:00:00+00:00", "code", "draft_blocked", "proposal:1",
        '{"check": "numbers: not in the facts: 40"}',
    )  # fmt: skip
