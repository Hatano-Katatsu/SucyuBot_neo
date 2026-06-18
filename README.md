# Telegram ComfyUI Selfie Service

使用 Telegram Bot API 原生 HTTP 接口重构的独立服务版 ComfyUI 角色自拍服务。

它保留原 AstrBot 插件的预期功能：ComfyUI 生图、角色聊天模型、生图辅助模型、角色/人格/外型/画风管理、天气和时区、主动推送、NTR 阶段、照片文本记忆、以及聊天中的 LLM 工具触发生图。

## 快速开始

1. 复制配置：

```powershell
Copy-Item config.example.json data\config.json
```

2. 修改 `data/config.json`：

- `telegram_bot_token`: BotFather 给你的 token
- `comfyui_url`: 本地 ComfyUI 地址
- `chat_llm_api_key`: 聊天与角色扮演模型 API key，用于回复用户、保持人设、决定何时调用发图工具
- `image_llm_api_key`: 生图辅助模型 API key，用于写推送场景、翻译 ComfyUI tags、分析角色和外型、识别时区
- 也可以只填旧版 `llm_api_key`，两类任务都会自动沿用这套通用模型配置
- 需要限制使用者时，设置 `allowed_chat_ids`

3. 启动：

```powershell
python -m telegram_comfyui_selfie --config data\config.json --state data\state.json
```

启动后打开本地图形控制台：

```text
http://127.0.0.1:8787
```

如果 `telegram_bot_token` 还没填，服务也会先启动图形控制台。你可以在浏览器里填 token、模型、ComfyUI 等配置，保存后点“启动机器人”。

服务使用长轮询，不需要公网 webhook。

## 图形控制台

控制台包含四个页面：

- `总览`: 查看机器人、聊天模型、生图辅助模型、ComfyUI、会话和生图状态
- `设置`: 编辑 Telegram、两类模型、ComfyUI、生图参数、角色默认设定和推送计划
- `会话`: 修改单个 Telegram 会话的人格、角色、外型、城市、画风、纯良度
- `操作`: 向指定 Chat ID 发送命令或测试消息

启动参数：

```powershell
python -m telegram_comfyui_selfie --web-host 127.0.0.1 --web-port 8787
python -m telegram_comfyui_selfie --no-web
```

## 主要命令

`/菜单`、`/自拍`、`/人格`、`/角色`、`/纯良度`、`/外型`、`/画风`、`/人设查看`、`/人设重置`、`/个性设置`、`/测试生图`、`/turbo`、`/提示词`、`/生图状态`、`/天气`、`/天气设置`、`/推送频率`、`/调度`、`/测试推送`、`/管理`。

Telegram 对中文 slash command 没有官方命令菜单注册支持，但消息文本里直接发送这些命令可以正常解析。
