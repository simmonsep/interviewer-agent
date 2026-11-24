# interview-observation-v0.1：observation 判据固定样本集

## 这是什么

F07N P0 §2.2 要求的"固定评测样本集"。8 条合成样本，覆盖终审点名的 8 种情况：

| kind | 想验证什么 | 人工预期标签 |
| --- | --- | --- |
| `correct` | 答对要认 | `supported_in_this_answer` |
| `partial` | 方向对但没说到关键，要追问 | `needs_probe` |
| `incorrect` | 明确错误不能被放过 | `needs_probe` |
| `insufficient` | 承认不清楚，只能判"不足以判断" | `inconclusive` |
| `fabricated_number` | 来源里没有的数字不能被当成事实 | `needs_probe` |
| `ambiguous_contribution` | "参与"不能被升格成"主导" | `needs_probe` |
| `concept_confusion` | 可见性≠原子性，属理解缺口 | `needs_probe` |
| `evidence_conflict` | 材料与回答矛盾，交用户核实 | `needs_probe` |

## 为什么答案里不写死题干和参考

样本只写 `role` + `topic_id` + `question_kind`，题干与参考在运行时从 `interviews.catalog.pack(role)` 取。
这样角色包一改，样本不会和题库文本悄悄漂移；同时保证"引用必须落在冻结参考里"这条校验用的是真参考。

## 三条评分维度（见 `rubric.json`）

1. **label**：模型给的 observation 标签是否等于人工预期；
2. **checks**：`summary` 里有没有点名该样本的关键缺陷（同义词任一命中即算）；
3. **forbid**：`summary` 里有没有出现不该有的断言（如把未核验数字说成"已确认"）。

`passed` 需要 label 对 + action 合法 + checks 全中 + 无 forbid 违规。

## 明确不成立的地方

- **关键词匹配不是语义判分**。命中同义词不代表真的说对了；漏命中也不代表模型一定没提。它只能筛"明显漏判"和"明显误判"。
- **还没有人工审核**（`review: pending`）。这份 rubric 是单人草稿，不是经过复核的评分标准。
- **不是独立留出集**（`visibility: public_suite_not_holdout`）。样本和预期都写在仓库里，调过参就不能再当留出用。
- 样本是合成的，**不含任何真人材料**。

## 跑法

```powershell
$env:PYTHONPATH='backend/src'
# 只校验样本完整性（0 模型调用）
python -m interview_agent.evaluation.observation validate
# 免费基线：走真实 interview LangGraph + 真实校验，用产品自带的 scripted 适配器
python -m interview_agent.evaluation.observation run
# 真实模型（已接线，fail-closed）：必须显式给出预算、配置与凭据，且只用开发库
python -m interview_agent.evaluation.observation run-live --repeats 2 `
  --api-config .private/postgres/dev.json `
  --worker-config .private/redis/dev-worker.json `
  --owner-config .private/postgres/migration.json `
  --policy configs/f07q-interviews.json `
  --credentials .private/live-credentials.txt `
  --max-cny 0.5
```

`run-live` 走真实 API + 真实 interview worker + 真实账本（`app.model_http_calls`），首题由 scripted 生成（免费），只有"判定回答"这一步计费（每条 ≤4 HTTP）。`LivePolicy` 单条授权上限 12 HTTP 不抬高，改为按批（默认每批 3 条）各自授权；0.5 元总量由运行期累计计数兜底。

产物落在 `.private/f07q/observation/<run-id>/`（`run.json` / `attempts.jsonl` / `report.json`），不入库、不进 Git。
