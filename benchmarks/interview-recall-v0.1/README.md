# interview-recall-v0.1 — 跨场检索 A/B

## 这个语料回答什么问题

S02 的验收表第一行是："较早相关观察和近期无关观察 → 同一冻结目标下，陪练选前者，
与旧的纯时间排序对照。" 这条一直没有可运行的对照。本语料把问题写成可量化的两条路径：

| 臂 | 实现 | 行为 |
| --- | --- | --- |
| **A** | `InterviewRepository` 不带 projector | 纯 SQL：`ORDER BY created_at DESC LIMIT 6` |
| **B** | 同一个仓储 + `MemoryProjector` | Store 按主题取候选，再用同一条 SQL 复核（存在、已完成、同角色、未撤回） |

两臂都跑真实 PostgreSQL 与真实 LangGraph Store，不调模型，**成本 0 元**。

## 语料

`cases.jsonl` 每个 case 用 `age_hours` 控制观察时间，用 `pack_topics` 控制"相关主题"，
所以"更早但更相关"是事实，不是期望。`manifest.json` 记录 `cases.jsonl` 的 sha256，
测试启动时校验（语料被改动必须同时改 hash，否则测试直接失败）。

| case | 形状 | 看点 |
| --- | --- | --- |
| R01 | 1 条 720h 前的相关观察 + 6 条 1–6h 前的无关观察 | A 臂完全错过相关项 |
| R02 | 3 条相关（800h / 700h / 30h）+ 5 条近期无关 | A 臂只捞到 30h 那条 |
| R03 | 没有相关观察 | B 臂必须回落到 SQL，不能变成"没有历史" |
| R04 | 相关项已撤回 | 先投影再撤回：残留 Store 命中被 SQL 复核丢弃；重投影后回落到 SQL |

## 怎么跑

```powershell
$env:PYTHONPATH='backend/src;backend/tests'
$env:PY_INTERVIEW_TEST_CONFIG='<repo>/.private/postgres/test.json'
$env:PY_INTERVIEW_WORKER_TEST_CONFIG='<repo>/.private/redis/test-worker.json'
& <venv>/python.exe -m pytest backend/tests/test_interview_recall_ab.py -q -s
```

`-s` 会打印逐 case 的 `sql` / `store` / `store_after_reprojection` 三列 id。

## 边界

- 语料是**合成观察**（答案文本固定），只测"检索是否把相关项送进来"，
  不测模型是否会用它，也不测能力判断。
- 每个 case 用一个新目标；同一目标里跨 case 混跑会让"最近 6 条"互相挤占，
  这正是 A 臂的语义，不应拿来做对照。
