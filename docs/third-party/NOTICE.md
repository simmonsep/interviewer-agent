# 项目许可与第三方依赖说明

2026-10-30：用户明确选择[项目根MIT许可证](../../LICENSE)，署名2026 simmonsep，适用于本项目有权许可的原创部分。第三方代码、依赖、资料及其原始通知保留各自条件，不因根许可证改变。

## Python主线通知材料（JA-09-05）

[Python通知材料包](python-notices/README.md)与[manifest](python-notices/manifest.json)记录当前uv.lock中68个第三方分发包：Windows实际安装67且版本匹配，已收集这67个包的82份原始通知/许可文件并逐个核SHA256。66包来自本机安装分发包，dnspython2.9.0另从对应官方固定提交补充，来源/原始路径/hash见manifest。httpx2-jsfetch为Emscripten条件依赖，当前未安装/未收集，不冒充已审。

这些通知记录了该阶段 Windows Python 依赖的范围。独立安装包还需要按实际打包内容核对解释器、前端依赖、容器与原生库的分发条款。工具 scripts/package-python-notices.py 按原始字节收集通知文件。

## 随前端源码提供的字体

Geist 和 Geist Mono 字体由 Geist Project Authors 提供，适用 SIL Open Font License 1.1。原始通知随字体目录保留，Vite 会将其复制到构建产物。[Geist 许可](../../apps/web/public/fonts/Geist-OFL.txt) · [Geist Mono 许可](../../apps/web/public/fonts/Geist-Mono-OFL.txt)

## 历史Pi依赖说明（PM0-02，保留原范围）

本项目通过 npm 依赖使用 Pi，不复制整个 coding-agent CLI 或上游仓库。core/ai/telemetry 的包元数据标为 MIT；[Pi 原始许可证与署名](Pi-LICENSE.txt) 来自固定提交 `71023aec1546d6d677a65669c5665c0927ce0dd0`，保留原文。

pi-ai 发行包自身包含一个 pi-ai 命令入口及多个 provider SDK；“未安装完整 coding-agent CLI”不等于依赖树中完全没有命令入口，也不等于已支持这些 provider。当前未运行这些命令、未注册线上 provider、未读取 API Key。

[依赖清单](../../manifests/dependencies-PM0-02.json) 保存固定版本、包 integrity、许可标识及本机包元数据哈希；安装包内已有的许可证/NOTICE 保留在 node_modules 中。本文件与清单不是完整法律审查，也不是漏洞扫描或可重新分发全依赖的合规结论。后续打包/公开发行时需根据实际分发内容保留所有必要通知。
