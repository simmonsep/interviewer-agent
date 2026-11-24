# Material Coverage Contract（v0.1）

> 目的：把"有没有漏掉 source 已有内容"从 grounding 里独立出来。
> 外审 §18：`0 hallucination ≠ 0 omission`；`Grounding` 与 `Coverage` 必须分开报。
> 实现：`backend/src/interview_agent/evaluation/material_coverage.py`。

## 1. 两层，按可信度分开

| 层 | 判什么 | 权威性 | 怎么算 |
| --- | --- | --- | --- |
| **structural tokens** | 日期、数字、区间、百分比、技术名 | **权威**（缺失即失败） | 源里出现过的 token 必须在输出里逐字出现（去空白/去 markdown 转义后比对） |
| **line coverage** | 每条实质 source 行是否被表达 | **仅诊断**（只进人审队列） | 字符 2-gram Jaccard ≥ 0.35 视为覆盖；低于阈值的列成 dropped |

**不做什么**：不看模型输出里"多出来的东西"（那是 grounding 的职责），因此不可能被误用成
grounding 检查；也不做语义等价判断——`semantic_validity` 恒为 `not_established`。

## 2. 为什么 line coverage 不当通过条件

改写本来就可能合并两条、调换语序、润色措辞。若把 Jaccard 阈值当硬门槛，会把**合法的合并**
判成失败。所以它只回答"这几行看起来没被表达，请人看一眼"。

## 3. 阈值与已知弱点

| 项 | 值 / 说明 |
| --- | --- |
| 最短实质行 | 8 个字符（去空白后）；标题词（教育经历/工作经历/技能…）排除 |
| 相似度阈值 | 0.35（字符 2-gram Jaccard） |
| 数字 token | `NUMBER` 与 `PERCENT`/`RANGE` 会重复计入（如 `20%` 与 `20`），仅作冗余保险 |
| 技术词表 | 固定小表（前端/后端/中间件/框架）；**不在表里的技术不会被检查** |
| 多语言 | 以中文与 ASCII 为主；纯英文/其他语言按同一规则，未单独验证 |
| 同义替换 | `重构→改造` 这类不会被判缺失（数字/技术名仍在），也不会被单独发现 |

## 4. 报告字段

```json
{
  "structural": {"tokens": 45, "missing": ["2018.10"], "passed": false},
  "lines": {"total": 31, "covered": 14, "ratio": 0.452,
            "dropped": [{"line": "...", "best_similarity": 0.111}], "threshold": 0.35},
  "passed": false,
  "semantic_validity": "not_established"
}
```

`passed` 只由 `structural.missing` 是否为空决定。

## 5. 已经抓到的问题（真实简历，2026-11-12）

B1（教育/技能由代码渲染）之后跑这份 1126 字简历：

```text
structural tokens 45，missing 8 —— 全部是工作经历的起止年月
（2018.10 / 2019.02 / 2018.05 / 2018.08 / 2017.08 及三个区间）
line coverage 14/31 = 0.452
```

也就是说：**B1 修好了教育经历与技能，但"单位 + 角色 + 起止年月"这条仍走模型**，
模型在 8 条叙述里塞不下，就把它们丢了。外审在 §6 把
`organization / role / start-end dates` 与 education/skills 并列写进"deterministic fields"，
这一项在 B1 里**没有实现**——coverage 第一次跑就把它暴露出来。

这正是"两个指标必须分开"的理由：grounding 全绿（C1 0、四要素守卫 0）的同时，
coverage 明确指出还有 8 个结构化事实不在文档里。
