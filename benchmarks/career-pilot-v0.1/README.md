# Career pilot v0.1

18个项目编写的合成场景：12 development、6 reserved_candidate，全部pending_human_review。覆盖目标行动选择、申请待办、有效训练证据选择。预留分组不是保密或独立审核过的留出集；开源文件可阅读，当前只能验证接口不把评分标签传给运行方。

- tasks.jsonl：公共初始输入、合成环境、分组与来源；运行方只接收其中input。
- oracles.jsonl：允许的业务结果、必要证据与人工rubric；评分控制器读取，提供方不读取。
- manifest.json：版本、范围、数量及文件hash；更改任务/标签/基线须明确升版，不覆盖旧运行证据。
- baseline-spec.json：已运行的两个协议适配器，以及待实现的同模型/同工具真实基线规范。

在项目根目录运行：

```powershell
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation validate
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation run --worker-config .private/redis/test-worker.json --owner-config .private/postgres/test-migration.json
```

run需真实测试PG/Redis、已有迁移，只支持test环境；复用FastAPI ASGI、SQL/Redis/Worker，每次独立合成账号/随机队列，仅清理本次资源。不启动公网服务，不做HTTP网络浏览器联调，不支持live或读模型Key。它直接向本次任务的随机队列发通知，不衡量Outbox全局投递可靠性。

默认两个适配器各跑一次开发任务，共24次尝试。system_scripted是原系统配ScriptedCoordinator；fixed_rules是读相同目标/申请工具、遵守相同SQL Gate/计数上限的确定性流程，不是LLM。二者结果不能归因为模型、多Agent或记忆优劣。--repeats 1/2/3可指定次数，当前首轮只跑1次，不能据此估计随机模型稳定性。预留候选执行还需显式--include-reserved，本步没有运行。

输出在.private/ja-09-01/benchmark/<新UUID>：run.json记录代码/数据版本，attempts.jsonl逐次保留SQL产物/引用/检查与失败，report.json给出分母和逐任务配对。SQL提交成功不等于业务结果正确；machine_pass只覆盖确定性检查，人工审核和domain_success保持pending/null。基础设施失败不被悄悄丢弃；报告同时给有效尝试和全部尝试分母，缺少守卫验证也不能当作已安全。
