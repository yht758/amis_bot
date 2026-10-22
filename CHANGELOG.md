# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号使用 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.0] - 2026-10-22

### 新增

- **本地图形化管理台 BotManager**（`launcher/`）：一键启动/停止/重启 bot、可视化配置（API Key 连通性测试、模型、权限、上下文、拟真输出）、实时日志（SSE）、环境自检、NapCat 一键启动，支持深/浅主题与动效。
- **单文件 exe 打包**：`python launcher/build_exe.py` 生成 `BotManager.exe`（PyInstaller，独立运行）。
- **NapCat 自动更新**：管理台环境页检查最新版并一键更新（自动下载、SHA256 校验、备份旧框架与 config、支持镜像源）。
- **配置校验 `--check-config`**：一次聚合输出全部缺失/非法项，密钥脱敏。
- **可插拔人设**：`AMIS_PERSONA` 支持内置 `aemeath` 或自定义 `.txt` / `.md` 提示词文件。
- **离线测试**：`tests/test_core.py`（22 个用例，覆盖上下文、分片、指令、异常兜底）。

### 变更

- 项目定位统一为「轻量 LLM 拟真聊天桥接器」，核心与平台（OneBot/NapCat）解耦，新增 `ports.MessageTransport` 端口抽象。
- OneBot 适配器重连退避上限从 60s 降到 15s，管理台可感知 OneBot 连接状态。
- 管理台品牌通用化（IM Bridge），不绑定单一角色。

### 修复

- 管理台 NapCat 启动命令指向已失效的 `apimain.exe`，改为新版 `napiLoader.bat` 并增加路径校验。
- 流式开启但分片关闭时回复不发送的问题。
- 模型别名（如 `deepseek-chat` 路由到 `deepseek-flash`）被误报为不可用。
- 摘要压缩遇到非 `LLMError` 异常时未兜底的问题。
- 分片器超长句子未按最大长度硬切的问题。

### 安全

- 移除仓库与文档中的真实 QQ 号、API Key、本地绝对路径。
- 密钥统一从环境变量 / `.env` 读取；`.env`、`config.json`、`config.secret.json`、`data/`、`logs/` 等全部加入 `.gitignore`。
- 管理台只监听 `127.0.0.1`，API Key 只写入 `.env`，不落 `config.json`。

## [1.0.0] - 2026-10-22

### 新增

- 平台解耦重构：`爱弥斯/` → `src/im_bridge/`（`core/` + `adapters/` + `personas/`）。
- Summary Buffer 上下文摘要压缩与滑动窗口兜底。
- SSE 流式接收 + 按标点分片 + 1~2 秒拟人发送间隔。
- 多用户上下文隔离、动态黑白名单、指令系统、异常输入容错。
- README 重写、`.env.example`、配置校验、GitHub Issue 草稿、管理台界面截图。

## [0.1.0] - 2026-08-16

### 新增

- 初始公开版本：NapCat/OneBot + DeepSeek 的 QQ 私聊机器人。
- 基础上下文、指令（`/help`、`/status`、`/reset`、`/allow`、`/deny`、`/log`、`/stop`）、日志轮转与重试。
