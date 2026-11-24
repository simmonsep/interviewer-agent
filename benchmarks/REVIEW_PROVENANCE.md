# Review Provenance（审查来源）

> 用途：纠正数据集元数据里的**枚举名与实际审查者不符**的问题。
> 任何报告、面试材料、对外说明，**以本文件为准**，不以 case/manifest 里的枚举为准。

## judgement-v0.5.0

```yaml
dataset: judgement-v0.5.0
frozen_at: 2026-11-12
review_status_field: human_reviewed        # ← legacy enum, 不代表有人类审核
actual_review_provenance: external_model_reviewed
human_domain_review: false
reviewed_by: 外部模型（external review），2026-11-12
review_verdict: APPROVE GOLD FREEZE
note: |
  schema 的 ReviewStatus 只有 human_reviewed / pending / ambiguous，冻结时按实际状态选了
  human_reviewed，但复核者是外部模型，不是人类领域专家逐条审核。枚举名会误导，故设本文件。
  v0.5.0 已冻结，不为了改枚举回写 cases.jsonl（那会改 checksum、破坏冻结语义）；
  下一个数据集 schema 增加 externally_reviewed 后再修正枚举。
```

### 可以怎么说

- ✅ “fixed benchmark with externally reviewed gold”
- ✅ “externally reviewed fixed gold set”
- ✅ “34 条 gold 已冻结，复核方为外部模型”

### 不可以怎么说

- ❌ “human-reviewed benchmark”
- ❌ “人类专家逐条审核过的金标”
- ❌ “经人类复核的评测集”

**升级条件**：由真人（我本人或真正的领域 reviewer）逐条确认 34 条 gold 后，
才能把 `human_domain_review` 改为 `true`，并在本文件记录复核人、日期与逐条结论位置。

## 其他数据集

| 数据集 | 冻结 | 审查来源 | 备注 |
| --- | --- | --- | --- |
| `judgement-v0.3.0` | 是 | 外部模型复核（3 轮） | 历史身份，不回写 |
| `judgement-v0.4.0` | 是 | 外部模型复核 | 被 v0.5.0 取代，冻结保留 |
| `judgement-v0.5.0` | 是 | 外部模型复核 + `APPROVE GOLD FREEZE` | 见上 |

## 与审核无关、但常被混淆的两件事

1. **任务/输出的 `review_status`**：`career-pilot` / `job-domain` 里的任务与输出是否经过
   人工审核，与 gold 的审查来源是两件事，不要互相代替。
2. **`semantic_validity`**：所有运行报告的该字段恒为 `not_established`——
   自动化判据通过不等于语义正确，任何审查来源都不改变这一点。
