"""端到端冒烟：转换 -> 封存 -> 补报继承 -> 加封 -> 按版查询 -> 无解保原表。"""

import json
import os
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def call(method, path, payload=None, expect=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
            code = r.status
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        code = e.code
    if expect is not None:
        assert code == expect, f"{method} {path} -> {code}: {body}"
    return code, body


def seals_of(version):
    _, body = call("GET", f"/versions?version={version}&include_seals=true",
                   expect=200)
    return body["versions"][0]["seals"]


SIM = [3, 0, 7, 11, 25, 1, 0, 9, 4, 40,
       2, 6, 8, 0, 13, 5, 5, 100, 0, 2,
       7, 7, 3, 0, 1, 9, 12, 30, 0, 6]
print("sim_total =", sum(SIM))

# 1) 初始转换（2026-01，31 天，核准第 1 日=5）
c, t1 = call("POST", "/convert",
             {"year": 2026, "month": 1, "sim_rain": SIM, "approved": {"1": 5}},
             201)
print("v1: version=%s days=%d total=%d sealed=%s"
      % (t1["version"], len(t1["days"]), t1["table_total"],
         t1["sealed_through"]))
assert t1["table_total"] == sum(SIM)

# 2) 封存到第 10 日：只新增一条封存记录
c, seal1 = call("POST", "/seal", {"through_day": 10}, 200)
print("seal1:", {k: v for k, v in seal1.items() if k != "days"})
sealed_snapshot = [(d["day"], d["amount"], d["source"]) for d in seal1["days"]]
assert len(sealed_snapshot) == 10

# 3) 补报未封存日 -> v2 沿用 v1 的原封存记录（编号不变）
c, t2 = call("POST", "/recompute", {"approved": {"20": 15, "31": 8}}, 201)
print("v2: version=%s sealed=%s total=%d"
      % (t2["version"], t2["sealed_through"], t2["table_total"]))
assert t2["sealed_through"] == 10 and t2["table_total"] == sum(SIM)
assert [(d["day"], d["amount"], d["source"]) for d in t2["days"][:10]] \
    == sealed_snapshot
v2_seals = seals_of(t2["version"])
assert [s["seal_id"] for s in v2_seals] == [seal1["seal_id"]]
print("v2 inherited seal ids:", [s["seal_id"] for s in v2_seals])

# 4) 在 v2 上加封到第 18 日：只影响 v2 及其后续版本
c, seal2 = call("POST", "/seal", {"through_day": 18}, 200)
print("seal2:", {k: v for k, v in seal2.items() if k != "days"})
assert seal2["day_from"] == 11 and seal2["day_to"] == 18
assert [s["seal_id"] for s in seals_of(1)] == [seal1["seal_id"]]
assert [s["through_day"] for s in seals_of(t2["version"])] == [10, 18]

# 5) 再补报 -> v3 继承两段封存，按版查询直接给出原编号
c, t3 = call("POST", "/recompute", {"approved": {"25": 3}}, 201)
v3_seals = seals_of(t3["version"])
print("v3 seals:", [(s["seal_id"], s["through_day"], s["sealed_at"])
                    for s in v3_seals])
assert [s["seal_id"] for s in v3_seals] == \
       [seal1["seal_id"], seal2["seal_id"]]
assert t3["sealed_through"] == 18

# 6) 再次 /convert（同年月）-> 全新未封存日表
c, t4 = call("POST", "/convert",
             {"year": 2026, "month": 1, "sim_rain": SIM}, 201)
print("convert again: version=%s sealed=%s seals=%d"
      % (t4["version"], t4["sealed_through"], len(seals_of(t4["version"]))))
assert t4["sealed_through"] == 0 and seals_of(t4["version"]) == []

# 7) 无解请求 -> 422，原表 v4 继续供应
c, infeasible = call("POST", "/recompute", {"approved": {"1": 99999}}, 422)
print("infeasible ->", c, infeasible["detail"][:40], "...")
c, cur = call("GET", "/table", expect=200)
assert cur["version"] == t4["version"] and cur["table_total"] == sum(SIM)

# 8) 按版取数、累计与封存标记
c, old = call("GET", f"/table?version={t2['version']}", expect=200)
assert old["sealed_through"] == 18 and old["table_total"] == sum(SIM)
c, cum = call("GET", f"/cumulative?through_day=15&version={t2['version']}",
              expect=200)
print("v2 cumulative@15:", cum["cumulative_amount"], "sealed=", cum["is_sealed"])
assert cum["is_sealed"] is True

print("\nSMOKE OK")
