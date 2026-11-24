# 评审请求：材料改写里的"复现"要做到什么程度，走哪条路线

> 需要你裁决的是**保真路线的选择**，不是文案好坏。请只依据本文；你看不到仓库，凡涉及代码我都贴了原文与位置。
> 冻结资产 `judgement-v0.5.0` 与本议题无关，不受影响。
> 证据强度先说清：**1 份真实脱敏简历、1 次成功改写的 1 份草稿**。这是缺陷发现，不是统计。

## 1. 现在的链路做了什么（代码事实）

用户上传自由文本材料 → `POST /materials/rewrite` → live materials worker → 模型调用一次 `submit_rewrite` →
**代码**按固定 section 渲染 markdown。

模型返回的参数结构（`backend/src/interview_agent/materials/contracts.py:119-127`，逐字）：

```python
class RewriteArgs(Contract):
    items: Annotated[list[RewriteItem],Field(min_length=1,max_length=8)]
    # Required keys, possibly empty lists: the model must decide explicitly rather
    # than omit the field, and the renderer owns where they appear.
    education: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=8)]
    experience: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=8)]
    projects: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=8)]
    skills: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=8)]
    suggestions: Annotated[list[...],Field(max_length=4,...)] = []
```

`items` 是**叙述**（模型改写）；四个 list 是**结构段**（教育/工作/项目/技能），prompt 要求逐字保留
（`materials/agent.py:21`，逐字）：

```text
正文items只写叙述（工作/项目要点）；教育经历、工作经历（单位+角色+起止年月）、项目经历（项目名+期间）、技能
分别放进education、experience、projects、skills字段，逐字保留（可只去冗余标点），这四段不得省略。
```

代码渲染（`contracts.py:227-238`，逐字节）：

```python
if scope.get('draft_style')=='rewrite':
    used={sid for item in result.rewrite_items for sid in item.source_ids}
    blocks=[]
    if result.education:
        blocks.append('# 教育经历\n\n'+'\n'.join('- '+escaped(line) for line in result.education))
    if result.experience:
        blocks.append('# 工作经历\n\n'+'\n'.join('- '+escaped(line) for line in result.experience))
    project_block=['- '+escaped(line) for line in result.projects]
    project_block+=['- '+escaped(item.text) for item in result.rewrite_items]
    blocks.append('# 项目经历\n\n'+'\n'.join(project_block))
    if result.skills:
        blocks.append('# 技能\n\n'+'\n'.join('- '+escaped(line) for line in result.skills))
```

守卫只查**非空**（`contracts.py:182-189`）：源文含「教育经历/工作经历/项目经历/技能」标记而对应字段为空 →
报错。**没有任何"必须逐字等于原文某一行"的校验。**

模型拿到的材料是什么（`agent.py:40` 与 `:104-107`）：`read_source` 直接返回整段 `source['text']`，
**没有行号**。

已有一个"按唯一原文定位"的机制（`contracts.py:165-171`，逐字）：

```python
def grounded(proposal,sources):
    source=next((s for s in sources if s['id']==proposal.source_id),None)
    if source is None or not proposal.quote.strip():raise ValueError('source_not_in_scope')
    text=source['text'];start=text.find(proposal.quote)
    if start<0 or text.find(proposal.quote,start+1)>=0:raise ValueError('unique_exact_quote_required')
    return {**proposal.model_dump(),'start_char':start,'end_char':start+len(proposal.quote),...}
```

它目前只用于 `analyze` 模式的事实提案，**没有用于 rewrite 的结构段**。

## 2. 实测：结构段不是逐字（1 份简历，10 行全部不等）

逐行比对 `payload` 与脱敏源文（`exact=False` 占全部 10 行）：

| 字段 | 源文 | 草稿 | 差异 |
| --- | --- | --- | --- |
| education | `2014.09 - 2019.07  本科   汕头大学 …` | `2014.09 - 2019.07 本科 汕头大学 …` | 多空格压成一个 |
| experience | `2018.10 - 2019.02  某科技有限公司 …` | 同上形态 | 多空格压缩 |
| projects | `2017.08 - 2017.08  即时聊天系统` | `即时聊天系统 2017.08 - 2017.08` | **字段顺序被调换** |
| skills | `Web 开发: Node/Java`、`开放平台:小程序开发` | `Web 开发：Node/Java`、`开放平台：小程序开发` | 半角冒号→全角，且 `开放平台` 行多出冒号后的空格 |

四类都不改变事实，**但"复现"不成立**。

另外一条实测（同一份草稿）：`socket.io / websocket / jquery / seajs` 四个技术名**完全不在输出里**，
原因是两段项目叙述**整块被 8 条 `items` 上限挤掉**（该简历工作+项目要点约 20 条），
不是被改写措辞。这与结构段是**不同**的问题，请不要混为一谈。

## 3. 三条（加上一条变体）候选路线

