# 评审请求：材料改写的"模型 / 确定性"边界该画在哪

> 需要你裁决的是**架构边界**，不是文案。请只依据本文；你看不到仓库，凡涉及代码我都贴了原文与位置。
> 相关事实已冻结在 `judgement-v0.5.0` 之外，不受影响。
> 证据强度先说明：**1 份真实简历、4 次尝试、1 次成功**。这是 failure discovery，不是统计。

## 1. 已经发生的事（真实简历，脱敏 1126 字）

材料改写链路（`draft_style=rewrite`）此前只在 8 条合成短句上验证过（每条 1–3 个要点），
C2 复核为「正文 0 violation、建议 0 写成事实」。换真实简历后：

| 尝试 | 结果 | 错误 |
| --- | --- | --- |
| 1–2 | 失败 | `one_material_tool_required` / `material_failed`（0 计费） |
| 3 | 失败 | `material_model_limit`（6 次响应用尽） |
| 4（旧 prompt） | **成功** | — |

成功那次的正文：**C1 检出违规 0、四族确定性守卫 0**，"修复 7-10 个 bug"和"性能提升一倍以上"
都原样保留（精度没有被放大），4 条建议全部"可补充…需你确认"，其中一条主动指出
"JD 要 React，材料只有技能栏有 React"——**没有编造**。

**但它只有 8 条、668 字**，以下内容全部丢失：

- 教育经历（院校/专业/排名区间 `20%~50%`）；
- 项目经历（一个完整项目）；
- 个人技能（Node/Java、Vue/React、MySQL…）；
- **全部起止年月**。

## 2. 代码事实（逐字）

### 2.1 正文条数上限 = 8

```python
# materials/contracts.py:114-115, 138
class RewriteArgs(Contract):
    items: Annotated[list[RewriteItem], Field(min_length=1, max_length=8)]

class MaterialResult(Contract):
    rewrite_items: Annotated[list[RewriteItem], Field(max_length=8)] = []
```

### 2.2 正文只能由 rewrite_items 渲染

```python
# materials/contracts.py:194
return '# 项目经历\n\n' + '\n'.join('- ' + escaped(item.text) for item in result.rewrite_items) + '\n', {...}
```

即：**日期、学历、技能想出现在正文里，只能由模型写成一条 `item`**。

### 2.3 输出预算是 2048，且失败被折叠成一个错误码

```json
// configs/core0615-materials.json
{ "max_output_tokens": 2048, "max_payload_bytes": 32768 }
```

```python
# runtime/deepseek.py:139
if value.invalid_tool_calls or value.additional_kwargs.get("reasoning_content"):
    raise ScopeError("invalid_provider_output")
```

## 3. 我试过的"最容易的解"，失败了

把上限 8 → **24**，并加一条"覆盖全部 section、日期与技能必须保留"的规则，重跑真实简历：

```
material_provider_or_graph_error | exception_type = ScopeError（3 次调用）
```

降到 **12** 条、允许同 section 合并，重跑：**同样 `ScopeError`**。

我的结论：**正文 + 每条 `{text, source_ids, change_note}` 的 JSON 在中文环境下超过 2048 输出 token，
工具调用被截断。条数不是瓶颈，输出预算才是。** 该改动已回退，生产重新等于通过 C2 的文本。

## 4. 四个候选

| 方案 | 做法 | 成本 | 我的判断 |
| --- | --- | --- | --- |
| **B 结构化透传** | 教育/日期/技能由**代码**从 source 直接写进 markdown，模型只改叙述 | 0 元调用成本，需前端接受段落结构 | **推荐**：不受输出预算约束，同时解决"丢内容" |
| C 分块改写 | 按 section 分多次调用，每次输出都短 | 每次运行 ×N 次调用 | 叙述部分也要全覆盖时的补充 |
| D 提高输出预算 | `max_output_tokens` 2048 → 4096 | 0 元改代码，但**每次调用更贵**（且 `Literal[2048]` 是计费边界） | 只在确实需要模型写长正文时考虑 |
| A 只放宽条数 | 已试，失败 | — | 不成立 |

## 5. 请你只回答这四个封闭问题

1. **B 是否成立？** 即："结构化内容（日期/学历/技能）由确定性代码透传、模型只改叙述"，
   是否比"让模型写全部正文"更符合本项目既定的「LLM 提议 + 确定性执行」原则？
2. **如果 B 成立，正文的段落结构由谁定？**
   (a) 代码固定 section（教育/工作/项目/技能）；
   (b) 模型标注 section，代码按标注渲染；
   (c) 保持单段"项目经历"，只在条目内保留原文结构。
   请选一个，并说明理由。
3. **D 现在是否该做？** 提高输出预算会让每次调用更贵，但也能让模型一次写完更长的正文。
   你建议先 B 再评估 D，还是直接 D？
4. **"正文条数上限"在 B 之后还需要放宽吗？** 如果结构化内容已经不走模型，剩下的叙述条目
   8 条是否够用？

## 6. 如果你认为我判断错了

我可能错在：把 `ScopeError` 归因于输出长度。代码里 `ScopeError` 同时覆盖
`invalid_tool_calls` 和 `reasoning_content` 两种情况，我没有区分，也没有抓到被截断的原始响应。
如果你认为"输出预算不是原因"，请指出应该先验证什么——我会照做，而不是继续换方案。

## 7. 边界（必须一并考虑）

- 1 份简历、1 次成功：**不能**据此估计失败率；
- 旧 prompt 的对照**没做成**（两次都在协议层失败、0 计费），所以"新 prompt 更好或更差"无结论；
- 8 条合成用例从未触发这些形状，说明**冻结 benchmark 通过 ≠ 真实输入可用**。
