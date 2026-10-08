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
