# 贡献指南

感谢你愿意改进这个项目。本项目定位是「轻量、平台解耦的 LLM 拟真聊天桥接器」，改动请尽量保持核心层（`src/im_bridge/core/`）不依赖任何具体 IM 平台。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py --check-config     # 只校验配置，不建立连接
Copy-Item .env.example .env      # 填写 AMIS_API_KEY 等
python tests/test_core.py        # 离线测试（22 个用例，不需要网络/真实 LLM）
```

管理台前端开发：

```powershell
python launcher/launcher.py      # 启动管理台（默认 http://127.0.0.1:8765）
python launcher/build_exe.py     # 构建 BotManager.exe（需要 PyInstaller）
```

## 代码约定

- 核心层（`src/im_bridge/core/`）不得 import `adapters/`；
- 平台能力通过 `src/im_bridge/ports.py` 的 `MessageTransport` 协议注入；
- 不硬编码密钥、QQ 号、个人路径；测试用占位符；
- 新增行为优先加配置项并给出默认值，保持旧配置可运行；
- 修改核心逻辑请补充 `tests/test_core.py` 用例。

## 提交与 PR

- 提交信息使用 `type: 描述`（`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`）；
- PR 请说明动机、改动点、验证方式（命令与结果）；
- 涉及 UI 的改动请附 `?demo=1` 演示模式截图（不要提交含真实账号的截图）；
- 不要提交 `.env`、`config.json`、`config.secret.json`、`data/`、`logs/`、`BotManager.exe` 等本地文件。

## 安全

发现安全问题请按 [SECURITY.md](SECURITY.md) 私下联系维护者，不要先公开 Issue。
