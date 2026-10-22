# BotManager 管理台

本地图形化管理台：一键启动/停止 bot、可视化编辑配置（API Key、模型、权限、拟真输出参数）、实时查看日志、环境自检。基于 Python 标准库实现本地 HTTP 服务，前端为原生 HTML/CSS/JS（无框架、离线可用），用 Edge 应用窗口打开，也可打包为单文件 `BotManager.exe`。

## 快速使用

### 方式一：双击 exe（推荐）

```
BotManager.exe
```

首次使用需先构建（见下文「构建 exe」），或直接使用仓库 Release 中的 `BotManager.exe`。

### 方式二：从源码启动

```powershell
python launcher\launcher.py
```

常用参数：

```powershell
python launcher\launcher.py --no-browser   # 只启动服务，不打开窗口
python launcher\launcher.py --open         # 打开已在运行的实例
python launcher\launcher.py --check        # 打印环境自检 JSON 后退出
python launcher\launcher.py --port 8790    # 指定端口（默认 8765，被占用时自动顺延）
```

管理台启动后默认监听 `http://127.0.0.1:8765`，并用 Edge 的应用窗口（无地址栏）打开。

## 功能

- **一键启停**：启动 / 停止 / 重启 bot，显示 PID、运行时长、退出码；可选自动重启。
- **配置管理**：API Key（密码框、一键测试连通性并显示延迟与模型列表）、Base URL、模型预设、Bot QQ、管理员/白名单（标签输入）、上下文与摘要参数、流式/分片开关、分片延迟、人设。
- **实时日志**：SSE 推送，级别筛选、搜索、自动滚动、清空、下载；bot 退出与异常一目了然。
- **环境自检**：Python 运行时、依赖、配置有效性、OneBot 端口、NapCat、Edge 应用窗口逐项检测并给出修复建议。
- **快捷操作**：打开项目/数据/日志目录，一键启动 NapCat（需先配置启动命令）。
- **退出管理台**：页面右上角「退出管理台」会同时停止 bot，避免进程残留。

配置写入规则（与 bot 核心保持一致）：

- API Key 只写入 `.env`（被 `.gitignore` 忽略），不会写入 `config.json`；
- 其余配置写入 `config.json`，并清理 `.env` 中的同名 `AMIS_*` 覆盖项；
- 读取优先级：进程环境变量 > `.env` > `config.secret.json` > `config.json` > 默认值。

## 界面预览

以下截图取自 `?demo=1` 演示模式（数据为模拟值，不含任何真实账号信息）：

| 概览 | 配置 |
| --- | --- |
| ![管理台概览](../docs/screenshots/overview.png) | ![配置页](../docs/screenshots/settings.png) |
| **实时日志** | **环境自检** |
| ![日志页](../docs/screenshots/logs.png) | ![环境自检](../docs/screenshots/env.png) |

关于页展示版本信息与常用命令：![关于页](../docs/screenshots/about.png)

## 构建 exe

在项目根目录执行：

```powershell
python launcher\build_exe.py
# 或双击 build_manager.bat
```

脚本会：

1. 在 `.build/venv` 中创建独立构建环境并安装 PyInstaller；
2. 打包 `launcher/launcher.py`（内置 `launcher/web/` 静态资源与图标）；
3. 输出 `BotManager.exe` 到项目根目录。

需要能访问 PyPI 安装 PyInstaller。构建产物 `.build/`、`BotManager.exe` 均已被 `.gitignore` 忽略，可随时重建。

## 目录结构

```
launcher/
├── launcher.py        # 入口：单实例探测、启动服务、打开 Edge 窗口
├── server.py          # HTTP 服务 + JSON API + SSE
├── bot_manager.py     # bot 子进程管理、日志缓冲与广播
├── config_store.py    # .env / config.json 读写、校验（复用核心 Config）
├── env_check.py       # 环境自检、NapCat/Edge 集成、打开目录
├── build_exe.py       # 构建脚本
├── assets/            # 图标（由 make_icon.py 生成）
└── web/               # 前端（index.html / styles.css / app.js）
```

界面截图（演示模式）位于仓库根目录 `docs/screenshots/`。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | bot 状态 + 配置摘要（前端每 2 秒轮询） |
| GET/POST | `/api/config` | 读取/保存配置（`api_key` 为空表示不修改） |
| POST | `/api/config/test` | 测试 LLM 连通性（优先 `/models`，失败回退最小 chat 请求） |
| POST | `/api/bot/start` `/stop` `/restart` | 启停 bot |
| POST | `/api/bot/auto-restart` | 自动重启开关 |
| GET | `/api/logs?tail=400` | 日志快照 |
| GET | `/api/logs/stream` | SSE 实时日志 |
| GET | `/api/env?refresh=1` | 环境自检 |
| POST | `/api/settings` | 管理台设置（NapCat 启动命令等） |
| POST | `/api/napcat/start` `/stop` | 启动/停止 NapCat |
| POST | `/api/action/open` | 打开项目/数据/日志/.env |
| POST | `/api/shutdown` | 停止 bot 并退出管理台 |

服务只监听 `127.0.0.1`，并校验请求 `Origin`，不会被外部网页调用。

## 常见问题

- **双击 exe 没反应**：首次启动 PyInstaller 单文件程序需要解压，约 2~4 秒；若仍无反应，查看 `data/launcher.log`。
- **窗口没弹出来**：管理台会优先用 Edge `--app` 打开；若未安装 Edge 则退化为默认浏览器。可手动访问 `http://127.0.0.1:8765`。
- **端口被占用**：默认从 8765 起自动顺延，也会复用已在运行的实例窗口。
- **NapCat 一键启动**：需在「配置 → 管理台设置」填写启动命令。新版 NapCat（bootmain 启动器）通常为：
  `"D:\NapCatQQ\bootmain\NapCatWinBootMain.exe"`
  管理台启动前会校验命令中的可执行文件是否存在，不存在会直接报错；若使用旧版 apimain 方式，请确认路径真实存在。
- **Bot 运行中但收不到消息**：概览页会提示「OneBot 未连接」，说明 NapCat 未启动或端口与 `AMIS_NAPCAT_WS_URL` 不一致。启动 NapCat 并确认 3001 端口监听后，bot 会在 15 秒内自动重连。
- **API Key 测试失败**：确认 Key 未过期/未被回收、Base URL 指向 OpenAI 兼容端点；DeepSeek 可在控制台重新生成。
- **退出后 bot 还在跑**：通过管理台「退出管理台」会先停止 bot；若直接结束进程，请手动结束 `python.exe run.py`。
