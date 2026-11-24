# 气象数仓 · 模拟降雨转公历日表服务

单站单月的 30 个模拟日雨量（十分之一毫米，非负整数）按两种日历的重叠比例
拆成公历日表，支持核准日照录、版本封存、补报更正。FastAPI + SQLite，
Docker Compose 一键启动。

## 1. 核心规则与算法

### 1.1 日历对齐与权重（`app/alloc.py::overlap_weights`）

- 模拟月固定 30 个模拟日，公历月按 `calendar.monthrange` 取 28~31 天；
  两种日历**月首、月末对齐**，时间轴归一化到同一月长。
- 模拟日 `i` 覆盖 `[i·L, (i+1)·L)`，公历日 `d` 覆盖 `[d, d+1)`，
  `L = 公历天数 / 30`；权重 = 重叠长度 / 模拟日长度，全部用
  `fractions.Fraction` 精确计算，无浮点误差。
- 每个模拟日拆出的权重和为 1；每个公历日收到的权重和为 `30/公历天数`。

### 1.2 核准与剩余量分摊（`app/alloc.py::allocate` / `allocate_suffix`）

- 核准公历日照录核准整数；剩余月量 `模拟总量 − 核准总量` 在未核准日之间
  按其重叠权重的精确份额比例分摊（权重为 0 而仍有剩余量时判无解）。
- 补报重算用 `allocate_suffix`：只重算**未封存后缀**，以封存截止日与封存
  累计量为输入，前缀不读出、不重算（见 §2.2）。两条路径整数化规则一致。
- 无解情形（422，不写库）：模拟值不是 30 个/含负数、核准日越界或落在
  封存范围内、核准总量超过可分配量、剩余量无处分摊。

### 1.3 整数化：平移向下取整，保证任意连续区间误差 < 1

设 `E_d` 为前 d 天精确累计，整数日表取
`x_d = floor(E_d) − floor(E_(d−1))`（总量为整数，故 `E_n` 为整数）。
前缀误差 `e_d = floor(E_d) − E_d ∈ (−1, 0]` 且 `e_0 = e_n = 0`，
于是任意连续区间 `[a,b]` 的累计误差 `e_b − e_(a−1) ∈ (−1,1)`，
即**严格小于一个单位**。同时天然保证：日值非负整数、整月总量与模拟值
完全相等、核准日（精确份额为整数）取整后仍是原值。每次分配后
`_verify` / `_verify_suffix` 全枚举区间做终检。

## 2. 版本、封存与补报（`app/storage.py`、`app/main.py`）

### 2.1 数据模型（SQLite / WAL）

| 表 | 作用 |
|---|---|
| `versions` | 版本元数据 + `seal_head_id`（该版有效封存链的表头） |
| `day_rows` | 该版**自身保存**的日值（未封存后缀）+ 来源 + 精确份额 |
| `seals` | 封存记录链表：`prev_seal_id` 指向上一段，`sealed_amount` 为累计快照 |
| `current_version` | 现行版号（单行） |

**两个“按引用继承”**是整个设计的关键：

1. **封存链继承**：`/seal` 只 `INSERT` **一条** `seals` 记录，把 `prev_seal_id`
   指向该版当前表头，再让 `versions.seal_head_id` 指向新记录。新版本补报时
   直接把 `seal_head_id` 复制过去——O(1) 的指针赋值，不复制任何封存行。
2. **封存日值继承**：每个版本只保存 `sealed_through+1` 日起的后缀日值；
   已封存前缀不复制、不重写，读取时沿封存链回到当时保存它的版本拼接。

因此**继承所需的读写量与日记录数、版本数无关**：无论补报多少次、日志多长，
只有“每次 `/seal` 恰好新增一条记录”，补报版本只写自己的未封存后缀。

### 2.2 补报重算（`POST /recompute`）

- 触及已封存日（`day ≤ sealed_through`）直接 422 拒绝；
- 只重算未封存后缀，前缀值与其**来源标签**由封存链原样继承——封存只冻结
  数值与当时的来源，补报不会改动它；
- 日表与继承的封存在**同一事务**内生效（`BEGIN IMMEDIATE`），任何异常整体
  回滚：不产生半成品版本、不改现行版号，调用方继续拿到原表；
- 新版自动切为现行，旧版永久可查。

### 2.3 补报历史上的加封

- 在某一版上 `/seal`：只把该版（及之后从它补报出的版本）的表头指向新记录。
- 更早的版本**不受影响**——它们的 `seal_head_id` 没变，仍只看到自己当时的
  封存范围。这满足“后来加封只影响当时的现行版及从它继续补报的版本”。

### 2.4 每次 `/convert` 都是一份新表

- `POST /convert` 每次都新建一个版本并切为现行，**天然未封存**（`sealed_through=0`），
  即使年月与已有版本相同；旧版本及其封存保持不变、仍可按 `version` 查询。

### 2.5 旧库兼容（就地迁移）

- 启动时 `_migrate_locked()` 为旧库补列（`versions.seal_head_id`、
  `seals.prev_seal_id`、`seals.sealed_amount`），并按版本回填封存链；
  `PRAGMA user_version` 记录升级版本，重复启动幂等。
- 旧库的**版本号、`day_rows` 主键、封存记录编号全部原样保留**；旧版仍按
  自身完整日表查询与继续补报，无需重建库。

