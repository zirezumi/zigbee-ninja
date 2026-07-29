"""Transaction hygiene on the thread-local connections.

These cover the failure that took the GUI down for 29 hours on 2026-07-28/29: a
write that raised left Python's implicit transaction open, the connection was
then holding a read snapshot older than every later commit, and SQLite refuses
to upgrade a stale snapshot immediately rather than waiting. Every later write on
that connection failed in about 3 ms no matter how generous busy_timeout was.
"""

import sqlite3
import threading

import pytest

from zigbee_ninja.store.db import Database


def test_failed_write_rolls_back_and_leaves_no_open_transaction(tmp_path):
    db = Database(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        with db.write() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES ('first', '1')")
            conn.execute("INSERT INTO no_such_table (x) VALUES (1)")

    conn = db.connect()
    assert conn.in_transaction is False, "a failed write must not leave a transaction open"
    assert conn.execute("SELECT COUNT(*) AS n FROM settings").fetchone()["n"] == 0
    assert db.write_failures == 1


def test_connection_still_writable_after_a_failure(tmp_path):
    """The regression proper: one failure must not end the connection's life."""
    db = Database(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        with db.write() as conn:
            conn.execute("INSERT INTO no_such_table (x) VALUES (1)")

    with db.write() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('after', '1')")
    assert db.connect().execute("SELECT value FROM settings").fetchone()["value"] == "1"


def _strand_on_a_stale_snapshot(db: Database) -> sqlite3.Connection:
    """Put this thread's connection in the exact state the live wedge was in.

    A READ transaction, not a write one: the wedged connection live could not
    have held the write lock, because the threadpool connections went on writing
    normally throughout. So the leak is an open snapshot, and it goes stale as
    soon as anyone else commits.
    """
    conn = db.connect()
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM settings").fetchone()

    def commit_from_another_thread() -> None:
        other = sqlite3.connect(db.path)
        other.execute("INSERT INTO settings (key, value) VALUES ('elsewhere', '2')")
        other.commit()
        other.close()

    thread = threading.Thread(target=commit_from_another_thread)
    thread.start()
    thread.join()
    return conn


def test_a_stale_snapshot_refuses_writes_outright(tmp_path):
    """Pin the SQLite behaviour the outage rested on, so it stays understood.

    The write is refused immediately rather than after busy_timeout, because
    waiting cannot make a stale snapshot current. Any "just raise the timeout"
    fix for this class of bug is therefore treating the wrong thing.
    """
    db = Database(tmp_path)
    conn = _strand_on_a_stale_snapshot(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO settings (key, value) VALUES ('blocked', '1')")
    conn.rollback()


def test_write_clears_a_transaction_leaked_by_something_else(tmp_path):
    """A leak from outside write() must not wedge the next writer either."""
    db = Database(tmp_path)
    _strand_on_a_stale_snapshot(db)

    with db.write() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('recovered', '3')")

    keys = {row["key"] for row in db.connect().execute("SELECT key FROM settings")}
    assert keys == {"elsewhere", "recovered"}


def test_fresh_read_is_not_served_from_a_stale_snapshot(tmp_path):
    """A read on a leaked transaction sees a stale database.

    This is why resolve_session could not simply be made read-only and left on a
    possibly-wedged connection: it would stop erroring and start lying, and a
    session created after the snapshot would read as no session at all.
    """
    db = Database(tmp_path)
    stale = _strand_on_a_stale_snapshot(db)
    on_the_snapshot = {row["key"] for row in stale.execute("SELECT key FROM settings")}
    assert "elsewhere" not in on_the_snapshot, "precondition: this snapshot is stale"

    keys = {row["key"] for row in db.fresh_read().execute("SELECT key FROM settings")}
    assert "elsewhere" in keys


def test_discard_connection_hands_back_a_new_one(tmp_path):
    db = Database(tmp_path)
    first = db.connect()
    db.discard_connection()
    assert db.connect() is not first


def test_health_counts_write_failures(client):
    """Failed writes have to be visible without the GUI.

    The live incident lost writes for 29 hours while /api/health kept answering
    "ok", because nothing it reported could go wrong in this way.
    """
    assert client.get("/api/health").json()["storage"]["write_failures"] == 0

    db = client.app.state.db
    with pytest.raises(sqlite3.OperationalError):
        with db.write() as conn:
            conn.execute("INSERT INTO no_such_table (x) VALUES (1)")

    assert client.get("/api/health").json()["storage"]["write_failures"] == 1
