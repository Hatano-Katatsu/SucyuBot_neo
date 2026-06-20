# SucyuBot_neo — Telegram ComfyUI 角色自拍服务

使用 Telegram Bot API 原生 HTTP 接口的独立服务：结合 AI 角色扮演（DeepSeek 等 OpenAI 兼容 API）与 ComfyUI（Anima3 模型）生成动漫角色自拍与日常配图，内置图形控制台。

功能涵盖：ComfyUI 生图、角色聊天模型、生图辅助模型、角色 / 人格 / 外型 / 画风管理、天气与时区、主动推送、世界动线（季节自然光 / 城市地点 / 同处判断）、长期记忆，以及聊天中由 LLM 工具触发的生图。

> ⚠️ 本项目面向成人向（NSFW）二次元角色扮演。请在合规、私有的前提下使用与分享，不得用于任何违法用途。

## 技术栈

- **语言**：Python 3.11+
- **第三方依赖**：仅 `aiohttp>=3.9`
- **外部服务**：ComfyUI（本地，Anima3 模型）+ OpenAI 兼容 LLM（聊天 / 生图可分开配置）
- **存储**：JSON（`data/config.json`、`data/state.json`）+ SQLite（长期记忆）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt   # 只有 aiohttp
```

### 2. 准备配置（每人各自一份，不进 git）

```bash
cp config.example.json data/config.json        # Windows PowerShell: Copy-Item config.example.json data\config.json
```

修改 `data/config.json`：

- `telegram_bot_token`：找 Telegram [@BotFather](https://t.me/BotFather) 免费创建一个测试 bot
- `comfyui_url`：本地 ComfyUI 地址（默认 `http://127.0.0.1:8188`）
- `chat_llm_api_key`：聊天与角色扮演模型 API key（回复用户、保持人设、决定何时发图）
- `image_llm_api_key`：生图辅助模型 API key（写推送场景、翻译 ComfyUI tags、分析角色 / 外型、判断空间与亲密场景、识别时区）
- 也可只填旧版 `llm_api_key`，两类任务会自动沿用这套通用模型配置
- `unet_model` / `clip_model` / `vae_model`：需与你 ComfyUI 中实际存在的模型文件名一致
- 需要限制使用者时，设置 `allowed_chat_ids`

### 3. 启动

```bash
python -m telegram_comfyui_selfie --config data/config.json --state data/state.json --web-port 8787
```

Windows 下也可直接运行 `Start-SucyuBot.cmd`（会自动打开图形控制台）。

如果 `telegram_bot_token` 还没填，服务也会先启动图形控制台，可在浏览器里填好 token、模型、ComfyUI 等配置后再点“启动机器人”。服务使用长轮询，不需要公网 webhook。

启动后在 Telegram 给 bot 发送 `/初始化` 查看上手向导。

## 图形控制台（默认 http://127.0.0.1:8787）

- `总览`：机器人、聊天 / 生图模型、ComfyUI、会话与生图状态
- `设置`：Telegram、两类模型、ComfyUI、生图参数、角色默认设定、推送计划
- `会话`：单个 Telegram 会话的人格、角色、外型、城市、画风、纯良度、提示词槽位、世界动线
- `操作`：向指定 Chat ID 发送命令或测试消息

启动参数示例：

```bash
python -m telegram_comfyui_selfie --web-host 127.0.0.1 --web-port 8787
python -m telegram_comfyui_selfie --no-web
```

## 主要命令

上手与角色：`/初始化`、`/创建OC`、`/角色`、`/人格`、`/外型`、`/画风`、`/个性设置`、`/人设查看`

日常与生图：`/自拍`、`/新场景`、`/纯良度`、`/提示词`、`/生图状态`、`/测试生图`、`/turbo`

记忆与推送：`/记忆`、`/记住`、`/忘记`、`/推送频率`、`/调度`、`/测试推送`

其它：`/菜单`、`/天气`、`/天气设置`、`/管理`

> Telegram 对中文 slash command 没有官方命令菜单注册支持，但消息文本里直接发送这些命令可以正常解析。

## 测试

```bash
python -m unittest tests.test_core -v
```

## 协作开发须知（重要）

- `data/`（含 `telegram_bot_token`、用户聊天记录 `state.json`、日志）已被 `.gitignore` 忽略，**不会进入仓库**。
- 每位开发者各自复制 `config.example.json → data/config.json`，填写**自己的**测试 bot token 与 LLM key。
- **切勿**把自己的 `data/`、`config.json` 或任何密钥提交到 git 或通过其它渠道外发——那等于泄露 token 和真实用户隐私。
- 各人的 `data/state.json` 是各自的本地状态，不共享、不提交。
- 详细开发约定与项目结构见 [AGENTS.md](AGENTS.md)。建议开分支提 Pull Request，不直接推 `main`。

## 安全提示

- 绝不提交 `data/config.json`（密钥）与 `data/state.json`（真实用户隐私）。
- 建议仓库设为 **Private**，仅邀请指定协作者。
