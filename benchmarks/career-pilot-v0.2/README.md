# Career pilot v0.2

2026-10-30。保留v0.1的18个场景/标签，在新目录增加12个开发场景，共30个（24开发/6预留候选）。增加暂停、用户意图优先于附带JD、未知进度、关闭后的已投事实、辅助后观察来源和不可信回执备注。全部仍pending_human_review；并未达到40～60经审核、完整六类任务覆盖。

免费验证/试跑：

```powershell
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation validate --suite benchmarks/career-pilot-v0.2
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation run --suite benchmarks/career-pilot-v0.2 --worker-config .private/redis/test-worker.json --owner-config .private/postgres/test-migration.json
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation review-export --suite benchmarks/career-pilot-v0.2
```

review-export导出本地reviews.jsonl及review-guide.md，默认24项开发任务待审、0项通过。请由操作者核对输入/允许结果/rubric后填写意见，再用review-validate --review-file <路径>核对内容绑定。程序只检查operator_attested，不证明填写者身份或已经审核模型语义；任务和oracle改变后旧意见无效。任务审核不等于模型输出审核，domain_success_rate仍保持null。
