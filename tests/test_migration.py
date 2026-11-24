"""旧库兼容与继承规模测试：迁移后编号不变、继承写读量与记录数无关。"""

import sqlite3

from fastapi.testclient import TestClient


SIM30 = [3, 0, 7, 11, 25, 1, 0, 9, 4, 40,
         2, 6, 8, 0, 13, 5, 5, 100, 0, 2,
         7, 7, 3, 0, 1, 9, 12, 30, 0, 6]

# v1.0.0 的旧库结构（无 seal_head_id / prev_seal_id / sealed_amount）
OLD_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE versions (
    version INTEGER PRIMARY KEY, year INTEGER NOT NULL, month INTEGER NOT NULL,
    sim_rain TEXT NOT NULL, sim_total INTEGER NOT NULL,
    created_at TEXT NOT NULL, note TEXT);
CREATE TABLE day_rows (
    version INTEGER NOT NULL REFERENCES versions(version),
    day INTEGER NOT NULL, amount INTEGER NOT NULL CHECK (amount >= 0),
    source TEXT NOT NULL CHECK (source IN ('approved', 'estimated')),
    exact_num INTEGER NOT NULL, exact_den INTEGER NOT NULL,
    PRIMARY KEY (version, day));
CREATE TABLE seals (
    seal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL, through_day INTEGER NOT NULL,
    sealed_at TEXT NOT NULL, UNIQUE (version, through_day));
CREATE TABLE current_version (
    id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);
