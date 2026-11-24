# 评审请求：结构段已经逐字了，叙述该不该分批（8 条 vs 23 段）

> 需要你裁决的是**叙述覆盖的机制**，不是文案。请只依据本文；你看不到仓库，凡涉及代码我都贴了原文与位置。
> 上一轮的 S1′ 已按你的裁决落地并通过真实复验，本文不再讨论结构段；冻结资产 `judgement-v0.5.0` 与本议题无关。
> 证据强度：**1 份真实脱敏简历、1 次成功草稿**。这是缺陷定位，不是统计。

## 1. 先确认上一轮的结果（避免重复讨论）

按你选的 S1′ 实现后，同一份简历、同一 JD、同样的 3 次模型请求：

| 指标 | 模型抄写（改前） | **段落 ID（改后）** |
| --- | --- | --- |
| 四段结构行与源文**逐字节**一致 | 0/10 | **10/10** |
| 结构 token 缺失 | 4 | 2 |
| C1 / 四要素守卫 | 0 / 0 | 0 / 0 |
| 费用 | 0.017418 元 | 0.015010 元 |

结构部分到此为止，请**不要**再建议回到"模型抄写 + 校验"。

另外报告一个我们必须承认的缺陷（与本议题相关）：
第一次尝试以 `invalid_provider_tool_calls` 失败——provider 层的工具调用参数**不是合法 JSON**
（`tool_call_count: 0`、`output_tokens: 1071 < 2048`，因此不是输出预算截断）。
同代码第二次成功。**间歇性失败，原因未归因**；我们没有用自动重试掩盖它。

## 2. 现在的问题：叙述仍是一刀 8 条

用同一份草稿做段落级核对（代码切段 = 你上轮建议的 stable segment；这里用来当**测量尺**，不是新增机制）：

```text
源文非空行 37 → 标题 3 → 正文 34
  结构化占用 10 行 → 叙述候选 **23 段**
本次 items 产出 **8 条**
按 0.35 相似度（诊断阈值）判定：**覆盖 11 段，未覆盖 12 段**
```

未覆盖的 12 段（逐字）：

```text
0.35 | 通过  vue-i18n 实现官网国际化,并修改相应样式(语言兼容、响应式)
0.06 | 出文件等功能                                   ← 上一行的换行续行
0.07 | 能提升一倍以上                                 ← 上一行的换行续行
0.31 | 设计 PV 、 UV 、使用时长等数据的统计规则,设计数据表及 sql 语句,编写 API 文档与设计文档
0.25 | 对前端数据进行统计采集,并基于 echarts 完成数据可视化展示
0.08 | 对打包后的 js 文件进行优化,主要是代码分块、提取公共模块、按需加载
0.20 | 设计数据表以及规则,开发完成前端监控「我的关注」功能
0.16 | Node 反向代理对接其他平台的相关接口
0.18 | 对之前的数据列表添加分页功能、跨页检索等功能
0.06 | 基于  Express 搭建网站后台,作为静态服务器
0.04 | 通过  socket.io 模块实现  WebSocket 通信
0.11 | 通过  FileReader 实现聊天图片读取以及展示
```

两个必须说清的点：

1. **`socket.io` / `websocket` 两个结构 token 缺失，就是这里造成的**（整段叙述没进 items）。
2. 上传文本按固定宽度换行，所以"`出文件等功能`"这种**续行**会单独成段。代码无法区分"续行"和"真正的短句"。

## 3. 代码事实（逐字）

### 3.1 单次调用的条目上限 = 8，且是整份文档

```python
# backend/src/interview_agent/materials/contracts.py:123
items: Annotated[list[RewriteItem],Field(min_length=1,max_length=8)]
```

```python
# backend/src/interview_agent/materials/contracts.py:110-113
class RewriteItem(Contract):
    text: Annotated[str,Field(min_length=1,max_length=500,...)]
    source_ids: Annotated[list[UuidString],Field(min_length=1,max_length=2)]
    change_note: Annotated[str,Field(min_length=1,max_length=200)]
```

### 3.2 所有叙述条目都渲染在同一个 `# 项目经历` 下

```python
# backend/src/interview_agent/materials/contracts.py:244-246
project_block=['- '+escaped(line) for line in result.projects]
project_block+=['- '+escaped(item.text) for item in result.rewrite_items]
blocks.append('# 项目经历\n\n'+'\n'.join(project_block))
```

即：**分批不会自动带来分段排版**，分批结果仍然全部堆在"项目经历"里，除非同时改渲染契约。

### 3.3 每次运行最多 6 次模型请求；授权上限也是 6 次

