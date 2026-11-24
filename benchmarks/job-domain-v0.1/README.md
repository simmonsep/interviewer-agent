# 六类业务流程 pilot：job-domain-v0.1

本套件把原 career-pilot-v0.2 的30个任务按manifest哈希链接进来，另增24个任务，总计54个，40开发/14预留候选。旧任务、标签、提供方与历史审核不改。所有任务由项目AI辅助编写，仍为pending_human_review。

| 类别 | ID | 总数 / 开发 | 本入口执行什么 |
| --- | --- | --- | --- |
| 目标澄清 | G01～G10 | 10 / 8 | 原协调Agent的业务选择 |
| 记忆与长期任务 | A01～A10、M01～M10 | 20 / 16 | 原申请版本、训练观察和撤回过滤 |
| 岗位研究 | R01～R06 | 6 / 4 | 协调→研究Agent→合成HTTP来源→SQL快照/匹配 |
| 材料事实 | D01～D06 | 6 / 4 | 上传→分析→合成用户确认→草稿/过期提示 |
| 面试训练 | I01～I06 | 6 / 4 | 无简历/用户JD、正式或教练模式、原回答/观察 |
| 笔试与Coding | X01～X06 | 6 / 4 | 真实Docker编译、用户接受/拒绝补丁、冻结终评 |

40是任务ID数量，不能解释为40个统计独立样本。预留文件公开可读，与开发集共用小型题库、仓库及模板，既未人工审核，也不是独立密封留出；本入口拒绝执行预留候选。新任务每个family独立命名只用于格式分组，不能据此证明语义隔离。

## 运行

在项目根目录，使用已初始化的测试PG/Redis和受控Java镜像。命令不读取模型Key，无供应商模型HTTP；scripts/reproduce-core.py可用于先建立独立后端环境，具体边界见JA-09-03记录。

```powershell
& .\.venv\Scripts\python.exe -m interview_agent.evaluation.domain validate
& .\.venv\Scripts\python.exe -m interview_agent.evaluation.domain run `
  --worker-config .private/redis/test-worker.json `
  --owner-config .private/postgres/test-migration.json `
  --executor-profile configs/ja06-docker-java.json
```

--tasks D01 I02可选开发子集；未提供执行器配置就选择X任务时直接拒绝，不使用假判题器。每次任务独立账号和随机测试Redis stream，只向本次任务发布通知；结束后删除本次合成账号/stream，JSON里保留产物快照。实际SQL清理由测试库owner完成，生产配置被拒绝。ASGI TestClient调用认证API，PG/Redis/Worker/Docker真实执行；本入口不是独立进程网络HTTP验收。

评测编号（如X01）、oracle、rubric和expected都留在控制器评分侧；业务题号（如ttl-boundary）是用户选题的合法公共输入。业务执行器execute_domain只接公共输入、服务配置、根目录和受控镜像配置。研究来源明确标offline_fixture，Coding正确补丁由已有scripted-solutions提供，不是模型自主解题成绩。笔试任务的source_code是合成用户提交。

## 评分与审核

原G/A/M沿用原评分；新R/D/I/X比较实际产物的observed字段，并要求对应guards有true证据。SQL提交成功仍可能机器业务检查失败。缺失字段不因为预期null而通过；基础设施失败machine_pass=null，正常完成但动作不符合任务machine_pass=false。任务正确性审核与最终输出语义审核分别记录，domain_success_rate始终null。

```powershell
& .\.venv\Scripts\python.exe -m interview_agent.evaluation.domain review-export
& .\.venv\Scripts\python.exe -m interview_agent.evaluation.domain review-validate --review-file <任务包>/reviews.jsonl
& .\.venv\Scripts\python.exe -m interview_agent.evaluation.domain output-review-export --run-dir <运行目录>
& .\.venv\Scripts\python.exe -m interview_agent.evaluation.domain output-review-validate --run-dir <运行目录> --bundle <输出审核包>
```

任务包检查输入/允许结果是否合理；输出包展示完整合成业务产物、请求状态和逐项rubric。默认随机顺序，mapping本地可读，并非密封盲审。全pending导出，operator_attested只记录操作者填表，不验证真人身份或意见真伪。不要把生成审核包当作已经完成审核。

## 已执行结果

2026-10-30：5例接缝联调通过；随后40开发×system_scripted×1，机器检查37/40，0基础设施失败。G04/G09/G10失败，新增四类各4/4。保留原失败，未修改原提供方/任务/oracle。0供应商模型HTTP/费用，14预留未执行。运行详情和末次补充范围见[JA-09-04记录](../../docs/records/JA-09-04-六类业务评测与校准准备.md)。

这批是业务流程pilot：尚无真人任务/输出审核、全角色真实同模型对照或学习迁移实验，不能发布为LLM能力或多Agent收益榜单。JA-09-02的4任务真实同模型对照继续保持原范围。
