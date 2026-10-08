from __future__ import annotations

import json
import time

from app.config import SINGLE_USER_ID
from app.db import Database


def finished_run(db: Database, conv_id: str, run_id: str, cost: float) -> None:
    db.create_run(run_id, conv_id, SINGLE_USER_ID, "q", "m")
    db.finish_run(run_id, "completed", {"steps": 1, "cost_usd": cost}, None)


def test_deleting_a_conversation_does_not_refund_the_budget():
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    finished_run(db, conv["id"], "r1", 1.25)
    assert db.month_spend(SINGLE_USER_ID) == 1.25

    assert db.delete_conversation(conv["id"], SINGLE_USER_ID)
    assert db.get_run("r1", SINGLE_USER_ID) is None  # the run record is gone...
    assert db.month_spend(SINGLE_USER_ID) == 1.25  # ...but the money was still spent


def test_finishing_a_run_twice_does_not_double_count():
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    finished_run(db, conv["id"], "r1", 0.4)
    db.finish_run("r1", "completed", {"cost_usd": 0.5}, None)
    assert round(db.month_spend(SINGLE_USER_ID), 6) == 0.5


def test_orphaned_run_recovers_its_cost_from_the_last_usage_event():
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    db.create_run("r1", conv["id"], SINGLE_USER_ID, "q", "m")
    db.add_event("r1", 3, "usage", {"step": 1, "run_cost_usd": 0.1})
    db.add_event("r1", 9, "usage", {"step": 2, "run_cost_usd": 0.35})

    assert db.fail_orphaned_runs() == 1
    run = db.get_run("r1", SINGLE_USER_ID)
    assert run["status"] == "error" and run["cost_usd"] == 0.35 and run["steps"] == 2
    assert round(db.month_spend(SINGLE_USER_ID), 6) == 0.35
    assert db.fail_orphaned_runs() == 0  # idempotent


def test_orphaned_run_without_usage_costs_nothing():
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    db.create_run("r1", conv["id"], SINGLE_USER_ID, "q", "m")
    assert db.fail_orphaned_runs() == 1
    assert db.month_spend(SINGLE_USER_ID) == 0.0


