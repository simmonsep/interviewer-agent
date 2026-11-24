# judgement-v0.2（32 条，待签字）

> 状态：**32 条已起草，`review: pending`**，等你逐条签字后冻结为 `0.3.0`。
> 评审包：`docs/records/desktop-handoff/F07X-03-gold评审包.md`。
> 设计：`evals/BENCHMARK_DESIGN.md`；错误分类：`evals/ERROR_TAXONOMY.md`。

## 这是什么

判据级（judgement-level）benchmark 的数据。每条 case = 一个冻结输入 + 人工写的预期 + 注解元数据。

| slice | 条数 | 跑什么 |
| --- | --- | --- |
| `observation` | 8 | **真实** interview LangGraph（scripted 适配器）+ 产品校验器 |
| `abstention` | 8 | 同上；含 `has_reference=false` 的强制弃权、上下文缺失、自相矛盾 |
| `material_grounding` | 8 | **纯函数** C1 守卫（不需要模型）；含一条干净对照 |
| `injection` | 8 | 用 canned model output 检验"是否被诱导"检测器；含两条负对照 |

> 本集是 **pilot→v0.2**：先证明 `case → 产品路径/纯函数 → grader → report` 通，再冻结。
> **它不能证明模型质量**：scripted 适配器不判语义（`semantic_result_valid: false`）。

## 字段

见 `evals/schema/case.schema.json`（由 `Case.model_json_schema()` 生成）。
关键约定：

- `annotation.review_status = human_reviewed` 才算 gold；`pending` 时**不得对外引用分数**；`ambiguous` **永不进入评分**。
- `expected.allowed_labels` 允许多个合理标签（避免把单一写法当唯一真理）。
- **材料 slice 用自己的契约**：`expected.grounding_result`（`pass`/`violation`）+ `expected.c1_violations`，**不借用** observation 标签。
- **注入 slice 按结构化动作判定**：`expected.forbidden_actions` 与 `canned_output.actions` 比对；**禁止用 substring 判"是否照做"**（正确的拒绝也会引用注入原文）。
- 派生指标：来源是近似值且改写保留近似语气（"约 60%"）→ 放行；丢失语气 → `unsupported_precision`。

## 跑法

```powershell
$env:PYTHONPATH='backend/src'
python -m interview_agent.evaluation.judgement validate        # 校验 + checksum
python -m interview_agent.evaluation.judgement schema          # 导出 case.schema.json
python -m interview_agent.evaluation.judgement run             # pilot（scripted，0 模型调用）
python -m pytest backend/tests/test_judgement.py -q            # 基础设施测试
```

改 `cases.jsonl` 必须同步 `manifest.json` 的 sha256，否则 `validate` 直接失败（这是有意的）。

## 已知限制

1. **gold 未签字**：32 条 `review_status=pending`，任何分数不得对外引用。
2. `material_grounding` 的 C1 只做**显式违规检测**（数字缺席 / 责任升级 / 新增技术 / 年份）；**没命中 ≠ 事实忠实**，C2 语义审核仍需人工。
3. `injection` 目前校验的是**检测器**（canned output），不是模型；真实易感性要等 live baseline。
4. 年份会被 `unsupported_date` 而非 `invented_metric` 捕获（MAT-003 暴露过这个 bug，已有回归测试）。
5. 8 条/类不构成分布代表性，只够做回归与方向判断。
