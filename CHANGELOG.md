# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号使用 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.1.0] - 2026-10-23

围绕 5 个 issue 完成一轮能力升级，全部功能默认安全（语音/主动会话默认关闭）。

### 新增

- **配置字段表 `FIELD_SPECS`（#3）**：把「默认值 / 校验规则 / `--check-config` 展示」收敛到单一事实来源，新增配置项只需加一行 + `.env.example` 一条，`tests/test_config_specs.py` 会守住一致性。
- **配置报错四要素（#3）**：每条错误现在都带「字段名、来源（环境变量 / .env / config.secret.json / config.json / 默认值）、当前值、修复建议」，`--check-config` 退出码非 0，便于脚本与 CI 判断。
- **保护区式分片（#1）**：代码块、行内代码、URL 内部不再被切断；流式中未闭合的代码块整段保护。
- **边界感知延迟（#1）**：句末停顿最久（×1.0）、换行更长（×1.15）、逗号更短（×0.6）、硬切几乎不停（×0.4），更接近真人节奏。
- **段数与长度上限（#1）**：`AMIS_CHUNK_MAX_SEGMENTS` 限制单次回复条数，`AMIS_MESSAGE_MAX_CHARS` 按平台上限自动拆条（关闭分片时同样生效）。
- **摘要后台异步化（#2）**：`AMIS_SUMMARY_ASYNC=true` 时压缩在后台任务执行，不阻塞当前回复；每用户一把锁 + 单飞去重，避免并发重复压缩。
- **token 估算阈值（#2）**：`AMIS_SUMMARY_TOKEN_THRESHOLD` 按估算 token 触发摘要，不再只看消息条数；新增 `estimate_tokens()`。
- **摘要落盘（#2）**：摘要写入 `data/summaries/<uid>.json`，重启后不丢；`/reset` 会一并清除。
- **主动会话（#4）**：新增 `core/proactive.py`，支持闲置时长触发、每日上限、跨零点静默时段、6 小时冷却；默认关闭（`AMIS_PROACTIVE_ENABLED=false`）。
- **语音输入（#5）**：`core/voice.py` 的 `VoiceTranscriber` 调用 OpenAI 兼容 `/v1/audio/transcriptions` 转写语音，失败则忽略该条消息；`AMIS_VOICE_PATH_MAP` 解决 NapCat 容器内路径到宿主机路径的映射。
- **语音回复（#5）**：`SpeechSynthesizer` 调用 `/v1/audio/speech` 合成语音，以 `base64://` 内联发送，避免容器读不到宿主机临时文件；失败自动降级为纯文字。
- **传输层通用 API 调用**：`OneBotTransport.call_api()` + `handle_response()`，支持 `get_record` 等需要请求-响应匹配的接口。

### 变更

- 管理台 `APP_VERSION` 提升到 2.1.0，配置页补齐本次新增的 14 项配置（分片上限、单条字数上限、异步摘要、token 阈值、主动会话、语音），保存后提示重启生效。
- `ChatEngine` 支持可选的 `synthesizer`，语音发送失败不影响文字回复。
- `ContextManager.maybe_compress()` 现在会返回「本轮是否同步完成压缩」，异步模式下返回 `False`。
- 分片器 `ChunkStreamer` 新增 `max_segments` 参数与 `sent_count` 属性；`next_delay()` 新增 `boundary` 参数（均向后兼容）。

### 修复

- 达到段数上限后，流式仍每批 delta 单独成条、导致实际发送条数远超上限的问题。
- 流式未闭合代码块在缓冲区末尾被切开的问题。
- 多处密钥字段在 `--check-config` 中共用同一个脱敏值的问题，现按字段各自脱敏。

### 测试

- 新增 `test_config_specs.py`（7）、`test_chunker.py`（16）、`test_voice.py`（13）、`test_proactive.py`（19）、`test_engine_integration.py`（5）。
- 合计 82 个离线用例全绿，原有 22 项 `test_core.py` 行为保持不变。

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
