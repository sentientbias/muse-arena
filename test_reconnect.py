#!/usr/bin/env python3
"""Reconnect regression test (production 500s, 2026-09-17).

Production Neon Postgres kills idle connections / autosuspends compute, and
the app used to connect exactly once at startup with no retry — so one dead
socket turned every DB-touching endpoint into a permanent 500 until redeploy.

This test simulates a dead connection and asserts:
  1. a connection-level error triggers exactly one reconnect + retry, and
     the query then succeeds;
  2. a NON-connection error (bad SQL) propagates unchanged — no reconnect,
     never silently swallowed;
  3. if the retry ALSO hits a connection error, it raises (no infinite loop).

Run:  python3 test_reconnect.py   (sqlite, no server needed)
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app


class FakeConnError(Exception):
    """Stands in for psycopg2.OperationalError / InterfaceError."""


def fresh_arena():
    db = tempfile.mktemp(suffix=".db")
    return app.Arena(db), db


def wire_dead_connection(arena):
    """Make the next cursor creation fail like a dead Postgres socket.

    Returns a dict tracking reconnect calls."""
    calls = {"reconnects": 0}
    real_cursor = arena._cursor
    state = {"dead": True}

    def dying_cursor():
        if state["dead"]:
            raise FakeConnError("server closed the connection unexpectedly")
        return real_cursor()

    arena._conn_errors = lambda: (FakeConnError,)  # noqa: E731

    def fake_reconnect():
        # A fresh connection heals the socket — exactly what the pg
        # _reconnect() does; here we just mark the fake socket alive.
        calls["reconnects"] += 1
        state["dead"] = False

    def fake_reconnect():
        # A fresh connection heals the socket — exactly what the pg
        # _reconnect() does; here we just mark the fake socket alive.
        calls["reconnects"] += 1
        state["dead"] = False

    def heal():
        state["dead"] = False

    arena._reconnect = fake_reconnect  # noqa: E731
    arena._cursor = dying_cursor  # noqa: E731
    calls["heal"] = heal
    return calls


def main():
    # 1) dead connection -> one reconnect, query succeeds
    arena, db = fresh_arena()
    try:
        calls = wire_dead_connection(arena)
        rows = arena._rows("SELECT id FROM players")
        assert rows == [], f"expected [], got {rows}"
        assert calls["reconnects"] == 1, \
            f"expected exactly 1 reconnect, got {calls['reconnects']}"
        # a subsequent query must NOT reconnect again
        arena._row("SELECT id FROM tournament WHERE id=1")
        assert calls["reconnects"] == 1, \
            f"spurious reconnect on healthy socket: {calls['reconnects']}"
        # exercise the real failing paths end to end on the healed socket
        info = arena.tournament_info()
        assert "pot_units" in info
        assert isinstance(arena.stakes_board(), list)
        snap = arena.spectate()
        assert "rooms" in snap and "boards" in snap
        print("1) dead-connection reconnect+retry: OK")
    finally:
        try:
            arena.db.close()
        except Exception:
            pass
        if os.path.exists(db):
            os.unlink(db)

    # 2) non-connection errors are NOT retried and NOT swallowed
    arena, db = fresh_arena()
    try:
        calls = wire_dead_connection(arena)
        calls["heal"]()  # socket is healthy here; only the SQL is bad
        arena._reconnect = lambda: calls.__setitem__(  # noqa: E731
            "reconnects", calls["reconnects"] + 1)
        try:
            arena._rows("SELECT * FROM no_such_table_xyz")
        except Exception as e:
            assert calls["reconnects"] == 0, \
                f"non-connection error triggered reconnect: {e!r}"
            assert "no_such_table_xyz" in str(e), \
                f"original error was swallowed/altered: {e!r}"
        else:
            raise AssertionError("bad SQL did not raise")
        print("2) non-connection error propagates unretried: OK")
    finally:
        try:
            arena.db.close()
        except Exception:
            pass
        if os.path.exists(db):
            os.unlink(db)

    # 3) retry that still fails raises instead of looping
    arena, db = fresh_arena()
    try:
        real_cursor = arena._cursor
        arena._conn_errors = lambda: (FakeConnError,)  # noqa: E731
        arena._reconnect = lambda: None  # reconnect does not heal  # noqa: E731
        arena._cursor = lambda: (_ for _ in ()).throw(  # noqa: E731
            FakeConnError("still dead after reconnect"))
        try:
            arena._rows("SELECT id FROM players")
        except FakeConnError:
            print("3) failed retry raises, no infinite loop: OK")
        else:
            raise AssertionError("dead-after-retry did not raise")
    finally:
        arena._cursor = real_cursor
        try:
            arena.db.close()
        except Exception:
            pass
        if os.path.exists(db):
            os.unlink(db)

    print("ALL RECONNECT TESTS PASSED")


if __name__ == "__main__":
    main()
