# Career pilot v0.2

2026-10-30。保留v0.1的18个场景/标签，在新目录增加12个开发场景，共30个（24开发/6预留候选）。增加暂停、用户意图优先于附带JD、未知进度、关闭后的已投事实、辅助后观察来源和不可信回执备注。全部仍pending_human_review；并未达到40～60经审核、完整六类任务覆盖。

免费验证/试跑：

```powershell
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation validate --suite benchmarks/career-pilot-v0.2
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation run --suite benchmarks/career-pilot-v0.2 --worker-config .private/redis/test-worker.json --owner-config .private/postgres/test-migration.json
& .\.venv\Scripts\python.exe -X utf8 -m interview_agent.evaluation review-export --suite benchmarks/career-pilot-v0.2
```

review-export导出本地reviews.jsonl及review-guide.md，默认24项开发任务待审、0项通过。请由操作者核对输入/允许结果/rubric后填写意见，再用review-validate --review-file <路径>核对内容绑定。程序只检查operator_attested，不证明填写者身份或已经审核模型语义；任务和oracle改变后旧意见无效。任务审核不等于模型输出审核，domain_success_rate仍保持null。

## 同模型预读基线

system_live走原动态工具循环；fixed_prefetch_live固定先执行read_goal，单个申请时再执行read_application，然后进入同一有界循环。两者使用同一DeepSeek模型配置、SYSTEM_PROMPT、工具Schema、SQL提交Gate、最多3次模型HTTP及本阶段共享预算；不是给基线减少资料或故意削弱工具。确定性预读不收费，实际HTTP次数/输入token可能不同，分别记账。多申请不会被固定代码擅自选择。

仅run-live支持真实调用，必须显式提供开发库配置、凭据、任务ID和本阶段授权；普通run仍只允许测试库且没有真实供应商调用。初步试跑规范冻结为G03/G04/A02/M03，两个流程各一次共8尝试、最多24次HTTP，共享本阶段最多2元。该规范本身不代表用户已授权或已经运行。

authorize是单独的显式命令，不发模型请求，但创建新阶段SQL额度；run-live不会自动创建/重置额度。真实试跑必须先有本阶段授权，再复用明确指定的私有Key，示例命令见JA-09实施方案。旧阶段额度不能沿用。价格配置按2026-10-30核对的官方高峰价保守预留，不是供应商账单。

付费合成账号/goal/产物/账本保留，setup面试仍用scripted，不混进被测模型HTTP。UNKNOWN保留全部预留并暂停授权；本轮后续矩阵行标not_run，不自动重试或当模型业务失败。预算/凭据停止也单列。输出含原始attempts、配对报告、累计授权账与具体停止原因；若账本不可读则金额/次数为unknown，不写成0。

所有数据均为项目编写的合成内容，预留候选并不保密/独立，默认不执行。真实同模型少量试跑也只是探索性证据，不能替代人工语义审核、统计稳定性、用户收益或开源复现验收。

## 本项目真实探索小批（2026-10-30）

用户另行明确授权后，按规范8尝试完成，14HTTP全KNOWN，两流程各4/4机器检查。预读4HTTP/0.01516192元上界，动态10HTTP/0.01359592元上界，合计0.02875784元。预读输入token少但缓存未命中更多；先后轮转并未独立控制供应商缓存，不能只数轮次称省钱/总体优胜。语义/任务审核仍pending、领域成功率null。本阶段不追加运行，费用/账号/产物保持；未来运行自备新授权。来源：JA-09方案7.6/JA-09-02回执，私有run cf2da14bf70848fb8378110709edc4cd。
