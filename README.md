# 轻量 LLM 拟真聊天桥接器（OneBot / NapCat 可选）

一个与 IM 平台解耦的轻量 LLM 拟真聊天核心：目标不是把模型包成问答接口，而是让它像真人一样在聊天软件里说话——口语化短句、去模板化、按打字节奏分段发送。平台相关逻辑被收敛成一个可选适配器，OneBot / NapCat 只是官方提供的部署示例；`src/im_bridge/core/` 不依赖任何 IM 协议，接入新平台只需实现 `MessageTransport` 端口。

默认对接 OpenAI 兼容的 Chat Completions API（默认 `deepseek-chat`，配置 `AMIS_API_KEY` 后即可使用），也可切换为任意兼容端点。

> **当前版本 2.0.0** · 新增图形化管理台与 NapCat 自动更新 · [更新日志](CHANGELOG.md) · [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md)

## 2.0 更新内容

- **本地图形化管理台（BotManager.exe）**：一键启停 Bot、可视化配置（含 API Key 连通性测试）、实时日志、环境自检、NapCat 一键启动与自动更新。
- **部署体验升级**：适配新版 NapCat（`napiLoader.bat` / `napimain.exe`）启动方式；管理台内置 NapCat 更新（下载、SHA256 校验、备份、保留配置、支持镜像）。
- **聊天核心**：Summary Buffer 摘要压缩、SSE 流式接收、按标点分片 + 1~2 秒拟人节奏、配置分层校验、22 个离线测试。
- **开源友好**：密钥与账号全部脱离代码、管理台只监听本机、完整 `.env.example` 与文档。

## 核心特性

- **多用户上下文隔离**：按用户维度维护独立对话历史，多账号同时聊天互不串台；同一用户同一时刻只处理一条消息，处理中到达的新消息会被忽略，避免重复回复与上下文串扰。
- **Summary Buffer 摘要压缩**：历史消息条数超过阈值时先压缩为摘要，再保留最近若干条原文，兼顾长期记忆与 token 开销。
- **拟真 IM 输出约束**：system prompt 限定口语化短句（单句约 10~30 字、每段不超过 50 字），禁止舞台动作、内心独白、外貌描写，不复读、不套模板；被追问身份时不承认自己是 AI，自然岔开话题；发送层按标点切片，并加入 1~2 秒随机间隔模拟打字节奏。
- **流式接收**：支持流式获取模型输出，边收边分片发送，缩短首字等待时间。
- **动态黑白名单**：`/allow`、`/deny` 运行时修改白名单并写回 `config.json`，无需重启；名单为空表示不启用限制。
- **指令系统**：`/help`、`/status`、`/reset`、`/allow`、`/deny`、`/log`、`/stop`，管理类指令仅对 `admin_users` 开放。
- **异常输入容错**：空消息、无意义字符、连续重复输入不会撑爆进程或触发机械复读；LLM 请求失败自动重试，多次失败后给出自然的兜底回应。
- **配置分层与校验**：环境变量 > `.env` > `config.secret.json` > `config.json`，启动时集中校验并列出缺失或非法项。

## 项目结构

