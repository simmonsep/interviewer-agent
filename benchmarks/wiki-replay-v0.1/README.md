# Wiki 单轮回放 v0.1

12 个项目自拟开发案例，覆盖检索排障、会话记忆和工具调用。每个主题都有具体回答、模糊回答、未经核验的个人经历与冲突来源。案例和独立 oracle 的字节哈希由 manifest 固定。

在仓库根目录使用项目 Python 运行，`--out` 使用一个未创建过的目录。

```powershell
$env:PYTHONPATH='backend/src'
python -m interview_agent.evaluation.wiki_replay --suite benchmarks/wiki-replay-v0.1 --out .private/wiki-replay-v0.1
```

| 实验组 | 输入变化 |
| --- | --- |
| A | 角色题包、共用的合成题面和同一问答前缀。 |
| B | 保留 A 的题面、顺序和前缀，给共用后续候选增加合成的资料可用标记。 |
| C | 保留 B。允许采用时，把一个已审核 Wiki 题加入合成题包。待审和冲突条目不送入工具。 |

三组都使用项目的 `interview()`、scripted 面试官、题面校验与事件投影。采用、未读和下一题变化根据实际工具轨迹和返回题面计算。当前输出用于检验协议、定位候选去向和审核题面。真实模型同输入成对评测、人工结果审核和服务端历史快照回放另做。这个目录是开发集；正式留出集需要不同样本。

生成的 `report.json` 含三组题面、工具记录、输入哈希和协议结论。 `review-pairs.md` 按样本固定 X/Y 顺序，页面不标明 B/C。让审阅者先给出选择及理由，再从报告解盲。 `provenance.json` 固定脚本代码、工作树差异和样本哈希。
