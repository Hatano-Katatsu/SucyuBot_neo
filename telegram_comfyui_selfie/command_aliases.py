from __future__ import annotations


# 命令同义词统一维护在这里：每组第一项是规范命令，后面是所有可接受写法。
# 同一概念尽量同时放入「动宾」和「主谓倒装」写法，后续新增别名只改这张表。
COMMAND_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("初始化", (
        "start", "init", "setup",
        "创建角色", "新建角色", "建立角色", "创建新角色",
        "角色创建", "角色新建", "角色建立", "新角色",
    )),
    ("菜单", (
        "help", "menu", "menyu", "帮助", "目录", "指令", "命令",
        "查看菜单", "菜单查看", "打开菜单", "菜单打开",
    )),
    ("创建OC", (
        "oc", "OC", "创建oc", "创建OC", "原创角色",
        "导入角色", "角色导入", "导入OC", "OC导入",
    )),
    ("自拍", (
        "selfie", "photo", "pic", "picture", "image",
        "拍照", "照片", "发图", "生成照片", "照片生成", "生成图片", "图片生成",
    )),
    ("配图", (
        "illustration", "illustrate", "scene-image", "sceneimage",
        "画图", "绘图", "生图", "出图", "生成图",
        "场景图", "剧情图", "画面", "按场景配图", "当前场景配图",
    )),
    ("天气", (
        "weather", "天气查询", "查询天气", "查天气", "天气查看", "查看天气",
    )),
    ("天气设置", (
        "setweather", "weather-set", "设置天气", "天气设置",
        "位置设置", "设置位置", "城市设置", "设置城市",
    )),
    ("测试推送", (
        "testpush", "push-test", "推送测试", "测试主动推送", "主动推送测试", "手动推送",
    )),
    ("测试生图", (
        "testimage", "image-test", "生图测试", "测试图片", "图片测试",
    )),
    ("外型", (
        "appearance", "look", "外貌", "外形", "外观",
        "修改外型", "外型修改", "修改外貌", "外貌修改",
    )),
    ("衣橱", (
        "closet", "wardrobe", "衣柜", "服装", "穿搭",
        "打开衣橱", "衣橱打开",
    )),
    ("外貌自动", (
        "自动外貌", "外型自动", "自动外型", "appearance-auto",
    )),
    ("人格", (
        "persona", "人设", "性格", "人设定义", "定义人设",
        "修改人格", "人格修改", "修改人设", "人设修改",
    )),
    ("画风", (
        "style", "风格", "画风查看", "查看画风", "风格查看", "查看风格",
    )),
    ("添加画风", (
        "addstyle", "添加风格", "画风添加", "风格添加",
    )),
    ("删除画风", (
        "delstyle", "删除风格", "画风删除", "风格删除",
    )),
    ("切换画风", (
        "switchstyle", "切换风格", "画风切换", "风格切换",
    )),
    ("记忆", (
        "memory", "mem", "查看记忆", "记忆查看",
    )),
    ("生活主线", (
        "life", "life-plan", "lifeplan", "生活线", "主线", "目标", "生活目标",
    )),
    ("记住", (
        "remember", "记忆写入", "写入记忆",
    )),
    ("忘记", (
        "forget", "删除记忆", "记忆删除",
    )),
    ("新场景", (
        "resetcontext", "newscene", "scene",
        "换场景", "场景切换", "切换场景", "上下文重置", "清空上下文",
    )),
    ("关系", (
        "relationship", "relation", "空间关系",
        "设置关系", "关系设置", "修改关系", "关系修改",
    )),
    ("回滚", (
        "rollback", "undo", "回退", "撤回", "上下文回滚", "回滚上下文",
    )),
    ("重答", (
        "regenerate", "redo", "重新生成", "重新回答", "重生成", "重答复",
    )),
    ("完整菜单", (
        "fullmenu", "allmenu", "full", "全部菜单", "菜单全部", "完整指令", "全部指令",
    )),
    ("web密码", (
        "webpass", "web-password", "web密码", "设置web密码", "web密码设置",
    )),
    ("webui", (
        "web", "web-ui", "控制台", "管理台",
    )),
    ("模型", (
        "model", "models", "模型设置", "设置模型", "模型管理", "管理模型",
    )),
    ("提示词", (
        "prompt", "查看提示词", "提示词查看",
    )),
    ("生图状态", (
        "status", "状态", "图片状态", "生图状态查看", "查看生图状态",
    )),
    ("调度", (
        "schedule", "sched", "推送调度", "调度查看", "查看调度",
    )),
    ("管理", (
        "admin", "manage", "管理面板", "打开管理", "管理打开",
    )),
    ("纯良度", (
        "purity", "边界", "边界设置", "设置边界", "纯良度设置", "设置纯良度",
    )),
    ("推送频率", (
        "push", "pushfreq", "push-frequency",
        "推送次数", "次数推送", "设置推送", "推送设置", "设置推送频率",
    )),
    ("角色", (
        "character", "char", "角色设置", "设置角色", "切换角色", "角色切换",
    )),
    ("个性设置", (
        "personalize", "个性化", "个性化设置", "设置个性", "个性设置",
    )),
    ("人设查看", (
        "persona-show", "查看人设", "人设查看", "查看个性", "个性查看",
    )),
    ("修改角色", (
        "modify-character", "modify_character", "编辑角色", "角色编辑",
        "修改角色", "角色修改",
    )),
    ("turbo", (
        "加速", "turbo模式", "模式turbo",
    )),
    ("更新", (
        "update", "git-update", "gitupdate", "git更新", "更新git",
    )),
)


BARE_COMMAND_CANONICALS = {
    "初始化",
    "菜单",
    "创建OC",
    "自拍",
    "配图",
    "新场景",
    "调度",
    "测试推送",
    "生图状态",
    "提示词",
}


def build_alias_map(canonicals: set[str] | None = None) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for canonical, aliases in COMMAND_ALIAS_GROUPS:
        if canonicals is not None and canonical not in canonicals:
            continue
        for alias in (canonical, *aliases):
            key = str(alias).strip()
            if not key:
                continue
            alias_map[key] = canonical
            alias_map[key.lower()] = canonical
    return alias_map


COMMAND_ALIAS_MAP = build_alias_map()
BARE_COMMAND_ALIASES = build_alias_map(BARE_COMMAND_CANONICALS)


def resolve_command_alias(command: str) -> str:
    key = (command or "").strip()
    return COMMAND_ALIAS_MAP.get(key) or COMMAND_ALIAS_MAP.get(key.lower()) or key