def test_ledger_is_backfilled_for_databases_created_before_it_existed(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    conv = db.create_conversation(SINGLE_USER_ID)
    finished_run(db, conv["id"], "r1", 2.0)
    db.create_run("live", conv["id"], SINGLE_USER_ID, "q", "m")  # still running: must not be counted
    db._exec("DROP TABLE usage_ledger")  # what an old database looks like
    db.close()

    reopened = Database(path)
    assert reopened.month_spend(SINGLE_USER_ID) == 2.0
    reopened.close()


def test_spend_before_this_month_is_excluded():
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    finished_run(db, conv["id"], "r1", 3.0)
    db._exec("UPDATE usage_ledger SET created_at=?", (time.time() - 90 * 86400,))
    assert db.month_spend(SINGLE_USER_ID) == 0.0
    assert json.dumps(db.month_spend(SINGLE_USER_ID))  # plain float


# ---- audit log --------------------------------------------------------------


def open_audit(db: Database, **over) -> int:
    args = dict(
        user_id=SINGLE_USER_ID, run_id="r1", tool_use_id="t1", tool="write_note", tier="write",
        tool_input={"title": "a", "content": "b"}, outcome="pending",
    )  # fmt: skip
    return db.audit_open(**{**args, **over})


def audit_items(db: Database, **kw) -> list[dict]:
    return db.list_audit(SINGLE_USER_ID, **kw)["items"]


def test_audit_row_goes_from_pending_to_decided_to_result():
    db = Database(":memory:")
    audit_id = open_audit(db)
    (row,) = audit_items(db)
    assert row["outcome"] == "pending" and row["decided_at"] is None and row["result"] is None
    assert json.loads(row["input_preview"]) == {"title": "a", "content": "b"}

    db.audit_decide(audit_id, "approved")
    db.audit_result(audit_id, "ok")
    (row,) = audit_items(db)
    assert row["outcome"] == "approved" and row["result"] == "ok"
    assert row["decided_at"] >= row["requested_at"]


def test_first_decision_wins():
    db = Database(":memory:")
    audit_id = open_audit(db)
    db.audit_decide(audit_id, "denied", "the user denied this action")
    db.audit_decide(audit_id, "approved")  # too late: the request is already settled
    (row,) = audit_items(db)
    assert row["outcome"] == "denied" and row["reason"] == "the user denied this action"


def test_a_call_the_policy_let_through_is_decided_immediately():
    db = Database(":memory:")
    open_audit(db, outcome="auto")
    (row,) = audit_items(db)
    assert row["outcome"] == "auto" and row["decided_at"] == row["requested_at"]


def test_audit_survives_deleting_the_chat():
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    finished_run(db, conv["id"], "r1", 0.1)
    open_audit(db, outcome="approved")
    assert audit_items(db)[0]["run_text"] == "q"  # context from the run, while it exists

    assert db.delete_conversation(conv["id"], SINGLE_USER_ID)
    (row,) = audit_items(db)  # the record that the action was allowed stays...
    assert row["outcome"] == "approved" and row["tool"] == "write_note"
    assert row["run_text"] is None  # ...but the conversation's own text does not


def test_audit_listing_is_newest_first_and_filters_and_paginates():
    db = Database(":memory:")
    open_audit(db, tool_use_id="a", tool="write_note", tier="write", outcome="approved", run_id="r1")
    open_audit(db, tool_use_id="b", tool="write_note", tier="write", outcome="denied", run_id="r2")
    open_audit(db, tool_use_id="c", tool="wipe_disk", tier="destructive", outcome="denied", run_id="r2")
    open_audit(db, tool_use_id="d", tool="write_note", tier="write", outcome="auto", run_id="r3")

    assert [r["tool_use_id"] for r in audit_items(db)] == ["d", "c", "b", "a"]
    assert [r["tool_use_id"] for r in audit_items(db, outcome="denied")] == ["c", "b"]
    assert [r["tool_use_id"] for r in audit_items(db, tool="wipe_disk")] == ["c"]
    assert [r["tool_use_id"] for r in audit_items(db, tier="destructive")] == ["c"]
    assert [r["tool_use_id"] for r in audit_items(db, run_id="r2", outcome="denied")] == ["c", "b"]

    page = db.list_audit(SINGLE_USER_ID, limit=2, offset=1)
    assert [r["tool_use_id"] for r in page["items"]] == ["c", "b"]
    assert page["total"] == 4

    filtered = db.list_audit(SINGLE_USER_ID, outcome="denied", limit=1)
    assert filtered["total"] == 2 and len(filtered["items"]) == 1
    assert filtered["counts"] == {"approved": 1, "denied": 2, "auto": 1}  # whole log, whatever the filter


def test_audit_is_per_user():
    db = Database(":memory:")
    open_audit(db, user_id="someone-else")
    assert db.list_audit(SINGLE_USER_ID) == {"items": [], "total": 0, "counts": {}}


def test_a_long_input_is_capped_in_the_preview():
    db = Database(":memory:")
    open_audit(db, tool_input={"content": "x" * 5000})
    preview = audit_items(db)[0]["input_preview"]
    assert len(preview) < 2100 and preview.endswith("more chars]")


def test_requests_left_pending_by_a_crash_are_closed_on_startup(tmp_path):
    path = tmp_path / "crash.db"
    db = Database(path)
    open_audit(db, tool_use_id="waiting")
    open_audit(db, tool_use_id="done", outcome="approved")
    db.close()

    reopened = Database(path)
    assert reopened.fail_orphaned_approvals() == 1
    rows = {r["tool_use_id"]: r for r in reopened.list_audit(SINGLE_USER_ID)["items"]}
    assert rows["waiting"]["outcome"] == "cancelled" and "restarted" in rows["waiting"]["reason"]
    assert rows["waiting"]["decided_at"] is not None
    assert rows["done"]["outcome"] == "approved"  # settled rows are not touched
    assert reopened.fail_orphaned_approvals() == 0  # idempotent
    reopened.close()


def test_stored_events_carry_the_time_they_were_recorded():
    db = Database(":memory:")
    conv = db.create_conversation(SINGLE_USER_ID)
    db.create_run("r1", conv["id"], SINGLE_USER_ID, "q", "m")
    before = time.time()
    db.add_event("r1", 0, "approval_request", {"id": "t1"})
    (ev,) = db.get_events("r1")
    assert before <= ev["ts"] <= time.time()
    assert ev["data"] == {"id": "t1"}
