# interview-context-v0.1 — 上下文 A/B（旧裁剪 vs 结构化编译）

## 问题

S02A 之前，模型视图把 `question_history` 截到最近 6 条；第 7 条以前的问题从载荷里彻底消失。
S02A 之后，`compile_interview_context` 增加 `working_state`（已问主题、已问问题 id、未解决线索 id）。
问题是：**"早于窗口"的轮次，旧视图还能不能追溯？新视图能不能？**

产品每个版本只有一种视图，所以这不是运行时开关。基线被**逐字冻结**在
`legacy.py`（来自 `b1c657a^` 的 `agent.py:scope_view`），由测试同时调用新旧两份实现。

## 语料

不是数据文件，而是"同一份冻结快照"：12 个已回答轮次（前 6 轮 `java-concurrency`，后 6 轮
`sql-isolation`），每轮都有一条 `needs_probe` 观察，当前回答与已确认事实固定不变。
见 `backend/tests/test_interview_context_ab.py:snapshot()`。

## 怎么跑

```powershell
$env:PYTHONPATH='backend/src;backend/tests'
& <venv>/python.exe -m pytest backend/tests/test_interview_context_ab.py -q -s
```

不需要 PG / Redis / 模型。

## 说明与边界

- 旧视图**不是错的**：它按 32 KiB 预算裁剪，只是**没有回指**；S02A 补的正是"回指"。
- 代价是约 +2.1 KB（+45%）；同一快照远低于 32 KiB，不影响预算内的可用性。
- 只测"能否追溯"，不测模型是否会用这些 id；那需要实模评测。
- 基线只覆盖 S02A 那一刻的 `scope_view`；更早的版本不在本语料范围内。
