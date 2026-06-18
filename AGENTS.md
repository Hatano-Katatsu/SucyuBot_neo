# AGENTS.md — SucyuBot_neo

## 项目概述

Telegram 机器人，结合 AI 角色扮演（DeepSeek 等 OpenAI 兼容 API）与 ComfyUI（Anima3 模型）生成动漫角色自拍。

## 技术栈

- **语言**: Python 3.11+
- **依赖**: `aiohttp>=3.9` (无其他第三方依赖)
- **数据库**: SQLite（长期记忆）
- **存储**: JSON 文件（配置 `data/config.json`，状态 `data/state.json`）
- **前端**: Vanilla HTML/CSS/JS（aiohttp SPA，Web 控制台）

## 运行命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行服务
python -m telegram_comfyui_selfie --config data/config.json

# 运行测试
python -m unittest tests.test_core -v

# 单独测试某个测试类/方法
python -m unittest tests.test_core.ServiceTestCase.test_parse_command_with_bot_mention -v
```

## 项目结构

```
telegram_comfyui_selfie/
├── __init__.py          # 导出 TelegramComfyUIService
├── __main__.py          # CLI 入口
├── service.py           # 核心服务类（组合所有 mixin）
├── defaults.py          # 默认配置、菜单、场景
├── commands.py          # 所有 /command 处理
├── chat_context.py      # 聊天管道：系统提示构建 + 工具调用
├── generation.py        # ComfyUI 生图：提示词构建 + 工作流 + 生成
├── image_planning.py    # LLM 画面规划器
├── appearance.py        # 外观标签解析/合并/注入
├── memory.py            # SQLite 长期记忆存储
├── memory_policy.py     # 记忆自动提取 + 过滤规则
├── scheduler_runtime.py # 定时推送 + 天气 + NTR 冷落惩罚
├── world_runtime.py     # 世界状态：地点动线 + 天气 + 城市
├── telegram_io.py       # Telegram Bot API 通信
├── process_restart.py   # 进程自重启
├── webui.py             # aiohttp Web 控制台 + REST API
└── static/              # Web 前端 (index.html, app.js, styles.css)
```

## 代码风格

- 使用 `from __future__ import annotations` 延迟求值类型注解
- 类通过 **mixin 多重继承** 组织：`TelegramComfyUIService(ProcessRestartMixin, TelegramIOMixin, CommandHandlersMixin, ChatContextMixin, MemoryPolicyMixin, SchedulerRuntimeMixin, WorldRuntimeMixin)`
- Mixin 方法通过 `self` 访问其他 mixin 的方法，使用 `hasattr` 做防御性检查
- 所有 I/O 方法都是异步 (`async def`)
- 配置通过 `self.config.get(key, default)` 访问，用户会话级覆盖通过 `self._get_session_cfg(session_id, key, default)`
- 日志统一用 `logger = logging.getLogger(__name__)`
- 注释用中文，docstring 用中文，代码标识符用英文

## 测试

- 测试文件：`tests/test_core.py`
- 基于 `unittest.TestCase` + `unittest.mock.AsyncMock`
- 每个测试方法内部用 `asyncio.run(run())` 执行异步代码
- `make_service()` 辅助方法创建临时目录 + 最小配置
- 添加新功能时必须写测试（核心路径已覆盖：命令解析、提示词构建、记忆 CRUD、角色切换、外观合并、Web 序列化）

## 注意事项

- **不要**在 mixin 类中定义 `__init__`；所有初始化在 `TelegramComfyUIService.__init__` 中完成
- **禁止**将 LLM API key 写入 `config.example.json`；通过 `masked_config()` 对 Web 端隐藏
- `session_id` 字符串格式固定为 `"telegram:{chat_id}"`，通过 `session_id_for_chat` / `chat_id_from_session` 转换
- 长期记忆以 `session_id + character` 为隔离维度：换角色即换记忆空间
- fire-and-forget `asyncio.create_task` 内的异常可能被静默吞掉，排查生图/推送失败时优先查看 service.log
- `_get_llm_value("chat", "temperature")` 的 legacy 回退会落到 `llm_temperature_scene` 而非专门的聊天温度——如果 `chat_llm_temperature` 被显式设为空字符串
