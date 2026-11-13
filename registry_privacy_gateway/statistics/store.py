"""中央侧摘要状态存储。

每个 ``(batch, partition)`` 只保留当前有效信封（修订替换、撤销墓碑），另有一张
只追加的接收日志记录每一次传输，供审计与“顺序不能改变结果”的回放验证使用。
状态完全由摘要信封驱动，库中不存在任何逐人原始行。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from registry_privacy_gateway.runtime import stable_json, utc_now

JOURNAL_REPLAY = "resend"
JOURNAL_REPLACE = "replace"
JOURNAL_SUPPLEMENT = "supplement"
JOURNAL_WITHDRAW = "withdraw"


class SummaryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=20000")
        return con

    def initialize(self) -> None:
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS summary_keys(
                batch TEXT NOT NULL,
                partition TEXT NOT NULL,
                site TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
                digest TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(batch, partition)
            );
            CREATE TABLE IF NOT EXISTS summary_journal(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                batch TEXT NOT NULL,
                partition TEXT NOT NULL,
                site TEXT NOT NULL,
                revision INTEGER NOT NULL,
                action TEXT NOT NULL,
                digest TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS journal_batch ON summary_journal(batch, partition, seq);
            """)

    def current(self, batch: str, partition: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM summary_keys WHERE batch=? AND partition=?",
                (batch, partition)).fetchone()
            if row is None:
                return None
            return dict(row) | {"envelope": json.loads(row["envelope_json"])}

    def active_partition_owners(self) -> dict[str, str]:
        """当前仍生效的分区占用情况：partition -> batch。"""
        with self.connect() as con:
            rows = con.execute(
                "SELECT partition, batch FROM summary_keys WHERE status='active'").fetchall()
            return {row["partition"]: row["batch"] for row in rows}

    def active_envelopes(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT envelope_json FROM summary_keys WHERE status='active' ORDER BY batch, partition"
            ).fetchall()
            return [json.loads(row["envelope_json"]) for row in rows]

    def journal(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT seq, batch, partition, site, revision, action, digest, envelope_json, received_at "
                "FROM summary_journal ORDER BY seq").fetchall()
            return [dict(row) | {"envelope": json.loads(row["envelope_json"])} for row in rows]

    def apply(self, envelope: Mapping[str, Any], action: str) -> dict[str, Any]:
        batch = envelope["batch"]
        partition = envelope["partition"]
        body = stable_json(envelope)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                existing = con.execute(
                    "SELECT revision, status FROM summary_keys WHERE batch=? AND partition=?",
                    (batch, partition)).fetchone()
                status = "withdrawn" if envelope["withdrawn"] else "active"
                if existing is None:
                    con.execute(
                        "INSERT INTO summary_keys VALUES(?,?,?,?,?,?,?,?)",
                        (batch, partition, envelope["site"], envelope["revision"], status,
                         envelope["digest"], body, utc_now()))
                else:
                    con.execute(
                        "UPDATE summary_keys SET site=?, revision=?, status=?, digest=?, "
                        "envelope_json=?, updated_at=? WHERE batch=? AND partition=?",
                        (envelope["site"], envelope["revision"], status, envelope["digest"],
                         body, utc_now(), batch, partition))
                cur = con.execute(
                    "INSERT INTO summary_journal(batch, partition, site, revision, action, digest, "
                    "envelope_json, received_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch, partition, envelope["site"], envelope["revision"], action,
                     envelope["digest"], body, utc_now()))
                con.execute("COMMIT")
                return {"seq": cur.lastrowid, "action": action, "batch": batch,
                        "partition": partition, "revision": envelope["revision"],
                        "digest": envelope["digest"]}
            except Exception:
                con.execute("ROLLBACK")
                raise
