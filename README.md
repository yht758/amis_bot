# 爱弥斯 QQ 机器人

基于 NapCatQQ + DeepSeek 的 QQ 私聊机器人，角色设定为《鸣潮》爱弥斯。

> 新手说明：这是我第一次使用 Codex 尝试做的小项目，代码和结构可能还有很多不成熟的地方。如果各位大佬愿意给出建议，帮助我改进，我会非常非常感谢！

## 功能

- 私聊聊天
- DeepSeek 对话
- 简单的用户状态记录
- 管理员权限控制

## Windows 下载

请到本仓库的 [Releases](https://github.com/yht758/amis_bot/releases) 下载最新的 Windows 便携版压缩包。

## 配置

1. 将 `config.example.json` 复制为 `config.json`，按需修改。
2. 将 `config.secret.example.json` 复制为 `config.secret.json`，填入 API Key。
3. 安装依赖到 `libs`：

```powershell
python -m pip install -r 爱弥斯/requirements.txt --target libs
```

## 启动

1. 先启动 NapCatQQ，并确认 WebSocket 端口为 `3001`。
2. 双击 `启动机器人.bat`，或运行：

```powershell
python run.py
```

## 开源协议

本项目使用 [MIT License](LICENSE) 开源。