```text
.
├── run.py                      # 启动入口：python run.py
├── requirements.txt
├── config.example.json
├── config.secret.example.json
├── .env.example
├── .gitignore
├── README.md
├── CHANGELOG.md                # 版本更新日志
├── CONTRIBUTING.md              # 贡献指南
├── SECURITY.md                  # 安全策略
├── LICENSE
├── scripts/                    # Windows 可选启动脚本
│   ├── start.bat
│   ├── start-with-napcat.bat
│   └── monitor.bat
├── launcher/                   # 本地图形化管理台（可选）
│   ├── launcher.py             # 启动入口（python launcher\launcher.py）
│   ├── web/                    # 原生 HTML/CSS/JS 管理界面
│   ├── build_exe.py            # 构建 BotManager.exe
│   └── README.md               # 管理台使用/构建说明
├── docs/
│   ├── GITHUB_ISSUES.md        # 待实现特性的 Issue 草稿
│   └── screenshots/            # 管理台界面截图（演示模式）
├── tests/
│   └── test_core.py            # 核心层离线测试
└── src/im_bridge/
    ├── config.py               # 配置加载与校验
    ├── main.py                 # 装配入口
    ├── __main__.py             # python -m im_bridge 入口
    ├── log_writer.py
    ├── ports.py                # MessageTransport 平台端口抽象
    ├── core/                   # 平台无关核心
    │   ├── llm.py              # LLMClient：OpenAI 兼容 API、重试、流式
    │   ├── context.py          # ContextManager：多用户隔离、Summary Buffer
    │   ├── chunker.py          # 标点分片 + 拟人化发送间隔
    │   ├── engine.py           # ChatEngine：编排上下文 / LLM / 分片发送
    │   ├── commands.py         # 指令系统
    │   └── state.py            # StateManager：用户互动状态
    ├── personas/               # 可插拔人设
    │   ├── __init__.py
    │   └── aemeath.py          # 默认示例人设（非官方同人设定）
    └── adapters/onebot/        # 唯一官方适配器，可选部署
        ├── transport.py        # OneBot v11 WebSocket 客户端
        └── app.py              # 事件循环、黑白名单过滤、重连
```

运行期生成的数据与日志分别写入 `data/`、`logs/`，二者已在 `.gitignore` 中忽略。

## 快速开始

> 想少折腾？直接使用图形化管理台：构建一次 `BotManager.exe` 后双击即可，见下方「图形化管理台」。

需要 Python 3.10+；以下以 Windows PowerShell 为例，其他 shell 替换为等价命令即可：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env    # 然后编辑 .env，至少填 AMIS_API_KEY
python run.py
```

只检查配置、不建立连接就退出：

```powershell
python run.py --check-config
```

该模式只做配置校验并打印结果：缺失或非法的配置项会给出字段名与修复提示，校验通过后以退出码 `0` 结束。最小可用 `.env` 示例：

```dotenv
AMIS_API_KEY=sk-xxxxxxxxxxxxxxxx
AMIS_BASE_URL=https://api.deepseek.com/v1
AMIS_MODEL=deepseek-chat
```

密钥也可以放在 `config.secret.json` 中，或直接通过环境变量注入；`config.json`、`config.secret.json`、`.env` 都不应提交到版本库。

Windows 用户也可以直接使用 `scripts/` 下的脚本：`start.bat` 启动桥接器，`start-with-napcat.bat` 先等待 NapCat 的 OneBot WebSocket 端口就绪再启动，`monitor.bat` 用于查看运行状态与日志（脚本行为以文件内容为准）。脚本仅是对上述命令的封装，可按自己的环境修改。如果本机已有仓库内的便携版 Python 运行时，直接用 `scripts\start.bat` 调用 `python\python.exe` 运行即可，无需先创建虚拟环境。

## 图形化管理台（可选）

不想手改 `.env` 或敲命令行？仓库附带本地管理台 **BotManager**：一键启动/停止/重启 bot、可视化配置、实时日志、环境自检，界面支持深/浅主题与动态效果。

启动方式（二选一）：

```powershell
# 1) 单文件 exe（推荐）：先构建一次，之后双击即可
python launcher\build_exe.py      # 生成项目根目录下的 BotManager.exe

# 2) 直接从源码启动
python launcher\launcher.py
```

管理台默认监听 `http://127.0.0.1:8765`，会用 Edge 应用窗口打开（未安装 Edge 时回退默认浏览器）。

- **一键启停**：启动 / 停止 / 重启 bot，显示 PID、运行时长；可选自动重启。
- **配置管理**：API Key（密码框 + 一键测试连通性）、模型、Bot QQ、管理员/白名单、上下文与摘要参数、流式/分片开关与延迟、人设。
- **实时日志**：SSE 推送、级别筛选、搜索、下载。
- **环境自检**：Python 运行时 / 依赖 / 配置 / OneBot 端口 / NapCat / Edge 逐项检测并给出修复建议。
- **NapCat 自动更新**：检查最新版并一键更新（自动下载、SHA256 校验、备份旧框架与 config），支持配置下载镜像。
- **数据安全**：只监听 `127.0.0.1`；API Key 只写 `.env`；不上传任何数据。