| 路线 | 做法 | 保真 | 代价 / 风险 |
| --- | --- | --- | --- |
| **S1 行索引切片** | `read_source` 返回带行号文本；模型对每段只返回**行号**；代码按行号**原样切片** | 逐字 | schema 从"内容"变成"索引"；模型可能给不存在的行号（需校验）；跨行条目要额外约定 |
| **S1b 引文锚点切片** | 模型返回每段的**逐字引文**（可多行），代码用已有 `grounded` 的唯一匹配定位后原样切片 | 逐字 | 复用现有机制；引文必须在源文中唯一，否则报错重试（会消耗重试预算） |
| **S2 先分析后渲染** | 走已有 `analyze` → 用户确认 `fact_ids` → 代码按 kind 渲染 | 逐字且经用户确认 | 多一次调用 + 多一步确认，与"先给可用初稿"的产品取舍冲突 |
| **S3 现状 + 逐字校验** | 保留模型抄写，加"每行必须能在源文找到同一行（允许空白差异）"校验，不满足即报错 | 近似 | 校验失败会让模型重试；本项目此前正是在重试/预算边界上失败过（见 §6） |

我的倾向：**S1b 优先、S1 次之**。理由是 S1b 不需要给模型引入"行号"这种新契约，且代码里已经有
"唯一原文定位"的实现与测试；S1 的行号方案更直观，但要新增"行号—源文"契约，且行号是模型最容易算错的东西。

需要说明的是：这四种都不解决 §2 末尾的"8 条装不下 20 条"问题。

## 4. 请你只回答这些封闭问题

1. **四段结构内容是否必须有"代码可验证的逐字来源"？** 即：接受 S1b/S1 这类"模型只定位、代码切片"的设计，
   还是认为"允许空白/标点差异的近似"（S3）已经足够？
2. **如果选 S1b：引文必须唯一吗？** 该简历里同一段文字可能出现多次（例如两条相同的技能行）。
   若要求唯一，模型可能无法表达"这两行都要"；若不要求唯一，代码要按顺序消费匹配。你倾向哪一种？
3. **段落边界的判定权归谁？** 现在由模型决定"哪一行属于教育/工作/项目/技能"（我们承认代码做不到可靠切分，
   因为用户简历格式不统一）。若走 S1b，边界仍然只能由模型给——你认为这算"用模型做分类"（可接受），
   还是仍属于"LLM 决定内容"（需要改成代码规则 + 用户确认）？
4. **叙述条目 `items` 是否也要逐字？** 现在它明确是改写（允许重排语序、合并重复、去口语）。
   如果你的结论是"整份简历都必须逐字"，那本产品的改写价值就只剩排序与建议，请明确说明这是你要的结论。
5. **8 条 `items` 上限现在该不该动？** 实测 1 份简历约 20 条要点装不下，且外审上一轮已裁定"先测覆盖、有证据再谈放宽"。
   现在证据是"整块丢失技术名"。你建议先做 S1b 再做上限，还是先做上限？

## 5. 我可能错在哪

- 我说"模型在结构段的正当理由只有分类"——**可能低估了归一化**：真实简历里日期写法五花八门
  （`2018.10`、`2018/10`、`18年10月`），"统一成一种写法"本身就是生成任务，代码要写得很多。
  如果归一化是合理需求，那么"逐字"与"统一格式"就冲突，需要你裁决哪个优先。
- 那 4 类差异我判定为"不影响事实"。但用户看到 `即时聊天系统 2017.08 - 2017.08` 这种顺序被调换，
  可能直接认为产品不可靠。**这属于产品口径，我只能提请你注意，不该由我单方面定为"可接受"。**
- 我断言 S1b "不增加调用次数"。这只在"引文一次就能唯一匹配"时成立；若频繁不唯一，
  重试会把 6 次 HTTP 预算（`configs/core0615-materials.json` 的 `max_http_attempts: 6`）吃掉。
- 我**没有测过**给模型加"逐字引文"要求后，它现有的改写质量会不会下降（例如为了抄准而不敢合并）。
  这需要真实跑一次才能知道，未做。

## 6. 边界与已知失败（必须一并考虑）

- 上一轮把 `items` 上限从 8 放宽到 24、再到 12，两次都以 `ScopeError` 失败，已回退；
  输出预算 `max_output_tokens: 2048`（`configs/core0615-materials.json`）。
  `ScopeError` 同时覆盖 `invalid_tool_calls` 与 `reasoning_content`（`runtime/deepseek.py`），
  到底哪一类**仍未归因**（原始响应没留存）。
- 因此任何"让模型多输出一点东西"的方案，都要考虑它是否会把调用推到同一个失败面。
- 面板侧已做（本轮）：四段在界面里可见、可逐行改正，保存生成新版本，旧版本保留。
  实测：改正后 payload 与导出 markdown 都是改正后的文本，叙述未被连带改动。
  **所以即使不做 S1b，用户也有兜底手段**——这是"人工兜底"而非"代码保证"，也是我想请你判定的分界。
- 用户编辑侧仍不允许"整段删除"（源文含该段标记时清空会 400），因为复用了模型侧守卫。