```python
# backend/src/interview_agent/materials/agent.py:70
if count>=6:raise ValueError('material_model_limit')
```

```json
// configs/core0615-materials.json
{ "max_output_tokens": 2048, "max_http_attempts": 6, "max_operation_attempts": 6 }
```

现状：一次成功改写用 **3 次**（scope / target+source 合并读 / submit）。分批成 3 批 = 5 次，仍在 6 以内；
若中间任何一次工具报错需要重试，就会撞 `material_model_limit`。

### 3.4 结构段已经能按段落 ID 取原文（同一个机制可以复用到叙述）

```python
# backend/src/interview_agent/materials/segments.py（本轮新增）
def scope_segments(scope):   # S001, S002, ... 跨材料连续编号
def resolve_sections(segments, fields):  # {section: [id]} -> 原文行
```

`RewriteItem` 目前只有 `source_ids`（**整份材料**的 ID），没有段落粒度。

## 4. 四个候选

| 方案 | 做法 | 新调用 | 我的判断 |
| --- | --- | --- | --- |
| **A 保持 8** | 用户自己在面板补漏 | 0 | 现状；实测漏 12 段，用户不会知道漏了哪几段 |
| **B 条目带段落证据** | `RewriteItem` 加 `evidence_segments`，代码产出"哪些叙述段没有被任何条目引用"的覆盖报告 | **0** | **建议先做**：把"漏"变成可判定、可显示，不改变调用次数 |
| **C 按 section 分批** | 每个 section 一次调用（每批 ≤8 条） | +2～+3（5–6 次） | 需要 B 的判据才知道每批够不够；且要处理渲染归位 |
| **D 改成 per-call 上限** | 8 只约束单次调用，文档总量不限 | +2～+3 | 与 C 等价但更含糊：它没说"如何切"，交给实现随意决定 |

我倾向 **先 B，再据 B 的数据决定 C 的粒度**。理由很直接：**没有覆盖判据的分批，只是把遗漏挪了位置**——
今天漏 12 段，明天分 3 批可能仍漏 5 段，而且用户更看不出来。

## 5. 请你只回答这些封闭问题

1. **B 是否成立？** `RewriteItem` 增加 `evidence_segments`（段落 ID），代码据此产出
   "未被引用的叙述段"清单。你是否认可它只用于**可追溯/覆盖统计**，而**不是**语义忠实的证明
   （你上轮 §16 明确说过：段落证据不是新的 semantic oracle）？
2. **覆盖判据取哪种？**
   (a) 每个叙述段必须被至少一条 `items` 引用，否则报错；
   (b) 允许不写，但必须显式说明（`change_note`/单独字段），否则报错；
   (c) 只产出报告，不报错，由用户决定。
   请选一个。
3. **分批粒度**：按 section（教育/工作/项目）切，还是按"每批 N 段"的窗口切？
   跨项目块的叙述（本项目里 4 个项目、两个块被挤掉）怎么切才不重复？
4. **换行续行**怎么处理？允许一条 item 引用多个相邻段（`["S020","S021"]`），
   还是要求代码先把续行合并成一段（但代码无法可靠判断续行）？
5. **预算边界**：分批后一次改写从 3 次请求变成 5–6 次，`max_http_attempts: 6` 会变紧。
   你是否认为应该同时放宽这个**计费保险丝**？如果放宽，建议的上限是多少（我们不自行扩额度）？

## 6. 我可能错在哪

- 我用 **0.35 相似度**判定"覆盖"，那是诊断阈值，不是语义判据。列表里 0.35 那条被算成"未覆盖"，
  很可能其实是覆盖的。所以"12 段未覆盖"应理解为**上限估计**，不是精确损失。
- 8 条 item 里有的合并了多行原文，所以"12 段未覆盖"**不等于**"丢了 12 条信息"。
- 我**没有验证**模型能否稳定地给每条 item 标注正确的段落（`evidence_segments` 的可行性未测），
  也没测过它会不会因此把 `text` 写短、或把输出推到 2048 之外。
- 换行续行是**用户上传文本**的特性（原简历按固定宽度硬换行），不是模型造成的；
  但这也意味着"一段 = 一行"的切法在叙述侧本来就不自然。

## 7. 边界

- 1 份简历、1 次成功草稿；不构成失败率或覆盖率结论。
- 本轮**未改** `items` 上限、未加 `evidence_segments`、未做分批、未做归一化。
- 面板已能逐行显示与改正四个结构段（0 元），叙述条目也能改；本文讨论的是**自动产出是否完整**，
  不是"用户能不能补"。
