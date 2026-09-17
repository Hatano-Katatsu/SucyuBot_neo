# SucyuBot_neo — Telegram ComfyUI 角色自拍服务

使用 Telegram Bot API 原生 HTTP 接口的独立服务：结合 AI 角色扮演（DeepSeek 等 OpenAI 兼容 API）与 ComfyUI（Anima 系列模型）生成动漫角色自拍与日常配图，内置图形控制台。

功能涵盖：ComfyUI 生图、角色聊天模型、生图辅助模型、角色 / 人格 / 外型 / 画风管理、天气与时区、主动推送、世界动线（季节自然光 / 城市地点 / 同处判断）、长期记忆，以及聊天中由 LLM 工具触发的生图。

> ⚠️ 本项目面向成人向（NSFW）二次元角色扮演。请在合规、私有的前提下使用与分享，不得用于任何违法用途。

## 技术栈

- **语言**：Python 3.11+
- **第三方依赖**：仅 `aiohttp>=3.9`
- **外部服务**：ComfyUI（本地，Anima 系列模型）+ OpenAI 兼容 LLM（聊天 / 生图可分开配置）
- **存储**：配置优先使用 `data/config.yml`（兼容 `data/config.json`）；会话、聊天、角色状态与长期记忆统一存入 SQLite

## 快速开始

### 1. 安装并配置 AnimaFlow

