# REVIEW REQUEST：`needs_probe` 与 `inconclusive` 是否还应保持区分

> 状态：待外部评审。**不要**在裁决前改 gold。
> 相关：`evals/BENCHMARK_DESIGN.md` §4.1、`evals/GRADER_CONTRACT.md`、`evals/BASELINE_REVIEW.md`、`benchmarks/judgement-v0.4/`
> 数据集 0.4.0 仍 `review_status=pending`。

## Context

判据级 benchmark（judgement-v0.4.0，34 条）第一次 live 基线跑完后，`abstention` 子集稳定出错。经过三轮重复与一次输入改写，错误的形状稳定下来，指向**标签口径本身**而不是模型行为。

现在需要裁决：`needs_probe` 与 `inconclusive` 这两个标签在产品里**是否还应该分开**。

## Current repository facts（都已核对代码）

1. **枚举只有三个**：`interviews/contracts.py:98` → `needs_probe` / `supported_in_this_answer` / `inconclusive`。
2. **`inconclusive` 只在一种情况下被产品强制**：题目的 `has_reference=false`（`contracts.py:138-140`），此时必须 `inconclusive` 且禁止 `teach_and_retest`。
3. **没有参考 = reference 为空**：`question_banks/repository.py:129` → `has_reference = bool(item['reference'])`。
4. **标签几乎不影响流程**：
   - 决定下一步动作的是 `Decision.action`（ask_question / teach_and_retest / finish），不是 label；
   - **协调器把两者当同一个东西**：`applications/planner.py:35` → `if p['label'] in ('needs_probe','inconclusive'): action('retest_topic', …)`；
   - label 的第二处影响是项目题 summary 的尾句（`contracts.py:17`：`needs_probe` 才加"建议补充具体证据继续核验"）与只读报告的聚合。
5. **提示词已加过显式规则**（F07Z-01）："上下文不可用 → inconclusive；回答不足 → needs_probe"。**实测无效**（见下）。

## Evidence

### E1 三轮重复（同 16 条，observation + abstention）

| 轮次 | 改动 | 标签正确 |
| --- | --- | --- |
| A | 无 | 12/16 |
| B | 无 | 11/16 |
| C | 加 Q0 提示词规则 | 12/16 |

- 翻转 3/16（19%）：`ABS-001`、`ABS-003`、`OBS-007`（**非 abstention 也翻**）；
- **三轮全错**：`ABS-005`（"我不太清楚"→模型弃权）、`ABS-007`、`ABS-008`。

### E2 v0.4.0 改写输入后的一次 live（abstention 10 条）

把 `ABS-007/008` 的参考改成**显式声明缺失**（"本场没有提供…"），不放宽 gold；另加 `ABS-009/010` 作 `ABS-005` 同族。

| case | 期望 | 实测 |
| --- | --- | --- |
| ABS-001 / 002 / 003 | `inconclusive` | ✅ `inconclusive` |
| ABS-004 / 005 / 006 | `needs_probe` | ✅ `needs_probe` |
| **ABS-007 / 008** | `inconclusive` | ❌ **仍 `needs_probe`** |
| ABS-009 / 010（新） | `needs_probe` | ✅ `needs_probe` |

结果 8/10，0 协议失败。

### E3 观察

- "回答空/没做过"这一类（ABS-005/009/010）**是稳定的**，模型一致判 `needs_probe`；
- "上下文缺失/问题歧义"这一类（ABS-007/008）**即使参考明说"什么都没提供"**，模型仍判 `needs_probe`；
- 也就是说：**面对缺失的上下文，模型的动作就是"去问"**。

## Concern / disagreement

我（维护者）认为：当"缺上下文"的最佳动作本来就是"追问"时，`inconclusive` 与 `needs_probe` 在**行为上不可区分**；而仓库里 `applications/planner.py:35` 也确实把两者合并处理。既然如此，继续要求模型区分它们，可能是在考一个**产品自己都没用上的口径**。

反对意见（也在理）：保留区分能让报告更诚实——"我不知道该判什么"与"这条回答不够"是两种状态；抹平会丢信息。

## Options considered

### A. 收窄 `inconclusive` 到"产品强制场景"，其余并入 `needs_probe`

- 定义：`inconclusive` **只**用于 `has_reference=false`（产品已强制）；"缺上下文 / 问题歧义 / 回答空" 一律 `needs_probe`。
- 影响：`ABS-001/003/007/008` 的 gold 改为 `needs_probe`；`ABS-002` 保持 `inconclusive`（唯一锚点）。
- 代价：abstention slice 的"语义区分"变薄（只剩产品契约一条）；但**与真实行为一致**。
- 收益：不再要求模型猜一个没有行为后果的枚举；错误集中到"该不该追问"这个真问题上。

### B. 保留区分，改模型侧（示例 / 后置校验）

- 用 few-shot 示例或确定性后置校验强制"上下文缺失 → inconclusive"。
- 代价：提示词变长 / 加产品逻辑；F07Z-01 已证明"加一句规则"无效。
- 收益：保留语义区分；但**用户能感知到的差别依然为零**（planner 合并、动作由 action 决定）。

### C. 什么都不改，把它记为已知产品缺陷

- 诚实，但等于承认 benchmark 里有 4/10 的用例在考一个无行为意义的标签。

## My recommendation

**选 A**，理由按强度排序：

1. **行为等价有代码证据**（`applications/planner.py:35`），不是我的推断；
2. 模型在 E2 里已给出**更好的行为**（缺上下文就追问），我们却因为它没选另一个等价标签判它错；
3. 保留区分只会让 abstention 分数长期停在 4–8/10，且无法通过 prompt 修复（E1 已证）。

如果选 A，我会把 `ABS-007/008`（以及 001/003）的 `allowed_labels` 收紧为 `needs_probe`、把 `ABS-002` 作为唯一的"强制弃权"锚点，并在 `GRADER_CONTRACT.md` 写死"`inconclusive` 仅用于 `has_reference=false`"。

## Questions for reviewer

1. 你是否同意"**行为等价即无需区分**"这条判据？如果不同意，请指出在哪个用户可观测路径上两者会产生不同结果（我核对过 planner / contracts / 报表，未找到）。
2. 若选 A，`ABS-002`（`has_reference=false`）作为唯一 `inconclusive` 锚点是否足够？是否还需要第二条同族样本？
3. 若选 B，你能给出**可验证的**改动形式吗（few-shot / 后置校验 / 结构化字段），而不是"再加一句规则"——后者已被 F07Z-01 证伪（12/16 → 12/16）。
4. `inconclusive` 在报告里的价值（"不足以判断" vs "不够好"）是否值得用 4 条永远失败的用例去守住？
5. 如果选 A，`OBS-007`（无来源数字，曾在两轮里翻成 `inconclusive`）是否也应写死为 `needs_probe`，并在 prompt 里保留"冲突/无来源事实属 needs_probe"这句？
6. 本方案还有个未决项：material 的 `负责 X → 方案设计与落地` 是否算责任膨胀（C1 不判、C2 才判）。你建议进 v0.5 的 `must_not_add_activities` 吗？

## Budget note

本阶段批准 0.5 元，实际用到 **约 0.519 元**（超支 0.019）。原因是我的 `--cases` 过滤写成 `getattr(dict)` 恒为 None，本意跑 5 条跑成 10 条。已在 `F07Z-02` 记录；后续 live 需要追加授权，裁决本身不花钱。