## 3. API 一览（启动后见 `/docs` 在线试用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/convert` | 初始转换，生成**未封存**新版本并切为现行（201） |
| POST | `/seal` | 封存现行版本从上次封存日之后到指定日；每次只新增一条记录 |
| POST | `/recompute` | 补报/更正（限未封存日），只写后缀并继承封存前缀 |
| GET | `/table?version=&source=` | 取有效日表，缺省取现行；`source` 区分核准/估算 |
| GET | `/cumulative?through_day=&version=` | 月初到指定日累计量，标注是否已封存 |
| GET | `/versions?version=&include_seals=` | 版本线、封存情况、现行标记；可附封存明细 |
| GET | `/seals?version=` | **按版直接查封存记录**：原编号、截止日、封存时间 |
| GET | `/health` | 健康检查 |

### 请求示例

```bash
curl -X POST localhost:8000/convert -H 'Content-Type: application/json' -d '{
  "year": 2026, "month": 1,
  "sim_rain": [3,0,7,11,25,1,0,9,4,40,2,6,8,0,13,5,5,100,0,2,
               7,7,3,0,1,9,12,30,0,6],
  "approved": {"1": 5}
}'
curl -X POST localhost:8000/seal -H 'Content-Type: application/json' \
     -d '{"through_day": 10}'
curl -X POST localhost:8000/recompute -H 'Content-Type: application/json' \
     -d '{"approved": {"20": 15, "31": 8}}'
# 按版查封存：返回该版有效封存记录的原编号、截止日、封存时间
curl 'localhost:8000/seals?version=2'
curl 'localhost:8000/versions?version=2&include_seals=true'
```

`/seals` 响应示例（第 2 版继承第 1 版封存，并在本版加封到第 18 日）：

```json
{
  "version": 2, "sealed_through": 18, "sealed_amount": 174, "count": 2,
  "seals": [
    {"seal_id": 1, "through_day": 10, "sealed_at": "2026-11-24T01:32:27Z",
     "day_from": 1,  "day_to": 10, "sealed_on_version": 1},
    {"seal_id": 2, "through_day": 18, "sealed_at": "2026-11-24T01:32:27Z",
     "day_from": 11, "day_to": 18, "sealed_on_version": 2}
  ]
}
```

封存记录编号是**原记录编号**，跨版本沿用同一 id，不会因补报生成副本；
查询只读该版表头一条链，无需沿补报历史逐版回找。

## 4. 运行

```bash
docker compose up --build        # 服务在 http://localhost:8000，文档 /docs
# 数据持久化在命名卷 rain-data（/data/rain.db）
```

无 Docker 时本地运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
RAIN_DB_PATH=./data/rain.db uvicorn app.main:app --port 8000
```

## 5. 自测

```bash
pip install -r requirements.txt pytest httpx
RAIN_DB_PATH=/tmp/test.db python -m pytest tests/ -q   # 42 项
python3 -I tests/smoke_e2e.py                          # 端到端冒烟（需先起服务）
```

- `tests/test_alloc.py`：权重平衡、28/29/30/31 天月守恒、任意连续区间
  误差严格 <1（Fraction 精确断言）、核准值保留、各类无解、非法输入；
  `allocate_suffix` 与整月算法结果一致、封存内核准被拒。
- `tests/test_api.py`：转换/封存/补报全流程、封存日拒绝更正、
  封存值与来源标签保留、无解保原表、取数过滤；以及封存继承模型：
  每次 `/seal` 只新增一条、补报沿用原封存编号、后加封只影响现行版与
  后继版本、重复 `/convert` 生成未封存新表、`/seals` 按版查询。
- `tests/test_migration.py`：旧库就地迁移且编号不变、迁移幂等、
  补报版本只存后缀（行数断言）、发版失败整事务回滚、有效日表与整月
  重算逐日一致。

## 6. 实测结果（本环境）

- `pytest`：**42 passed**。
- 不变式随机压力：**400/400** 通过（随机转换/封存/补报序列，逐版校验
  总量守恒、精确份额守恒、任意连续区间误差 <1、封存快照累计一致、
  每次 `/seal` 恰好一条记录、继承封存编号不重复）。
- 规模验证：201 个版本、4231 行日值，`seals` 仅 **1 行**；`/seals` 的
  封存查询为 `versions` / `seals` 各自的主键点查（`EXPLAIN QUERY PLAN`
  显示 `SEARCH ... USING INTEGER PRIMARY KEY`），不随版本数增长。
- 端到端冒烟（uvicorn + 真实 SQLite）：v1 转换 → 封存至第 10 日（1 条记录）
  → 补报得 v2（沿用 seal_id=1）→ v2 加封至第 18 日（新增 seal_id=2，`day_from=11`）
  → 补报得 v3（继承 [1,2]）→ 再次 `/convert` 得 v4（`sealed_through=0`，无封存）
  → 无解请求 422、v4 继续供应 → 按版累计与封存标记正确。
- 实测库结构：v1 存 31 行、v2 存 21 行（11..31）、v3 存 13 行（19..31）、
  v4 存 31 行；`seals` 共 2 行（对应两次 `/seal`）。
- `/docs` 与 `/openapi.json` 返回 200；`/versions`、`/seals` 参数已在 OpenAPI 中。

## 7. 未完成项 / 边界说明

- 本沙箱无 Docker 守护进程，`docker compose build/up` 未实机执行；
  Dockerfile 与 compose 文件沿用可启动配置，容器内启动命令与本地实测一致，
  需在有 Docker 的环境补一次冒烟。同一沙箱还残留一个后台 uvicorn（端口 8011），
  受权限限制无法终止，与交付代码无关。
- 服务定位为**单站单月版本线**：历年月各自独立版本，可共存；跨站的
  同月数据需引入站点维度（当前数据模型未含）。
- 封存只支持“从上次封存日继续到指定日”的**前缀式**封存；封存区间不可
  回退或撤销（如需更正要走新的补报版本并保留原封存）。
- 未做鉴权/限流，内网或网关后部署为宜。