本项目不再内置 ComfyUI 插件源码。需要使用 AnimaFlow 生图时，请在 ComfyUI 中手动安装官方 [ComfyUI-AnimaFlow](https://github.com/Langzaigg/ComfyUI-AnimaFlow)：

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/Langzaigg/ComfyUI-AnimaFlow.git
pip install -r ComfyUI-AnimaFlow/requirements.txt
```

安装依赖并重启 ComfyUI 后，确认工作流发现接口可用：

```powershell
Invoke-RestMethod http://127.0.0.1:8188/anima/workflows
```

不同工作流需要的模型文件、目录和可覆盖环境变量以 AnimaFlow 仓库中的 README、实时 schema 与 knowledge 为准；模型可从 [circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima) 获取。SucyuBot 不复制工作流清单或模型规则。

随后在管理员 WebUI 的“设置 → 生图”中打开“启用 AnimaFlow”。每次打开开关都会重新检测 `/anima/workflows`；工作流选项来自该接口，默认优先选择 `anima29_turbo`，不存在时选择其他可用的 turbo 工作流。切换工作流会加载它的 schema、knowledge 以及默认 CFG/步数，之后可在同一面板调整 CFG 与步数。

若 `/anima/workflows` 或所选工作流的 schema/knowledge 检测失败，Bot 会自动回退到改造前的 `turbo_v1` 兼容接口（`/anima/schema_turbo_v1`、`/anima/knowledge_new_models`、`/anima/generate_turbo_v1`），默认 CFG 1、步数 12。该回退只调用 ComfyUI 中已安装插件暴露的 HTTP 接口，不会把插件源码重新内置到 Bot。

### 2. 安装依赖

```bash
pip install -r requirements.txt   # 只有 aiohttp
```

### 3. 准备配置（每人各自一份，不进 git）

```bash
cp config.example.json data/config.json        # Windows PowerShell: Copy-Item config.example.json data\config.json
```

修改 `data/config.json`：

- `telegram_bot_token`：找 Telegram [@BotFather](https://t.me/BotFather) 免费创建一个测试 bot
- `comfyui_url`：本地 ComfyUI 地址（默认 `http://127.0.0.1:8188`）
- `animaflow_enabled`：也可直接在配置文件打开；推荐在管理员 WebUI 开启，以便立即检测接口
- `animaflow_workflow` / `animaflow_cfg` / `animaflow_steps`：由 WebUI 按实时工作流初始化并允许管理员调整
- `chat_llm_api_key`：聊天与角色扮演模型 API key（回复用户、保持人设、决定何时发图）
- `image_llm_api_key`：生图辅助模型 API key（写推送场景、翻译 ComfyUI tags、分析角色 / 外型、判断空间与亲密场景、识别时区）
- 也可只填旧版 `llm_api_key`，两类任务会自动沿用这套通用模型配置
- `unet_model` / `clip_model` / `vae_model`：仅原生 ComfyUI 后端使用；AnimaFlow 模型配置由插件自身管理
- 需要限制使用者时，设置 `allowed_chat_ids`

### 4. 启动

```bash
py -3 -m telegram_comfyui_selfie --config data/config.json --web-port 8787
```

Windows 下也可直接运行 `Start-SucyuBot.cmd`（会自动打开图形控制台）。

### Telegram 菜单和快捷回复

Bot 启动后自动注册私聊命令菜单。在输入框键入 `/` 或点击 Telegram 的菜单按钮，即可选择带中文说明的英文命令，例如 `/help`、`/selfie`、`/character`、`/quickreply`。中文命令（如 `/菜单`、`/自拍`）仍可手动发送；Telegram 原生补全仅接受英文小写字母、数字和下划线。菜单同步失败不会阻止聊天，下次启动会重试。

发送 `/菜单` 或 `/快捷回复`（英文 `/quickreply`）可打开固定回复键盘，包含“早上好呀”“晚安，做个好梦”“我回来啦”“我先忙一会儿”“抱抱你”“陪我聊会儿吧”“今天过得怎么样？”“给我看看你现在的样子吧”。**点击按钮会立即发送对应文字**，与手动输入一样参与聊天和记忆；它不会只把文字填入草稿框。

点击“隐藏键盘”、发送 `/隐藏键盘`、`/hidekeyboard` 或 `/quickreply off` 可关闭；之后用 `/quickreply` 重新打开。普通聊天回复不会强制把已隐藏的键盘弹回来。固定文案集中在 `telegram_comfyui_selfie/telegram_io.py` 的 `QUICK_REPLY_ROWS`。

如果 `telegram_bot_token` 还没填，服务也会先启动图形控制台，可在浏览器里填好 token、模型、ComfyUI 等配置后再点“启动机器人”。服务使用长轮询，不需要公网 webhook。

启动后在 Telegram 给 bot 发送 `/初始化` 查看上手向导。

## 图形控制台（默认 http://127.0.0.1:8787）

- `总览`：机器人、聊天 / 生图模型、ComfyUI、会话与生图状态
- `设置`：Telegram、两类模型、ComfyUI、生图参数、角色默认设定、推送计划
- `会话`：单个 Telegram 会话的人格、角色、外型、城市、画风、纯良度、提示词槽位、世界动线
- `操作`：向指定 Chat ID 发送命令或测试消息

启动参数示例：

```bash
py -3 -m telegram_comfyui_selfie --web-host 127.0.0.1 --web-port 8787
py -3 -m telegram_comfyui_selfie --no-web
```

## 自然生活与照片分享

普通主动推送会在现有生活线和动线内选择自拍、物件细节或无人环境图，并记录成功发送的主题、动作与构图。近似内容最多重规划一次；仍重复则跳过当前随机窗口。用户说“别再提这个”或“今天先不聊”会形成角色独立的话题边界。引用已发照片时，聊天会带上对应照片的场景摘要。

无人生活图同时支持原生 ComfyUI 与 AnimaFlow；如果远端工作流 schema 明确不允许无人图，会重规划为合理的人物照片。图片保持当前画风与角色状态。实际成图质量和多样性仍取决于模型及工作流。

## WebUI 导入酒馆角色和世界背景

在角色页选择“导入”上传 PNG/JSON 角色卡，或粘贴 JSON；在动线页选择“导入酒馆世界背景”上传独立 World Info/Lorebook。上传后使用现有聊天/快速模型自动整理，预览后创建。默认新建且同名不覆盖；选择合并时先展示资料差异，目标资料变更会拒绝旧版本合并。创建不切换当前聊天，也不发送开场消息。

支持旧版常见角色字段、Character Card V2/V3 公共字段、PNG 的 ccv3/chara 元数据及内嵌 character_book。不支持参数预设、资源压缩包或插件脚本；带条件、禁用、未知宏的条目保留来源并标记待整理。示例台词只作风格参考，不导入成真实聊天或用户记忆。

动线页的“世界背景”可编辑概况、地点用途和角色所知范围，并关联或解除关联自己的角色。虚构世界不查询现实天气/POI；缺少摄影技术的世界默认分享场景画面。草稿保留 24 小时，失败可重试，同文件与同模型配置可复用转换结果。

## WebUI 访问缓存

浏览器会保存最近查看的用户列表、角色资料、长期记忆、日记和历史提要，再次访问先显示只读记录并同步最新内容。缓存有效期 24 小时，总量约 2 MB；保存、删除或点击页面顶部刷新按钮会清理旧缓存。密钥、模型配置、日志和实时动线不缓存。浏览器禁用本地存储时仍可正常访问。

管理员登录后会恢复角色页与动线页上次选择的用户；该用户已不存在时自动回退。选择记录按登录账号分别保存，普通用户始终查看自己的会话。

## 主要命令

上手与角色：`/初始化`、`/创建OC`、`/角色`、`/人格`、`/外型`、`/画风`、`/个性设置`、`/人设查看`

日常与生图：`/自拍`、`/新场景`、`/纯良度`、`/提示词`、`/生图状态`、`/测试生图`、`/turbo`

记忆与推送：`/记忆`、`/记住`、`/忘记`、`/推送频率`、`/调度`、`/测试推送`

其它：`/菜单`、`/天气`、`/天气设置`、`/管理`

> Telegram 对中文 slash command 没有官方命令菜单注册支持，但消息文本里直接发送这些命令可以正常解析。

## 测试

```bash
py -3 -m unittest tests.test_core -v
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
