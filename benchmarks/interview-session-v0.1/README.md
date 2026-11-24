# interview-session-v0.1

整场面试的**协议与 fixture 覆盖**基线。不是语义质量基线，不是模型能力评分。

- 提供方固定 `scripted`：不发起任何供应商 HTTP，不产生费用。
- `semantic_scoring_eligible: false`：所有样本 `pending_human_review`，语义通过率恒为 `null`。
- 生成器是仓库外的私有脚本；本目录是**冻结产物**，改动必须出新版本（`interview-session-v0.2`），不要就地改。

## 文件

| 文件 | 作用 |
| --- | --- |
| `manifest.json` | 版本、样本数、开发/留出拆分、`family_id → split`、三个数据文件的 SHA-256 |
| `cases.jsonl` | 10 个 case（8 开发 + 2 留出候选）：公共输入 + 候选人应答规则 |
| `oracles.jsonl` | 每个 case 的预期题序、最终状态、`stop_reason`、协议检查项、人工 rubric |
| `baseline-spec.json` | 冻结的版本组合（catalog/policy/project-summary/bank）、提供方语义、检查项定义、指标口径 |

`load_session_suite()` 逐个比对 `manifest['sha256']`，任何一字节变化都会抛
`session_suite_checksum`。

## 运行

```powershell
$env:PYTHONPATH='backend/src'
$env:PY_INTERVIEW_TEST_CONFIG='.private/postgres/test.json'
$env:PY_INTERVIEW_WORKER_TEST_CONFIG='.private/redis/test-worker.json'

# 离线校验（不需要数据库）
python -m interview_agent.evaluation.interview_session validate --root .

# 受控集成跑（真实测试 PG/Redis + 真实 FastAPI + 真实 scripted Worker，0 模型调用）
python -m interview_agent.evaluation.interview_session run --root . `
  --worker-config .private/redis/test-worker.json `
  --owner-config .private/postgres/test-migration.json
```

`run` 默认只跑 `development`；`reserved_candidate` 由入口拒绝，避免"留出集被反复调参"。

## 样本（family 即拆分单位）

| case | family | split | 覆盖 |
| --- | --- | --- | --- |
| IS01 | branch-substantive-switch | development | 回答充分后换主题推进 |
| IS02 | branch-vague-probe | development | 含糊回答后同主题追问 |
| IS03 | branch-technical-error-probe | development | 技术错误陈述后核验 |
| IS04 | coaching-vs-formal | development | 陪练：讲解 + 复测（`assisted=true`） |
| IS05 | coaching-vs-formal | development | 正式面试同输入同规则，不得出现讲解 |
| IS06 | no-material-ai-application | development | 无材料，不得出项目题 |
| IS07 | material-fact-fidelity | development | 项目题只引用冻结原文 |
| IS08 | duplicate-submission | development | 重复提交幂等 / 冲突正文 / 过期版本 |
| IS09 | reserved-ai-coaching | reserved_candidate | 预留：AI 应用岗陪练 |
| IS10 | reserved-user-stop | reserved_candidate | 预留：用户主动停止 |

IS04/IS05 共用 `pair_id=P-IS04-IS05`，`load_session_suite()` 校验两者除 `mode`
以外完全相同，防止"改了输入再说模式差异"。

## 候选人契约

`resolve_rule(case, question)` 按顺序取：**精确 `question_id` → `topic_id + kind` → 仅 `topic_id`**。
都不中时返回原因，而不是补一个答案：

- `agent_out_of_scope`：Agent 出了本 case 声明范围外的题（`allowed_topics/allowed_kinds`）；
- `fixture_uncovered`：题在范围内但本 case 没有为该题写规则。

两种都让该场记为 `evaluation_incomplete` 并计入 `unmatched`，不进入成功分母。

## 明确不覆盖

- scripted 面试官只按词面弱信号分支（`不知道/不清楚/只用SETNX/只加volatile`），
  识别不了"答非所问"，也判断不了技术正确性 —— 所以 IS03 的"识别错误"只能人工审核。
- 不含压缩、撤回、跨账号、Worker 故障恢复；这些在对应节点实现后另加回归。
- 2 个留出候选样本与开发样本共享题库/模板/仓库，**不是统计独立留出**；真正独立留出需复核者另行保留。
