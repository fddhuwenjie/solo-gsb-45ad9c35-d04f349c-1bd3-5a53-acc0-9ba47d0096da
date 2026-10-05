"""Upgrade test: legacy ``address_provisions`` rows survive the schema rebuild.

Old databases tied the provision record to the allocation with
``ON DELETE CASCADE`` (and a NOT NULL allocation id).  The upgrade rebuilds the
table so releasing the allocation sets the id to NULL and keeps the row; an
unreleased provision recorded by the old code must still replay afterwards.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

_LEGACY_ADDRESS_PROVISIONS_DDL = """
CREATE TABLE address_provisions (
    plan_id        TEXT    PRIMARY KEY
                      REFERENCES bgp_plans(plan_id) ON DELETE CASCADE,
    pool_id        INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    allocation_id  INTEGER NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    request_id     TEXT    NOT NULL,
    params         TEXT    NOT NULL,
    result         TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);
CREATE INDEX idx_address_provisions_request ON address_provisions(request_id);
"""


def _build_legacy_db(db_path):
    """Create an old-schema database holding one unreleased provision."""

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE pools (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cidr        TEXT    NOT NULL UNIQUE,
                created_at  TEXT    NOT NULL
            );
            CREATE TABLE exclusions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                cidr        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                UNIQUE (pool_id, cidr)
            );
            CREATE TABLE allocations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id         INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                cidr            TEXT    NOT NULL,
                request_id      TEXT    NOT NULL UNIQUE,
                prefix_length   INTEGER NOT NULL,
                created_at      TEXT    NOT NULL
            );
            CREATE TABLE bgp_plans (
                plan_id          TEXT    PRIMARY KEY,
                pool_id          INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                params           TEXT    NOT NULL,
                audit_results    TEXT    NOT NULL,
                pool_state       TEXT    NOT NULL,
                state_digest     TEXT    NOT NULL,
                status           TEXT    NOT NULL
                                   CHECK (status IN ('planned', 'active', 'retired')),
                created_at       TEXT    NOT NULL,
                activated_at     TEXT
            );
            CREATE INDEX idx_bgp_plans_pool ON bgp_plans(pool_id);
            """
            + _LEGACY_ADDRESS_PROVISIONS_DDL
        )
        conn.execute(
            "INSERT INTO pools (cidr, created_at) VALUES (?, ?)",
            ("203.0.113.0/24", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO allocations (pool_id, cidr, request_id, prefix_length, created_at) "
            "VALUES (1, '203.0.113.0/25', 'order-1', 25, '2026-01-01T00:00:00+00:00')"
        )

        # Build params/result exactly as the current code would, so a replay
        # after the upgrade recognises the stored fingerprint.
        from app.bgpplan import _canonical_params, _state_digest
        from app.provision import _provision_params
        from app.schemas import BgpAnnouncement, CustomerProviderRelation

        rels = [
            CustomerProviderRelation(
                type="customer_provider", customer=10, provider=100
            )
        ]
        new_request = type("NewAnnouncement", (), {"id": "new", "as_path": [10, 100]})()
        results = [
            {
                "id": "new",
                "prefix": "203.0.113.0/25",
                "as_path": [10, 100],
                "origin": 100,
                "status": "valid",
                "flags": [],
                "violating_hop": None,
            }
        ]
        pool_state = {
            "cidr": "203.0.113.0/24",
            "exclusions": [],
            "allocations": [
                {
                    "cidr": "203.0.113.0/25",
                    "request_id": "order-1",
                    "prefix_length": 25,
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        }
        digest = _state_digest(pool_state)
        plan_params = json.dumps(
            _canonical_params(
                rels,
                [BgpAnnouncement(id="new", prefix="203.0.113.0/25", as_path=[10, 100])],
            )
        )
        conn.execute(
            "INSERT INTO bgp_plans (plan_id, pool_id, params, audit_results, "
            "pool_state, state_digest, status, created_at, activated_at) "
            "VALUES ('plan-1', 1, ?, ?, ?, ?, 'planned', '2026-01-01T00:00:00+00:00', NULL)",
            (plan_params, json.dumps(results), json.dumps(pool_state), digest),
        )
        prov_params = _provision_params(
            1, "order-1", 25, "plan-1", rels, [], new_request, "203.0.113.0/25"
        )
        result = {
            "pool_id": 1,
            "allocation": {
                "id": 1,
                "pool_id": 1,
                "cidr": "203.0.113.0/25",
                "request_id": "order-1",
                "prefix_length": 25,
                "created_at": "2026-01-01T00:00:00+00:00",
                "replayed": False,
            },
            "plan": {
                "plan_id": "plan-1",
                "pool_id": 1,
                "status": "planned",
                "results": results,
                "state_digest": digest,
                "pool_state": pool_state,
                "created_at": "2026-01-01T00:00:00+00:00",
                "activated_at": None,
                "replayed": False,
            },
            "new_announcement": results[0],
            "created_at": "2026-01-01T00:00:00+00:00",
            "replayed": False,
        }
        conn.execute(
            "INSERT INTO address_provisions (plan_id, pool_id, allocation_id, "
            "request_id, params, result, created_at) "
            "VALUES ('plan-1', 1, 1, 'order-1', ?, ?, '2026-01-01T00:00:00+00:00')",
            (prov_params, json.dumps(result)),
        )
        conn.commit()
    finally:
        conn.close()


_BODY = {
    "request_id": "order-1",
    "prefix_length": 25,
    "plan_id": "plan-1",
    "relationships": [
        {"type": "customer_provider", "customer": 10, "provider": 100}
    ],
    "announcements": [],
    "new_announcement": {"id": "new", "as_path": [10, 100]},
}


def test_legacy_provision_table_is_rebuilt_and_unreleased_provision_replays(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-provisions.db"
    _build_legacy_db(db_path)

    monkeypatch.setenv("SUBNET_DB_PATH", str(db_path))
    from app import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.init_db()

    # The table now uses ON DELETE SET NULL, the historical row survived with
    # its live allocation reference, and the request-id index was rebuilt.
    conn = sqlite3.connect(str(db_path))
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='address_provisions'"
        ).fetchone()[0]
        assert "ON DELETE SET NULL" in sql
        assert "ON DELETE CASCADE" not in sql.split("allocation_id")[1].split(",")[0]
        assert conn.execute(
            "SELECT plan_id, allocation_id, request_id FROM address_provisions"
        ).fetchone() == ("plan-1", 1, "order-1")
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='address_provisions'"
            )
        }
        assert "idx_address_provisions_request" in indexes
    finally:
        conn.close()

    from app.main import app

    with TestClient(app) as client:
        # An unreleased provision stored by the old code still replays after
        # the upgrade, returning the original allocation and plan.
        replay = client.post("/pools/1/provisions", json=_BODY)
        assert replay.status_code == 200, replay.text
        data = replay.json()
        assert data["replayed"] is True
        assert data["allocation"]["cidr"] == "203.0.113.0/25"
        assert data["allocation"]["id"] == 1
        assert data["plan"]["plan_id"] == "plan-1"
        assert data["plan"]["created_at"] == "2026-01-01T00:00:00+00:00"

        # Releasing it now nulls the reference instead of deleting the record,
        # and the same request still replays afterwards.
        released = client.delete("/pools/1/allocations/order-1")
        assert released.status_code == 200
        after = client.post("/pools/1/provisions", json=_BODY)
        assert after.status_code == 200, after.text
        assert after.json()["replayed"] is True
        assert after.json()["allocation"]["cidr"] == "203.0.113.0/25"

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute(
                "SELECT allocation_id FROM address_provisions WHERE plan_id = 'plan-1'"
            ).fetchone() == (None,)
            assert conn.execute(
                "SELECT count(*) FROM bgp_plans WHERE plan_id = 'plan-1'"
            ).fetchone() == (1,)
        finally:
            conn.close()


def test_fresh_database_is_not_rebuilt(tmp_path, monkeypatch):
    """init_db on a fresh database must leave the new table untouched."""

    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("SUBNET_DB_PATH", str(db_path))
    from app import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.init_db()

    conn = sqlite3.connect(str(db_path))
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='address_provisions'"
        ).fetchone()[0]
        assert "ON DELETE SET NULL" in sql
        # No legacy leftovers.
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE '%legacy%'"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_database_needing_both_migrations_keeps_provision_rows(tmp_path, monkeypatch):
    """A DB predating handover *and* release-replay upgrades without loss.

    The bgp_plans rebuild renames/drops the old plan table the legacy
    provisions rows reference with ON DELETE CASCADE; foreign keys are
    suspended for that rebuild so the provision rows survive.
    """

    db_path = tmp_path / "both-legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cidr TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL);
            CREATE TABLE allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                cidr TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                prefix_length INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE bgp_plans (
                plan_id TEXT PRIMARY KEY,
                pool_id INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                params TEXT NOT NULL, audit_results TEXT NOT NULL,
                pool_state TEXT NOT NULL, state_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('planned','active')),
                created_at TEXT NOT NULL, activated_at TEXT);
            CREATE INDEX idx_bgp_plans_pool ON bgp_plans(pool_id);
            CREATE TABLE address_provisions (
                plan_id TEXT PRIMARY KEY REFERENCES bgp_plans(plan_id) ON DELETE CASCADE,
                pool_id INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                allocation_id INTEGER NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL, params TEXT NOT NULL, result TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE INDEX idx_address_provisions_request ON address_provisions(request_id);
            """
        )
        conn.execute(
            "INSERT INTO pools (cidr, created_at) VALUES ('203.0.113.0/24','t0')"
        )
        conn.execute(
            "INSERT INTO allocations (pool_id,cidr,request_id,prefix_length,created_at) "
            "VALUES (1,'203.0.113.0/25','order-1',25,'t0')"
        )
        conn.execute(
            "INSERT INTO bgp_plans VALUES "
            "('plan-1',1,'{}','[]','{}','d','active','t0','t1')"
        )
        conn.execute(
            "INSERT INTO address_provisions VALUES "
            "('plan-1',1,1,'order-1','{}','{}','t0')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("SUBNET_DB_PATH", str(db_path))
    from app import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.init_db()
    db_module.init_db()  # migrations are idempotent

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT plan_id, status FROM bgp_plans"
        ).fetchone() == ("plan-1", "active")
        assert conn.execute(
            "SELECT plan_id, allocation_id, request_id FROM address_provisions"
        ).fetchone() == ("plan-1", 1, "order-1")
        plan_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='bgp_plans'"
        ).fetchone()[0]
        prov_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='address_provisions'"
        ).fetchone()[0]
        assert "'retired'" in plan_sql
        assert "ON DELETE SET NULL" in prov_sql
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {"idx_bgp_plans_pool", "idx_address_provisions_request"} <= indexes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
