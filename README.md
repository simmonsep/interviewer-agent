# Interview Workbench · 求职面试工作台

Interview Workbench 是本地运行的求职与技术面试练习应用。你可以从目标岗位出发做一轮模拟面试、复盘回答，并管理用于训练的简历材料和题库。现有界面以中文为主，提供 Web 开发入口和 Windows 桌面端源码。

## 体验一轮面试

1. 注册本地账号，创建求职目标，可以直接粘贴 JD。
2. 选择 AI 应用或 Java 后端方向。练习支持有简历与无简历两种输入。
3. 选择内置题库或导入自己的题库，生成准备方案并确认开场。
4. 回答技术问题、查看复盘。训练材料支持导入、草稿版本与导出。

默认使用本地脚本模式演示完整流程。真实模型由使用者显式开启并提供自己的 API Key 和预算。面经搜索是可选连接器，默认关闭，百度千帆和 Brave 分别使用搜索凭据。面经片段为考法线索，自动评分依据来自题库或已核实的评审规则。搜索词按岗位类别和考点生成。简历、JD 正文和回答有各自的模型调用范围。

## Windows 源码启动

需要 Git、Docker Desktop、PowerShell、Node 24.19 系列和 pnpm 11.25。项目通过脚本安装锁定版本的 Python 和 uv。GitHub 源码快照包含桌面端代码，下面的 Web 入口是最简体验方式。

在仓库根目录运行，Docker Desktop 需已启动。

```powershell
.\scripts\bootstrap-python.ps1
$py = ".\.venv\Scripts\python.exe"
& $py -m interview_agent.storage.provision --root .
& $py -m interview_agent.storage --config .private/postgres/migration.json --root .
& $py -m interview_agent.worker.provision --root .
pnpm --dir apps/web install --frozen-lockfile
```

启动 API 与前端。以下每行分别在仓库根目录的独立终端运行。

```powershell
.\.venv\Scripts\python.exe -m interview_agent.api --config .private/postgres/app.json
pnpm --dir apps/web dev
```

在独立终端启动所需后台 Worker。前两行即可完成核心准备与面试链路，后两行支持模糊目标协调与材料任务。

```powershell
.\.venv\Scripts\python.exe -m interview_agent.interview_prep.worker --config .private/redis/worker.json --provider scripted
.\.venv\Scripts\python.exe -m interview_agent.interviews.worker --config .private/redis/worker.json --provider scripted
.\.venv\Scripts\python.exe -m interview_agent.career.worker --config .private/redis/worker.json --provider scripted
.\.venv\Scripts\python.exe -m interview_agent.materials.worker --config .private/redis/worker.json --provider scripted
```

访问 [工作台](http://127.0.0.1:5174)，API 的 [健康检查](http://127.0.0.1:3100/health) 和 [OpenAPI](http://127.0.0.1:3100/docs) 可单独打开。容器、配置和数据卷供本机开发，默认监听回环地址。

## 实现结构

| 模块 | 实现 |
| --- | --- |
| Web 与桌面 | React、TypeScript、Vite。Windows 桌面使用 Tauri 与 Python sidecar |
| API 与持久化 | FastAPI、PostgreSQL，SQL 迁移提供账号、场次、材料、项目状态与模型用量 |
| 后台执行 | Redis Streams 与 PostgreSQL 任务记录，使用版本、幂等键和租约控制并发 |
| 准备与面试 | LangGraph／LangChain 编排，后端约束工具、状态与用量。题库、岗位文本和可选公共正文帮助出题 |
| 评测 | 模拟传输、场次回放、按考点覆盖、成对真实评测。评测样例使用合成数据 |

源码入口位于 `backend/src/interview_agent`、`apps/web`、`apps/desktop`。测试、SQL 迁移、依赖锁文件和内置样例随源码提供。

## 验证和后续

```powershell
.\.venv\Scripts\python.exe -m pytest -q backend/tests/test_interview_experiences.py -m "not postgres and not redis"
pnpm --dir apps/web typecheck
```

[评测记录](docs/EVALUATION.md)说明真实模型与模拟结果的区别。[后续路线](docs/ROADMAP.md)按实习投递中的真实反馈维护。公开版本的首发交付形式为源码。免配置的桌面安装和更新、生产托管和跨平台安装属于独立验收。部分历史 Windows 桌面打包脚本依赖开发机工具链路径，使用前需按本机环境调整。

本项目原创部分使用 MIT 许可证。第三方依赖和资料适用各自条款。[许可证](LICENSE) · [第三方声明](docs/third-party/NOTICE.md)