界面预览（均为 `?demo=1` 演示模式，数据是模拟值）：

| 概览 | 配置 |
| --- | --- |
| ![管理台概览](docs/screenshots/overview.png) | ![配置页](docs/screenshots/settings.png) |
| **实时日志** | **环境自检** |
| ![日志页](docs/screenshots/logs.png) | ![环境自检](docs/screenshots/env.png) |

详细说明见 [launcher/README.md](launcher/README.md)。

## 配置项

所有配置均可通过环境变量设置，也可写入配置文件。`config.json` 使用去掉 `AMIS_` 前缀的小写字段名（如 `AMIS_NAPCAT_WS_URL` 对应 `napcat_ws_url`），密钥建议放在 `.env` 或 `config.secret.json`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AMIS_API_KEY` | 无 | **必填**，LLM 服务密钥 |
| `AMIS_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容 API 地址 |
| `AMIS_MODEL` | `deepseek-chat` | 模型名称 |
| `AMIS_BOT_QQ` | `123456789` | 机器人自身 QQ，用于启动展示与日志（占位即可） |
| `AMIS_RESPOND_TO_GROUPS` | `false` | 是否响应群聊消息（当前适配器仅处理私聊，此项为预留开关） |
| `AMIS_ALLOWED_USERS` | 空 | 逗号分隔白名单；为空表示不限制 |
| `AMIS_ADMIN_USERS` | 空 | 逗号分隔管理员名单，决定管理指令权限 |
| `AMIS_NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | OneBot v11 正向 WebSocket 地址 |
| `AMIS_MAX_HISTORY` | `40` | 上下文窗口保留的消息条数上限（约 20 轮对话） |
| `AMIS_SUMMARY_ENABLED` | `true` | 是否启用摘要压缩 |
| `AMIS_SUMMARY_THRESHOLD` | `24` | 消息条数超过该值后触发摘要压缩（须小于 `AMIS_MAX_HISTORY`） |
| `AMIS_SUMMARY_KEEP_RECENT` | `8` | 压缩后保留的最近消息条数 |
| `AMIS_STREAMING_ENABLED` | `true` | 是否流式接收模型输出 |
| `AMIS_CHUNKING_ENABLED` | `true` | 是否按标点分片发送 |
| `AMIS_CHUNK_MIN_DELAY` / `AMIS_CHUNK_MAX_DELAY` | `1.0` / `2.0` | 分片之间的随机间隔（秒） |
| `AMIS_PERSONA` | `aemeath` | 人设名（内置 `aemeath`），或自定义人设提示词文件路径（`.txt` / `.md`） |

加载优先级从高到低为：环境变量、`.env`、`config.secret.json`、`config.json`。

## 指令系统

指令以 `/` 开头，管理员身份由 `AMIS_ADMIN_USERS` 决定。`/allow`、`/deny` 的改动会写回 `config.json`，重启后仍然有效。

| 指令 | 权限 | 作用 |
| --- | --- | --- |
| `/help` | 全部用户 | 查看可用指令；管理员可见管理项 |
| `/status` | 管理员 | 查看模型、用户数与运行时长 |
| `/reset [QQ]` | 全部用户 | 不带参数时清空自己的对话历史与摘要；管理员可指定 QQ 清空他人 |
| `/allow QQ` | 管理员 | 将用户加入白名单并持久化 |
| `/deny QQ` | 管理员 | 将用户移出白名单并持久化；管理员账号受保护 |
| `/log [status\|questions\|answers] [行数]` | 管理员 | 查看指定日志文件的末尾内容 |
| `/stop` | 管理员 | 优雅停止桥接器 |

## 已验证场景

以下场景已在真实运行环境中验证：

1. **多账号并发隔离**：多个用户同时私聊时，各自的对话历史、摘要与互动状态独立维护，一问一答不会串到其他会话，也不会互相覆盖。
2. **连续发送无意义字符**：连发“1”“1+1=几”之类的短文本，进程不崩溃、不重复回复、不机械复读，仍能给出自然的短句回应。
3. **`/reset` 精准清空**：只清空目标用户的对话历史与摘要，其他用户的上下文不受影响，也不会清空 `data/` 下的用户状态文件。

## OneBot / NapCat 部署示例（可选）

核心与平台解耦，OneBot 适配器只是把私聊消息转成 `MessageTransport` 调用，可按需替换。

### 1. 准备 NapCat 与 QQ

- NapCat 需要与 QQ 版本匹配。QQ 升级后 NapCat 可能失效（典型表现：QQ 弹「文件已损坏」或 3001 端口不监听），此时更新 NapCat 框架即可。
- 从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载与当前 QQ 匹配的版本；推荐 Shell 版或官方安装器。
- 本项目管理台内置「NapCat 自动更新」：环境页可检查最新版并一键更新（自动下载、SHA256 校验、备份旧框架与 config、保留配置）。

### 2. 启动 NapCat

- 新版 Framework 版 NapCat 使用 `napiLoader.bat`（内部调用 `napimain.exe`）启动；管理台「配置 → 管理台设置」可配置启动命令，并在概览页一键启动。
- 在弹出的 QQ 登录窗口登录**机器人账号**。机器人的 QQ 窗口可以最小化，但**不要关闭**——关闭会导致 NapCat 断开、bot 收不到消息。
- 确认 OneBot v11 正向 WebSocket 监听 `127.0.0.1:3001`（NapCat 的 `onebot11_<QQ>.json` 中配置）。若改端口，同步修改 `AMIS_NAPCAT_WS_URL`。

### 3. 启动 Bridge

- `python run.py`，或在管理台点「启动 Bot」。
- 连接断开后适配器会自动重连（最长 15 秒内），无需手动重启。
- 首次部署建议至少配置一个 `AMIS_ADMIN_USERS`，否则管理指令无人可用；需要限制范围时再填写 `AMIS_ALLOWED_USERS`。
- 接入其他平台时，实现 `ports.MessageTransport` 协议并参考 `adapters/onebot/` 的事件循环即可复用全部核心逻辑。

## 已知限制与 Roadmap

| 方向 | 已做 | 待办 / 后续 |
| --- | --- | --- |
| 上下文 | 固定条数滑动窗口截断，已引入 Summary Buffer 摘要压缩 | 按 token 预算动态分配窗口与摘要预算；摘要增量更新与质量评估 |
| 输出节奏 | 已支持流式接收、按标点分片、1~2 秒随机间隔 | 逐字 typing 效果；按内容类型调整节奏；发送过程可打断 |
| 配置校验 | 分层加载、启动时校验、`--check-config` 只校验并报告字段级问题后退出 | 更完整的字段级取值范围提示；启动前完整自检；交互式配置 |
| 平台适配 | 官方 OneBot 适配器、自动重连、黑白名单过滤 | 更多平台适配器与协议测试；多实例 / 多账号运行 |
| 会话持久化 | 对话上下文与摘要保存在进程内存，重启后清空；用户互动状态落盘 `data/` | 会话持久化与加密存储；重启后继续历史对话 |
| 可观测性 | 文本日志（`status` / `questions` / `answers`）与 `/log` 查看；管理台实时日志与环境自检 | 结构化日志、运行指标与告警；日志持久化 |
| 管理台 | 一键启停、可视化配置、实时日志、环境自检、NapCat 自动更新、单文件 exe | 托盘图标与开机自启；多实例管理；远程访问鉴权 |

## 免责声明

- 本项目为非官方同人作品，与《鸣潮》及其权利方无关，不代表其立场，也不包含官方素材。
- 默认人设 `aemeath.py` 仅作为拟真 IM 约束的示例，可通过 `AMIS_PERSONA` 替换为自定义人设；请自行确认角色内容的使用边界。
- 仅供学习与技术交流，不得用于骚扰、诈骗、刷屏或其他违法违规用途；使用者须遵守所在平台的服务条款与当地法律法规，因使用本项目产生的后果由使用者自行承担。

## 贡献与安全

- 贡献方式、代码约定与 PR 要求见 [CONTRIBUTING.md](CONTRIBUTING.md)；
- 漏洞报告与密钥泄露处理见 [SECURITY.md](SECURITY.md)；
- 版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

本项目基于 [MIT License](LICENSE) 开源。
