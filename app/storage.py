"""SQLite 持久化：现行日表、版号、封存区间。

版本继承模型
------------
* 每个版本只保存**未封存后缀**的日值（第 ``sealed_through + 1`` 日起）；
  已封存前缀不复制、不重写，读取时按封存链回溯到真正保存它的版本。
* 封存记录是一条**链表**：``seals.prev_seal_id`` 指向上一段封存，
  ``versions.seal_head_id`` 指向该版有效封存链的表头。新版本只继承表头
  （一次指针赋值，O(1)），不复制任何封存记录；每次 ``/seal`` 只新增一条。
* 因此继承的读写量与日记录数无关；``/versions`` 按版查封存时直接从表头
  沿封存链取原记录编号，无需沿补报历史逐版回找。

旧库迁移
--------
启动时自动为旧库补列（``versions.seal_head_id``、``seals.prev_seal_id``、
``seals.sealed_amount``）并回填封存链。旧库中已有的版本号、日记录主键和
封存记录编号全部原样保留，旧版仍按自身完整日表继续查询与补报。

所有写操作在单个事务内完成，失败时整体回滚，调用方继续供应原表。
连接以 WAL 模式打开；进程内用一把锁串行化写事务（SQLite 单写者）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = 2


class StorageError(RuntimeError):
    """写入失败（已回滚，原表保持可用）。"""


SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS versions (
    version       INTEGER PRIMARY KEY,
    year          INTEGER NOT NULL,
    month         INTEGER NOT NULL,
    sim_rain      TEXT    NOT NULL,
    sim_total     INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    note          TEXT,
    seal_head_id  INTEGER
);
CREATE TABLE IF NOT EXISTS day_rows (
    version      INTEGER NOT NULL REFERENCES versions(version),
    day          INTEGER NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount >= 0),
    source       TEXT    NOT NULL CHECK (source IN ('approved', 'estimated')),
    exact_num    INTEGER NOT NULL,
    exact_den    INTEGER NOT NULL,
    PRIMARY KEY (version, day)
);
CREATE TABLE IF NOT EXISTS seals (
    seal_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    version        INTEGER NOT NULL,
    through_day    INTEGER NOT NULL,
    sealed_at      TEXT    NOT NULL,
    prev_seal_id   INTEGER,
    sealed_amount  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (version, through_day)
);
CREATE TABLE IF NOT EXISTS current_version (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    version      INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class DayRow:
    day: int
    amount: int
    source: str
    exact_num: int
    exact_den: int


@dataclass(frozen=True)
class VersionMeta:
    version: int
    year: int
    month: int
    sim_rain: tuple[int, ...]
    sim_total: int
    created_at: str
    note: Optional[str]
    seal_head_id: Optional[int] = None


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_locked()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 读 ----------
    def current_version_no(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT version FROM current_version WHERE id = 1"
        ).fetchone()
        return None if row is None else int(row["version"])

    def get_version(self, version: int) -> Optional[VersionMeta]:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            return None
        return self._version_from_row(row)

    def get_current(self) -> Optional[VersionMeta]:
        v = self.current_version_no()
        return None if v is None else self.get_version(v)

    def own_days(self, version: int, after_day: int = 0,
                 through_day: Optional[int] = None) -> list[DayRow]:
        """该版自身保存的日值（继承来的前缀不在其中）。"""
        sql = ("SELECT * FROM day_rows WHERE version = ? AND day > ?")
        args: list = [version, after_day]
        if through_day is not None:
            sql += " AND day <= ?"
            args.append(through_day)
        sql += " ORDER BY day"
        return [self._day_from_row(r)
                for r in self._conn.execute(sql, args).fetchall()]

    def list_days(self, version: int) -> list[DayRow]:
        """该版**有效日表**：封存链上的继承前缀 + 自身保存的后缀。

        继承来的封存段按“冻结的整数记录”呈现：其 ``exact`` 即该整数本身。
        这样有效日表的精确份额与日值在封存边界处严格对齐（边界累计误差为
        0），跨边界的任意连续区间累计误差仍严格小于 1，且精确份额总量与
        模拟月总量一致。自身封存段保留本版重算时的精确份额，不做改写。
        """
        meta = self.get_version(version)
        if meta is None:
            return []
        if meta.seal_head_id is None:
            return self.own_days(version)

        rows: list[DayRow] = []
        prev_through = 0
        for seal in self._seal_chain(meta.seal_head_id):
            if seal["through_day"] <= prev_through:
                raise StorageError(
                    f"封存链异常：版本 {version} 的封存截止日未递增"
                )
            segment = self.own_days(
                seal["version"], after_day=prev_through,
                through_day=seal["through_day"],
            )
            expected = seal["through_day"] - prev_through
            if len(segment) != expected:
                raise StorageError(
                    f"封存段记录缺失：版本 {seal['version']} "
                    f"第 {prev_through + 1}~{seal['through_day']} 日"
                )
            if seal["version"] != version:
                # 继承段：按冻结整数记录呈现，审计精确份额即日值。
                segment = [
                    DayRow(r.day, r.amount, r.source, r.amount, 1)
                    for r in segment
                ]
            rows.extend(segment)
            prev_through = seal["through_day"]

        rows.extend(self.own_days(version, after_day=prev_through))
        return rows

    def sealed_through(self, version: int) -> int:
        """该版有效封存到哪一天；未封存返回 0。"""
        meta = self.get_version(version)
        if meta is None or meta.seal_head_id is None:
            return 0
        row = self._conn.execute(
            "SELECT through_day FROM seals WHERE seal_id = ?",
            (meta.seal_head_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"版本 {version} 的封存表头丢失")
        return int(row["through_day"])

    def sealed_amount(self, version: int) -> int:
        """该版有效封存前缀的累计雨量（封存时的快照）。"""
        meta = self.get_version(version)
        if meta is None or meta.seal_head_id is None:
            return 0
        row = self._conn.execute(
            "SELECT sealed_amount FROM seals WHERE seal_id = ?",
            (meta.seal_head_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"版本 {version} 的封存表头丢失")
        return int(row["sealed_amount"])

    def list_seals(self, version: int) -> list[dict]:
        """该版的有效封存记录（原编号，按截止日升序）。

        直接从 ``seal_head_id`` 沿封存链读取，不扫描版本历史。
        """
        meta = self.get_version(version)
        if meta is None or meta.seal_head_id is None:
            return []
        seals = self._seal_chain(meta.seal_head_id)
        out = []
        prev_through = 0
        for s in seals:
            out.append({
                "seal_id": s["seal_id"],
                "through_day": s["through_day"],
                "sealed_at": s["sealed_at"],
                "sealed_amount": s["sealed_amount"],
                "day_from": prev_through + 1,
                "day_to": s["through_day"],
                "sealed_on_version": s["version"],
            })
            prev_through = s["through_day"]
        return out

    def list_versions(self) -> list[VersionMeta]:
        rows = self._conn.execute(
            "SELECT * FROM versions ORDER BY version"
        ).fetchall()
        return [self._version_from_row(r) for r in rows]

    # ---------- 写 ----------
    def write_new_version(self, *, year: int, month: int,
                          sim_rain: tuple[int, ...], sim_total: int,
                          rows: list[DayRow], note: str) -> int:
        """写入一个全新、未封存的版本并切换为现行版本（``/convert``）。"""
        now = _now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                next_v = self._next_version_locked()
                self._insert_version_locked(
                    next_v, year, month, sim_rain, sim_total, now, note,
                    rows, seal_head_id=None,
                )
                self._conn.execute(
                    "INSERT INTO current_version(id, version) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                    (next_v,),
                )
                self._conn.execute("COMMIT")
                return next_v
            except Exception as exc:
                self._rollback()
                raise StorageError(f"写入新版本失败: {exc}") from exc

    def add_version_from(self, base_version: int, *, sim_rain: tuple[int, ...],
                         sim_total: int, rows: list[DayRow], note: str,
                         seal_head_id: Optional[int]) -> int:
        """以基版为基础追加新版（补报/更正重算）。

        ``rows`` 只含未封存后缀；封存前缀由 ``seal_head_id`` 指向的封存链
        继承，不复制封存记录、不重写封存日值。日表与继承的封存在同一事务
        内生效，失败整体回滚，现行版保持不变。
        """
        base = self.get_version(base_version)
        if base is None:
            raise StorageError(f"基版 {base_version} 不存在")
        now = _now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                next_v = self._next_version_locked()
                self._insert_version_locked(
                    next_v, base.year, base.month, sim_rain, sim_total,
                    now, note, rows, seal_head_id=seal_head_id,
                )
                self._conn.execute(
                    "UPDATE current_version SET version = ? WHERE id = 1",
                    (next_v,),
                )
                self._conn.execute("COMMIT")
                return next_v
            except Exception as exc:
                self._rollback()
                raise StorageError(f"追加版本失败: {exc}") from exc

    def seal_current(self, through_day: int) -> dict:
        """封存现行版本从上次封存日之后到指定日的结果。

        只新增一条封存记录，并把现行版本的封存表头指向它；旧版本不受影响，
        后续补报出的版本通过表头继承这条记录。
        """
        v = self.current_version_no()
        if v is None:
            raise StorageError("尚无现行版本，无法封存")
        now = _now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                meta = self.get_version(v)
                if meta is None:
                    raise StorageError(f"现行版本 {v} 不存在")
                if meta.seal_head_id is None:
                    prev_through, prev_amount = 0, 0
                else:
                    head = self._conn.execute(
                        "SELECT seal_id, through_day, sealed_amount FROM seals "
                        "WHERE seal_id = ?", (meta.seal_head_id,),
                    ).fetchone()
                    if head is None:
                        raise StorageError(f"版本 {v} 的封存表头丢失")
                    prev_through = int(head["through_day"])
                    prev_amount = int(head["sealed_amount"])
                if through_day <= prev_through:
                    raise StorageError(
                        f"现行版本已封存至第 {prev_through} 日，无需重复封存"
                    )
                seg = self._conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS s FROM day_rows "
                    "WHERE version = ? AND day > ? AND day <= ?",
                    (v, prev_through, through_day),
                ).fetchone()
                if self._conn.execute(
                    "SELECT COUNT(*) AS c FROM day_rows WHERE version = ? "
                    "AND day > ? AND day <= ?",
                    (v, prev_through, through_day),
                ).fetchone()["c"] != through_day - prev_through:
                    raise StorageError(
                        f"第 {prev_through + 1}~{through_day} 日记录缺失，无法封存"
                    )
                sealed_amount = prev_amount + int(seg["s"])
                cur = self._conn.execute(
                    "INSERT INTO seals(version, through_day, sealed_at, "
                    "prev_seal_id, sealed_amount) VALUES (?, ?, ?, ?, ?)",
                    (v, through_day, now, meta.seal_head_id, sealed_amount),
                )
                seal_id = int(cur.lastrowid)
                self._conn.execute(
                    "UPDATE versions SET seal_head_id = ? WHERE version = ?",
                    (seal_id, v),
                )
                self._conn.execute("COMMIT")
                return {
                    "seal_id": seal_id, "version": v,
                    "through_day": through_day,
                    "day_from": prev_through + 1, "day_to": through_day,
                    "sealed_at": now, "sealed_amount": sealed_amount,
                }
            except Exception as exc:
                self._rollback()
                raise StorageError(f"封存失败: {exc}") from exc

    # ---------- 内部 ----------
    def _seal_chain(self, head_id: int) -> list[dict]:
        """从表头回溯封存链，返回按截止日升序的原记录。"""
        chain: list[dict] = []
        seen: set[int] = set()
        current: Optional[int] = head_id
        while current is not None:
            if current in seen:
                raise StorageError("封存链成环")
            seen.add(current)
            row = self._conn.execute(
                "SELECT seal_id, version, through_day, sealed_at, "
                "prev_seal_id, sealed_amount FROM seals WHERE seal_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                raise StorageError(f"封存记录 {current} 丢失")
            chain.append(dict(row))
            current = row["prev_seal_id"]
        chain.reverse()
        return chain

    def _next_version_locked(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM versions"
        ).fetchone()
        return int(row["v"])

    def _insert_version_locked(self, version: int, year: int, month: int,
                               sim_rain: tuple[int, ...], sim_total: int,
                               created_at: str, note: str,
                               rows: list[DayRow],
                               seal_head_id: Optional[int]) -> None:
        self._conn.execute(
            "INSERT INTO versions(version, year, month, sim_rain, sim_total, "
            "created_at, note, seal_head_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version, year, month, json.dumps(list(sim_rain)),
             sim_total, created_at, note, seal_head_id),
        )
        self._conn.executemany(
            "INSERT INTO day_rows(version, day, amount, source, exact_num, "
            "exact_den) VALUES (?, ?, ?, ?, ?, ?)",
            [(version, r.day, r.amount, r.source,
              r.exact_num, r.exact_den) for r in rows],
        )

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> VersionMeta:
        keys = row.keys()
        return VersionMeta(
            version=row["version"], year=row["year"], month=row["month"],
            sim_rain=tuple(json.loads(row["sim_rain"])),
            sim_total=row["sim_total"], created_at=row["created_at"],
            note=row["note"],
            seal_head_id=(row["seal_head_id"]
                          if "seal_head_id" in keys else None),
        )

    @staticmethod
    def _day_from_row(r: sqlite3.Row) -> DayRow:
        return DayRow(r["day"], r["amount"], r["source"],
                      r["exact_num"], r["exact_den"])

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # ---------- 旧库迁移 ----------
    def _migrate_locked(self) -> None:
        """就地升级旧库：补列、回填封存链，保留全部既有编号。"""
        version_cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(versions)").fetchall()}
        seal_cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(seals)").fetchall()}
        if "seal_head_id" not in version_cols:
            self._conn.execute(
                "ALTER TABLE versions ADD COLUMN seal_head_id INTEGER")
        if "prev_seal_id" not in seal_cols:
            self._conn.execute(
                "ALTER TABLE seals ADD COLUMN prev_seal_id INTEGER")
        if "sealed_amount" not in seal_cols:
            self._conn.execute(
                "ALTER TABLE seals ADD COLUMN sealed_amount INTEGER "
                "NOT NULL DEFAULT 0")

        migrated = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if migrated >= SCHEMA_VERSION:
            return

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            versions = self._conn.execute(
                "SELECT version FROM versions ORDER BY version"
            ).fetchall()
            for vrow in versions:
                v = int(vrow["version"])
                seals = self._conn.execute(
                    "SELECT seal_id, through_day FROM seals WHERE version = ? "
                    "ORDER BY through_day, seal_id", (v,),
                ).fetchall()
                prev_id: Optional[int] = None
                for s in seals:
                    amount = self._conn.execute(
                        "SELECT COALESCE(SUM(amount), 0) AS a FROM day_rows "
                        "WHERE version = ? AND day <= ?",
                        (v, s["through_day"]),
                    ).fetchone()["a"]
                    self._conn.execute(
                        "UPDATE seals SET prev_seal_id = ?, sealed_amount = ? "
                        "WHERE seal_id = ?",
                        (prev_id, int(amount), s["seal_id"]),
                    )
                    prev_id = int(s["seal_id"])
                self._conn.execute(
                    "UPDATE versions SET seal_head_id = ? WHERE version = ?",
                    (prev_id, v),
                )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except Exception as exc:
            self._rollback()
            raise StorageError(f"旧库迁移失败: {exc}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