"""


def _build_old_db(path):
    """手工造一个“旧版”库：v1 封存第 1~5 日，v2 补报并继承了同一条封存。"""
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO versions VALUES (1, 2026, 1, ?, 316, "
        "'2026-01-01T00:00:00Z', '初始转换')", ("[" + ",".join(map(str, SIM30)) + "]",))
    conn.execute(
        "INSERT INTO versions VALUES (2, 2026, 1, ?, 316, "
        "'2026-01-02T00:00:00Z', '补报')", ("[" + ",".join(map(str, SIM30)) + "]",))
    # v2 的日表沿用 v1 的前 5 日 + 自身的后 26 日（模拟旧模型整表复制）
    for v in (1, 2):
        for day in range(1, 32):
            conn.execute(
                "INSERT INTO day_rows VALUES (?, ?, 10, 'estimated', 10, 1)",
                (v, day))
    conn.execute("INSERT INTO seals VALUES (7, 1, 5, '2026-01-01T01:00:00Z')")
    conn.execute("INSERT INTO seals VALUES (8, 2, 5, '2026-01-01T01:00:00Z')")
    conn.execute("INSERT INTO current_version VALUES (1, 2)")
    conn.commit()
    conn.close()


def _app_for(tmp_path, monkeypatch, db):
    monkeypatch.setenv("RAIN_DB_PATH", str(db))
    import importlib, sys
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    importlib.reload(main)
    return main


def test_old_db_migrates_in_place_and_keeps_ids(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    _build_old_db(db)
    main = _app_for(tmp_path, monkeypatch, db)
    with TestClient(main.app) as c:
        # 旧库的现行版号、日记录、封存编号原样可用
        assert c.get("/health").json()["current_version"] == 2
        tbl = c.get("/table").json()
        assert tbl["version"] == 2 and tbl["sealed_through"] == 5
        seals = c.get("/versions", params={
            "version": 2, "include_seals": "true"}).json()["versions"][0]["seals"]
        assert [s["seal_id"] for s in seals] == [8]
        assert seals[0]["through_day"] == 5
        assert seals[0]["sealed_at"] == "2026-01-01T01:00:00Z"

        # 旧库继续补报：只写后缀，继承原封存记录（编号 8 不变）
        r = c.post("/recompute", json={"approved": {20: 15}})
        assert r.status_code == 201, r.text
        assert r.json()["version"] == 3
        seals3 = c.get("/versions", params={
            "version": 3, "include_seals": "true"}).json()["versions"][0]["seals"]
        assert [s["seal_id"] for s in seals3] == [8]
    main.store.close()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    _build_old_db(db)
    for _ in range(3):
        main = _app_for(tmp_path, monkeypatch, db)
        with TestClient(main.app) as c:
            assert c.get("/table").json()["sealed_through"] == 5
        main.store.close()


def test_row_counts_grow_only_with_suffix(tmp_path, monkeypatch):
    """补报版本只存未封存后缀；封存只为每次 /seal 新增一行。"""
    main = _app_for(tmp_path, monkeypatch, tmp_path / "t.db")
    with TestClient(main.app) as c:
        c.post("/convert", json={"year": 2026, "month": 1, "sim_rain": SIM30})
        c.post("/seal", json={"through_day": 10})
        for d in (20, 21, 22, 23, 24):
            c.post("/recompute", json={"approved": {d: 4}})

    conn = sqlite3.connect(tmp_path / "t.db")
    # 6 个版本，封存到第 10 日 -> 每个补报版本仅存 21 行（11..31），v1 存 31 行
    counts = dict(conn.execute(
        "SELECT version, COUNT(*) FROM day_rows GROUP BY version").fetchall())
    assert counts[1] == 31
    assert all(counts[v] == 21 for v in range(2, 7))
    # 5 次补报共 5 个版本，但只有 1 条封存记录
    total_rows = conn.execute("SELECT COUNT(*) FROM day_rows").fetchone()[0]
    assert total_rows == 31 + 5 * 21
    assert conn.execute("SELECT COUNT(*) FROM seals").fetchone()[0] == 1
    # 所有版本共用同一条封存表头
    heads = {r[0] for r in conn.execute(
        "SELECT seal_head_id FROM versions WHERE version >= 2")}
    assert len(heads) == 1 and None not in heads
    conn.close()


def test_effective_table_matches_full_recompute(tmp_path, monkeypatch):
    """继承拼接出的有效日表，与不分段整月重算的结果逐日一致。"""
    from app.alloc import allocate
    main = _app_for(tmp_path, monkeypatch, tmp_path / "t.db")
    with TestClient(main.app) as c:
        c.post("/convert", json={"year": 2026, "month": 4, "sim_rain": SIM30,
                                 "approved": {2: 10}})
        c.post("/seal", json={"through_day": 8})
        c.post("/recompute", json={"approved": {15: 12, 25: 3}})
        c.post("/seal", json={"through_day": 18})
        v3 = c.post("/recompute", json={"approved": {28: 1}}).json()

    # 手工按整月算法重算（封存段作为整数核准钉住）作为对照
    sealed = {r["day"]: r["amount"] for r in v3["days"] if r["day"] <= 18}
    appr = dict(sealed)
    appr.update({15: 12, 25: 3, 28: 1})
    full = allocate(2026, 4, SIM30, appr)
    assert [r.amount for r in full.rows] == [d["amount"] for d in v3["days"]]
    main.store.close()


def test_failed_append_rolls_back_atomically(tmp_path, monkeypatch):
    """发版写入失败时整事务回滚：不留下半成品版本，原版与封存照常供应。"""
    from app.storage import StorageError

    db = tmp_path / "t.db"
    main = _app_for(tmp_path, monkeypatch, db)
    with TestClient(main.app) as c:
        c.post("/convert", json={"year": 2026, "month": 1, "sim_rain": SIM30})
        c.post("/seal", json={"through_day": 6})
        before = c.get("/table").json()

        original = main.store._insert_version_locked

        def boom(*a, **k):
            raise RuntimeError("模拟磁盘写入失败")

        monkeypatch.setattr(main.store, "_insert_version_locked", boom)
        r = c.post("/recompute", json={"approved": {20: 5}})
        assert r.status_code == 500
        monkeypatch.setattr(main.store, "_insert_version_locked", original)

        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM seals").fetchone()[0] == 1
        conn.close()
        after = c.get("/table").json()
        assert after["version"] == before["version"] == 1
        assert after["sealed_through"] == 6
        assert [d["amount"] for d in after["days"]] == \
               [d["amount"] for d in before["days"]]
    main.store.close()
