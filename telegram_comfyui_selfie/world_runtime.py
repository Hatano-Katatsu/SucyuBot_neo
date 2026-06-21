from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# 时间段顺序与各时间段的“代表时刻”——用代表时刻（而非边界）推断接下来会去的地点，更贴近该时段的典型活动。
PERIOD_ORDER = ["早晨", "下午", "傍晚", "深夜"]
PERIOD_REP_HOUR = {"早晨": 8, "下午": 14, "傍晚": 19, "深夜": 23}

# 职业（白天职场锚点）→ 白天工作时段去向分布。例行评分里的“工作权重”（公司+学校）会按这个比例
# 重新分配；分布里若包含身份锚定场所（公司/学校/工厂/田间/工地），该职业就“解锁”这些场所。
# 单一职场（上班族/学生/工人/务农/建筑）落在一个锚点；流动型（外卖/快递/司机/店员）分散到沿途的公共场所。
OCCUPATION_ANCHORS: dict[str, dict[str, Any]] = {
    "company":      {"label": "上班族", "desc": "上班族（白天在公司，不会出现在学校/工地）", "places": {"company": 1.0}},
    "school":       {"label": "在校", "desc": "在校（白天在学校，不会出现在公司）", "places": {"school": 1.0}},
    "factory":      {"label": "工厂工人", "desc": "工厂工人（白天在车间/厂区）", "places": {"factory": 1.0}},
    "farm":         {"label": "务农", "desc": "务农（白天在田间地头）", "places": {"farm": 1.0}},
    "construction": {"label": "建筑工人", "desc": "建筑工人（白天在工地）", "places": {"construction": 1.0}},
    "medical":      {"label": "医护", "desc": "医护人员（白天在医院）", "places": {"hospital": 1.0}},
    "retail":       {"label": "店员/服务员", "desc": "店员或服务员（白天在商场/便利店/餐厅当班）", "places": {"mall": 0.45, "convenience": 0.3, "restaurant": 0.25}},
    "delivery":     {"label": "外卖/快递员", "desc": "外卖或快递员（白天在路上奔波送单）", "places": {"street": 0.4, "transit": 0.3, "restaurant": 0.2, "convenience": 0.1}},
    "driver":       {"label": "司机", "desc": "司机（白天在路上或车站）", "places": {"street": 0.55, "transit": 0.45}},
    "home":         {"label": "无固定职场", "desc": "无固定职场（主妇/自由职业/非人类设定，白天多在家或周边）", "places": {"home": 0.6, "cafe": 0.25, "mall": 0.15}},
    "flexible":     {"label": "时间自由", "desc": "时间自由，无固定职场", "places": {"home": 0.5, "cafe": 0.3, "mall": 0.2}},
}

WORK_SIGNAL_PLACES = ("company", "school")  # 例行评分里代表“白天工作强度”的信号场所
# 仅“身份锚定”的职场：只有当它正好是角色职业的去向时才会出现，普通人白天不会无故出现在这些地方。
ANCHOR_ONLY_PLACES = {"company", "school", "factory", "farm", "construction"}
VALID_AGE_STAGES = {"minor", "adult"}
VALID_DAY_ANCHORS = set(OCCUPATION_ANCHORS)

# 把人设里可能出现的中英文说法归一到固定枚举。
AGE_STAGE_ALIASES = {
    "minor": "minor", "未成年": "minor", "未成年人": "minor", "青少年": "minor",
    "中学生": "minor", "高中生": "minor", "初中生": "minor", "小学生": "minor", "teen": "minor", "child": "minor",
    "adult": "adult", "成年": "adult", "成年人": "adult", "成人": "adult", "大人": "adult",
}
DAY_ANCHOR_ALIASES = {
    # 上班族
    "company": "company", "公司": "company", "上班族": "company", "上班": "company",
    "职场": "company", "白领": "company", "程序员": "company", "office": "company", "work": "company", "ol": "company",
    # 在校（学生/教职工）
    "school": "school", "学校": "school", "学生": "school", "在校": "school", "大学生": "school",
    "中学生": "school", "高中生": "school", "教师": "school", "老师": "school", "教职工": "school", "student": "school", "teacher": "school",
    # 工厂工人
    "factory": "factory", "工厂": "factory", "工人": "factory", "车间": "factory", "流水线": "factory", "厂工": "factory", "worker": "factory",
    # 务农
    "farm": "farm", "农民": "farm", "务农": "farm", "农夫": "farm", "种地": "farm", "农户": "farm", "farmer": "farm",
    # 建筑工人
    "construction": "construction", "工地": "construction", "建筑工": "construction", "建筑工人": "construction", "施工": "construction", "泥瓦匠": "construction",
    # 医护
    "medical": "medical", "医生": "medical", "护士": "medical", "医护": "medical", "大夫": "medical", "doctor": "medical", "nurse": "medical",
    # 店员/服务业
    "retail": "retail", "店员": "retail", "营业员": "retail", "收银员": "retail", "服务员": "retail", "导购": "retail", "售货员": "retail", "clerk": "retail", "waiter": "retail", "waitress": "retail",
    # 外卖/快递
    "delivery": "delivery", "外卖": "delivery", "外卖员": "delivery", "快递": "delivery", "快递员": "delivery", "送餐": "delivery", "骑手": "delivery", "courier": "delivery",
    # 司机
    "driver": "driver", "司机": "driver", "网约车": "driver", "出租车": "driver", "货车": "driver", "货运": "driver", "代驾": "driver",
    # 无固定职场
    "home": "home", "家": "home", "居家": "home", "主妇": "home", "家庭主妇": "home", "家里蹲": "home",
    "自由职业": "home", "无业": "home", "freelance": "home", "homemaker": "home", "neet": "home",
    # 时间自由
    "flexible": "flexible", "无固定": "flexible", "不固定": "flexible", "弹性": "flexible",
}


PLACE_TYPES: dict[str, dict[str, Any]] = {
    "home": {
        "label": "家中",
        "examples": ["客厅", "卧室", "厨房", "玄关", "阳台"],
        "indoor": True,
        "public": False,
        "views": ["pov", "mirror", "selfie"],
        "activities": ["起床", "做饭", "等你回家", "休息", "睡前聊天"],
    },
    "company": {
        "label": "公司",
        "examples": ["办公室", "工位", "茶水间", "写字楼大堂", "公司楼下"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["上班", "午休", "加班", "下班前发消息"],
    },
    "school": {
        "label": "学校",
        "examples": ["教室", "图书馆", "社团活动室", "校门口", "操场边"],
        "indoor": False,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["上课", "自习", "课间", "放学"],
    },
    "park": {
        "label": "公园",
        "examples": ["林荫道", "湖边长椅", "花坛旁", "草坪边", "公园入口"],
        "indoor": False,
        "public": True,
        "views": ["third", "selfie"],
        "activities": ["散步", "吹风", "等人", "周末约会"],
    },
    "mall": {
        "label": "商场",
        "examples": ["商场中庭", "服装店", "试衣间外", "电影院楼层", "甜品店门口"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "mirror", "third"],
        "activities": ["逛街", "避雨", "买东西", "试衣服", "吃饭前等待"],
    },
    "street": {
        "label": "大街",
        "examples": ["路口", "商业街", "街边橱窗", "人行道", "夜晚街灯下"],
        "indoor": False,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["通勤", "散步", "买东西路上", "等红灯"],
    },
    "cafe": {
        "label": "咖啡店",
        "examples": ["靠窗座位", "吧台旁", "角落小桌", "咖啡店门口"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["休息", "等人", "看书", "躲雨"],
    },
    "restaurant": {
        "label": "餐厅",
        "examples": ["餐桌边", "餐厅门口", "包间", "吧台座位"],
        "indoor": True,
        "public": True,
        "views": ["pov", "selfie", "third"],
        "activities": ["午餐", "晚餐", "约会", "点餐"],
    },
    "transit": {
        "label": "车站/地铁",
        "examples": ["地铁站台", "车厢里", "公交站", "车站出口", "出租车后座"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["通勤", "下班路上", "赶车", "等车"],
    },
    "convenience": {
        "label": "便利店",
        "examples": ["便利店货架前", "收银台旁", "店门口", "热饮柜边"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["买夜宵", "买饮料", "临时停留"],
    },
    "cinema": {
        "label": "电影院",
        "examples": ["影厅门口", "取票机旁", "走廊海报前", "座位边"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["看电影", "等开场", "约会"],
    },
    "hotel": {
        "label": "酒店",
        "examples": ["酒店房间", "走廊", "窗边", "浴室镜前"],
        "indoor": True,
        "public": False,
        "views": ["pov", "mirror", "selfie"],
        "activities": ["旅行", "休息", "夜晚停留"],
    },
    "hospital": {
        "label": "医院",
        "examples": ["候诊区", "走廊", "药房旁", "医院门口"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["看诊", "陪同", "等待"],
    },
    "gym": {
        "label": "健身房",
        "examples": ["跑步机旁", "更衣室外", "休息区", "瑜伽教室"],
        "indoor": True,
        "public": True,
        "views": ["mirror", "selfie", "third"],
        "activities": ["运动", "拉伸", "休息"],
    },
    "factory": {
        "label": "工厂",
        "examples": ["车间", "生产线旁", "厂区", "工厂门口", "员工通道"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["上工", "倒班", "午休", "下工"],
    },
    "farm": {
        "label": "田间",
        "examples": ["田埂", "菜地", "果园", "大棚里", "村口"],
        "indoor": False,
        "public": True,
        "views": ["third", "selfie"],
        "activities": ["农活", "采摘", "歇晌", "收工"],
    },
    "construction": {
        "label": "工地",
        "examples": ["工地", "脚手架旁", "工棚", "工地门口", "塔吊下"],
        "indoor": False,
        "public": True,
        "views": ["third", "selfie"],
        "activities": ["施工", "搬运", "午歇", "收工"],
    },
}

PLACE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("home", re.compile(r"(到家|回家|在家|家里|客厅|卧室|厨房|玄关|阳台|my home|at home)", re.I)),
    ("company", re.compile(r"(公司|办公室|上班|工位|写字楼|公司楼下|office|work)", re.I)),
    ("school", re.compile(r"(学校|大学|教室|图书馆|上课|有课|放学|校园|school|college|university|classroom|library)", re.I)),
    ("park", re.compile(r"(公园|湖边|草坪|散步|park)", re.I)),
    ("mall", re.compile(r"(商场|购物中心|商城|试衣|逛街|mall|shopping)", re.I)),
    ("street", re.compile(r"(大街|街上|路上|路口|商业街|street|road)", re.I)),
    ("cafe", re.compile(r"(咖啡店|咖啡馆|咖啡厅|cafe|coffee)", re.I)),
    ("restaurant", re.compile(r"(餐厅|饭店|吃饭|晚饭|午饭|restaurant|dinner|lunch)", re.I)),
    ("transit", re.compile(r"(地铁|车站|公交|出租车|高铁|火车站|机场|station|subway|train|airport)", re.I)),
    ("convenience", re.compile(r"(便利店|小卖部|超市|convenience|store)", re.I)),
    ("cinema", re.compile(r"(电影院|电影票|影厅|cinema|movie)", re.I)),
    ("hotel", re.compile(r"(酒店|旅馆|民宿|hotel)", re.I)),
    ("hospital", re.compile(r"(医院|诊所|候诊|hospital|clinic)", re.I)),
    ("gym", re.compile(r"(健身房|健身|运动馆|gym)", re.I)),
    ("factory", re.compile(r"(工厂|车间|厂里|流水线|生产线|factory|plant)", re.I)),
    ("farm", re.compile(r"(田里|田间|地里|农田|菜地|果园|大棚|种地|farm|field)", re.I)),
    ("construction", re.compile(r"(工地|脚手架|施工现场|construction site)", re.I)),
]

CITY_CATALOG_KEYS = set(PLACE_TYPES)
BAD_WEATHER_RE = re.compile(r"(雨|雪|雷|雾|霾|大风|暴|storm|rain|snow|fog|thunder|shower)", re.I)
HOT_COLD_RE = re.compile(r"(炎热|高温|酷暑|寒冷|低温|冰|hot|cold|freezing)", re.I)
CLEAR_WEATHER_RE = re.compile(r"(晴|clear|sunny)", re.I)


class WorldRuntimeMixin:
    def _world_runtime_enabled(self) -> bool:
        return self._bool_config("world_runtime_enabled", True)

    def _world_city_places_enabled(self) -> bool:
        return self._bool_config("world_city_places_enabled", True)

    def _bool_config(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "启用", "开启")
        return bool(value)

    def _city_catalog_key(self, city: str) -> str:
        return re.sub(r"\s+", "", (city or "").strip().lower())

    def _date_set_config(self, key: str) -> set[str]:
        raw = self.config.get(key, "")
        if isinstance(raw, list):
            parts = raw
        else:
            parts = re.split(r"[\s,;，；]+", str(raw or ""))
        return {str(part).strip() for part in parts if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(part).strip())}

    def _day_type(self, now: datetime) -> dict[str, Any]:
        today = now.strftime("%Y-%m-%d")
        holidays = self._date_set_config("world_holiday_dates")
        workdays = self._date_set_config("world_workday_dates")
        if today in workdays:
            return {"label": "调休工作日", "is_day_off": False}
        if today in holidays:
            return {"label": "节假日", "is_day_off": True}
        if now.weekday() >= 5:
            return {"label": "周末", "is_day_off": True}
        return {"label": "工作日", "is_day_off": False}

    def _weather_text(self, weather: Any) -> str:
        if isinstance(weather, dict):
            desc = str(weather.get("desc") or "未知").strip()
            temp = str(weather.get("temp") or "").strip()
            return f"{desc} {temp} C".strip() if temp else desc
        if weather:
            return str(weather)
        return "未知"

    def _is_bad_world_weather(self, weather: Any) -> bool:
        if isinstance(weather, dict):
            try:
                if self._is_bad_weather(weather):
                    return True
            except Exception:
                pass
        return bool(BAD_WEATHER_RE.search(self._weather_text(weather)))

    def _routine_scores(self, now: datetime, is_day_off: bool, mode: str = "chat") -> dict[str, float]:
        hour = now.hour
        scores = {key: 0.0 for key in PLACE_TYPES}
        if is_day_off:
            if hour < 6:
                scores.update(home=6, hotel=2)
            elif hour < 10:
                scores.update(home=5, cafe=2, park=1)
            elif hour < 12:
                scores.update(cafe=3, mall=3, park=3, street=1, home=1)
            elif hour < 14:
                scores.update(restaurant=4, mall=2, cafe=2, park=1)
            elif hour < 18:
                scores.update(mall=4, park=3, street=2, cafe=2, cinema=2)
            elif hour < 21:
                scores.update(restaurant=4, mall=3, cinema=3, street=1, home=1)
            else:
                scores.update(home=5, convenience=2, hotel=1, cafe=1)
        else:
            if hour < 6:
                scores.update(home=6)
            elif hour < 9:
                scores.update(home=3, transit=3, cafe=1, street=1)
            elif hour < 12:
                scores.update(company=5, school=2, cafe=1)
            elif hour < 14:
                scores.update(restaurant=3, company=2, cafe=2, street=1)
            elif hour < 18:
                scores.update(company=5, school=2, cafe=1)
            elif hour < 20:
                scores.update(transit=3, restaurant=2, mall=2, street=2, company=1)
            elif hour < 23:
                scores.update(home=4, restaurant=2, mall=1, convenience=1, cafe=1)
            else:
                scores.update(home=6, convenience=1)
        if mode == "morning":
            scores["home"] += 3
        elif mode == "ntr":
            scores["transit"] += 1
            scores["street"] += 1
            scores["home"] -= 1
        return scores

    def _adjust_scores_for_weather(self, scores: dict[str, float], weather: Any, is_day_off: bool):
        text = self._weather_text(weather)
        bad = self._is_bad_world_weather(weather)
        if bad:
            for key, meta in PLACE_TYPES.items():
                scores[key] += 1.8 if meta.get("indoor") else -2.5
            scores["transit"] += 1.0
            scores["mall"] += 1.0
            scores["cafe"] += 0.8
        elif is_day_off and CLEAR_WEATHER_RE.search(text):
            scores["park"] += 1.3
            scores["street"] += 0.7
        if HOT_COLD_RE.search(text):
            for key, meta in PLACE_TYPES.items():
                if meta.get("indoor"):
                    scores[key] += 0.7

    @staticmethod
    def _normalize_age_stage(value: Any) -> str:
        key = re.sub(r"\s+", "", str(value or "").strip().lower())
        if key in VALID_AGE_STAGES:
            return key
        return AGE_STAGE_ALIASES.get(key, "")

    @staticmethod
    def _normalize_day_anchor(value: Any) -> str:
        key = re.sub(r"\s+", "", str(value or "").strip().lower())
        if key in VALID_DAY_ANCHORS:
            return key
        return DAY_ANCHOR_ALIASES.get(key, "")

    def _life_profile(self, session_id: str) -> dict[str, str]:
        """角色生活档案（年龄段 + 白天职场锚点）。显式配置优先，其次缓存的 LLM 推断，最后中性默认。"""
        age = self._normalize_age_stage(self._get_session_cfg(session_id, "character_age_stage", ""))
        anchor = self._normalize_day_anchor(self._get_session_cfg(session_id, "character_day_anchor", ""))
        cached = (self._get_session_state(session_id) or {}).get("life_profile") if session_id else None
        if isinstance(cached, dict):
            age = age or self._normalize_age_stage(cached.get("age_stage"))
            anchor = anchor or self._normalize_day_anchor(cached.get("day_anchor"))
        return {"age_stage": age or "unknown", "day_anchor": anchor or "unknown"}

    def _apply_life_profile_to_scores(self, scores: dict[str, float], profile: dict[str, str] | None):
        """按角色职业重定向“白天工作时段”权重，并屏蔽身份不符的锚定职场。

        - 例行评分里的工作权重（公司+学校）会按角色职业的去向分布重新分配：上班族进公司、
          学生/教师进学校、工人进工厂、农民进田间、建筑工进工地、外卖/司机散到街道车站等。
        - 公司/学校/工厂/田间/工地属于身份锚定场所：只有正好是该职业的去向时才出现，否则清零，
          这样成年上班族不会出现在学校、未成年不会出现在公司、路人不会出现在车间或工地。
        - 无固定职场（主妇/自由职业/魅魔等设定或无法判断）时，把工作权重转到居家与休闲。
        """
        profile = profile or {}
        anchor = profile.get("day_anchor") or "unknown"
        age = profile.get("age_stage") or "unknown"
        # 未成年绝不出现在公司：即便职业被判成上班族，也按在校学生处理。
        if age == "minor" and anchor == "company":
            anchor = "school"
        work_weight = sum(scores.get(key, 0.0) for key in WORK_SIGNAL_PLACES)
        for key in ANCHOR_ONLY_PLACES:
            scores[key] = 0.0
        if work_weight <= 0:
            return  # 非白天工作时段，无需重定向
        spec = OCCUPATION_ANCHORS.get(anchor) or OCCUPATION_ANCHORS["home"]  # 未知职业按无固定职场处理
        for place_key, frac in spec["places"].items():
            scores[place_key] = scores.get(place_key, 0.0) + work_weight * frac

    def _effective_profile_text(self, session_id: str) -> str:
        parts = []
        if hasattr(self, "_get_effective_persona"):
            parts.append(self._get_effective_persona(session_id))
        for key in ("role_name", "bot_name"):
            val = self._get_session_cfg(session_id, key, "")
            if val:
                parts.append(str(val))
        state = self._get_session_state(session_id) if session_id else {}
        for key in ("custom_character", "custom_series"):
            val = state.get(key)
            if val:
                parts.append(str(val))
        return "\n".join(p for p in parts if p).strip()

    async def _ensure_life_profile(self, session_id: str, force: bool = False) -> dict[str, Any]:
        """按人设推断并缓存角色生活档案。命中缓存（人设未变）时不调用 LLM，开销可忽略。"""
        if not session_id or not self._world_runtime_enabled():
            return {}
        persona = self._effective_profile_text(session_id)
        phash = hashlib.md5(persona.encode("utf-8")).hexdigest()
        state = self._get_session_state(session_id)
        cached = state.get("life_profile")
        if not force and isinstance(cached, dict) and cached.get("persona_hash") == phash:
            return cached
        if not self.has_llm_config("image"):
            return cached if isinstance(cached, dict) else {}
        system = (
            "你是角色生活背景分析器。根据角色人设，判断角色的【职业/白天主要去向】和【年龄段】，只输出严格 JSON，不要解释。\n"
            "day_anchor 必须是其一: company(上班族/白领/有固定职场), school(在校学生或学校教职工), "
            "factory(工厂工人), farm(农民/务农), construction(建筑工人/工地), medical(医生/护士), "
            "retail(店员/营业员/服务员), delivery(外卖员/快递员), driver(司机/网约车), "
            "home(家庭主妇/自由职业/无业，或魅魔/精灵等无固定职场的设定), unknown(无法判断)。\n"
            "age_stage 必须是其一: minor(未成年/在读中小学生), adult(成年), unknown。\n"
            "未成年通常 school；成年教师 school；按人设里的真实职业选最贴切的一项，没有职业线索就 home。\n"
            "只输出: {\"day_anchor\":\"company|school|factory|farm|construction|medical|retail|delivery|driver|home|unknown\",\"age_stage\":\"minor|adult|unknown\"}"
        )
        try:
            text = await self._call_llm(system, persona or "（无人设）", temp=0.1, tag="life-profile", purpose="image")
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
        except Exception as exc:
            logger.warning("life profile inference failed: %s", exc)
            return cached if isinstance(cached, dict) else {}
        profile = {
            "age_stage": self._normalize_age_stage(parsed.get("age_stage")) or "unknown",
            "day_anchor": self._normalize_day_anchor(parsed.get("day_anchor")) or "unknown",
            "persona_hash": phash,
            "source": "llm",
            "updated_at": time.time(),
        }
        state["life_profile"] = profile
        self._save_session_state(session_id, state)
        return profile

    def _catalog_for_city(self, city: str) -> dict[str, list[str]]:
        key = self._city_catalog_key(city)
        catalog = getattr(self, "city_place_catalogs", {}).get(key, {}) if key else {}
        places = catalog.get("places") if isinstance(catalog, dict) else None
        return places if isinstance(places, dict) else {}

    def _place_example(self, city: str, place_key: str, index: int = 0) -> str:
        city_places = self._catalog_for_city(city).get(place_key) or []
        examples = [str(x).strip() for x in city_places if str(x).strip()]
        if not examples:
            examples = PLACE_TYPES[place_key]["examples"]
        return examples[index % len(examples)]

    def _top_place_candidates(self, city: str, scores: dict[str, float], count: int = 3) -> list[dict[str, Any]]:
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        candidates = []
        for idx, (key, score) in enumerate(ranked):
            if score <= 0:
                continue
            meta = PLACE_TYPES[key]
            candidates.append({
                "key": key,
                "label": meta["label"],
                "name": self._place_example(city, key, idx),
                "score": score,
                "public": bool(meta.get("public")),
                "indoor": bool(meta.get("indoor")),
                "views": list(meta.get("views", [])),
                "activities": list(meta.get("activities", [])),
            })
            if len(candidates) >= count:
                break
        return candidates

    def _active_user_place(self, state: dict[str, Any]) -> dict[str, Any] | None:
        try:
            ttl = max(0.25, float(self.config.get("world_user_place_ttl_hours", "4") or "4")) * 3600
        except Exception:
            ttl = 4 * 3600
        updated = float(state.get("user_place_updated_at", 0) or 0)
        if updated and time.time() - updated > ttl:
            return None
        if state.get("user_co_located"):
            return {
                "key": "__with_character__",
                "label": state.get("user_place_label") or "与角色同处",
                "text": state.get("user_place_text") or "",
                "updated_at": updated,
                "co_located": True,
            }
        key = (state.get("user_place") or "").strip()
        if key not in PLACE_TYPES:
            return None
        return {
            "key": key,
            "label": state.get("user_place_label") or PLACE_TYPES[key]["label"],
            "text": state.get("user_place_text") or "",
            "updated_at": updated,
            "co_located": False,
        }

    def _infer_user_place(self, text: str) -> tuple[str, str] | tuple[None, None]:
        text = text or ""
        if re.search(r"(不在家|没在家|不在公司|没在公司|不是在)", text):
            return None, None
        for key, pattern in PLACE_PATTERNS:
            match = pattern.search(text)
            if match:
                prefix = text[max(0, match.start() - 10):match.start()]
                mentions_self = re.search(r"(我|俺|咱|本人|这边|这里|这儿)", prefix)
                mentions_other = re.search(r"(你|姐姐|哥哥|妹妹|她|他|角色|蕾伊|小姐)", prefix)
                if mentions_other and not mentions_self:
                    continue
                return key, match.group(0)
        return None, None

    def _update_user_place_from_text(self, session_id: str, text: str) -> bool:
        if not session_id or not self._world_runtime_enabled():
            return False
        key, matched = self._infer_user_place(text)
        if not key:
            return False
        state = self._get_session_state(session_id)
        state["user_place"] = key
        state["user_place_label"] = PLACE_TYPES[key]["label"]
        state["user_place_text"] = matched or (text or "")[:40]
        state["user_place_updated_at"] = time.time()
        state["user_place_confidence"] = 0.85
        state["user_co_located"] = False  # 用户报了独立地点，清除同处标记，避免与历史同处状态打架
        self._mark_dirty(session_id)
        return True

    # ---- 角色位置持久化：对话/工具确立的位置在新鲜期内优先于时钟推断，消除跨上下文的位置漂移 ----
    def _active_character_place(self, state: dict[str, Any]) -> dict[str, Any] | None:
        try:
            ttl = max(0.25, float(self.config.get("world_character_place_ttl_hours", "4") or "4")) * 3600
        except Exception:
            ttl = 4 * 3600
        updated = float(state.get("character_place_updated_at", 0) or 0)
        if not updated or time.time() - updated > ttl:
            return None
        key = (state.get("character_place") or "").strip()
        if key not in PLACE_TYPES:
            return None
        return {
            "key": key,
            "label": state.get("character_place_label") or PLACE_TYPES[key]["label"],
            "text": state.get("character_place_text") or "",
            "updated_at": updated,
        }

    def _set_character_place(self, session_id: str, key: str, matched: str, confidence: float) -> bool:
        if key not in PLACE_TYPES:
            return False
        state = self._get_session_state(session_id)
        state["character_place"] = key
        state["character_place_label"] = PLACE_TYPES[key]["label"]
        state["character_place_text"] = (matched or "")[:40]
        state["character_place_updated_at"] = time.time()
        state["character_place_confidence"] = confidence
        self._mark_dirty(session_id)
        return True

    def _update_character_place_from_text(self, session_id: str, text: str) -> bool:
        """从角色（助手）回复里自动抽取角色自述所在并持久化。

        助手以角色身份发言，故 `_infer_user_place` 提取出的“说话者自述所在”即角色位置。
        自动抽取置信度低于工具显式声明。
        """
        if not session_id or not self._world_runtime_enabled():
            return False
        key, matched = self._infer_user_place(text)
        if not key:
            return False
        return self._set_character_place(session_id, key, matched or (text or ""), 0.8)

    async def tool_update_location(self, session_id: str, place: str = "") -> str:
        """聊天模型显式声明角色换到新地点时调用，持续生效，优先于时钟动线。"""
        place = (place or "").strip()
        if not place:
            return "未提供地点。"
        key, matched = self._infer_user_place(place)
        if not key:
            return f"无法识别地点「{place[:30]}」，位置未更新。可用：家/公司/学校/商场/咖啡店/餐厅/公园/街道/车站/便利店等。"
        self._set_character_place(session_id, key, matched or place, 0.95)
        self._ulog(session_id, "MOVE", f"角色移动到 {PLACE_TYPES[key]['label']}（{place[:30]}）")
        return f"已记录角色当前在 {PLACE_TYPES[key]['label']}。"

    def _apply_llm_user_location(
        self,
        session_id: str,
        user_location: str,
        co_located: bool,
        now: datetime | None = None,
    ) -> bool:
        """把生图前 LLM 对【用户当前位置 / 是否与角色同处】的判断写入会话状态。

        带迟滞：判成 unknown（或非法值）时不清空旧状态，保留到 TTL 自然过期，
        避免视角在“同处/异地”之间来回抖动。具体地点或同处判断才覆盖。
        """
        if not session_id or not self._world_runtime_enabled():
            return False
        loc = re.sub(r"\s+", "", str(user_location or "").strip().lower())
        state = self._get_session_state(session_id)
        now_ts = now.timestamp() if isinstance(now, datetime) else time.time()
        if co_located or loc in ("with_user", "with_character", "together", "同处", "一起"):
            state["user_co_located"] = True
            state["user_place"] = ""  # 同处时不绑定具体场所，跟随角色动线
            state["user_place_label"] = "与角色同处"
            state["user_place_text"] = "生图前判断：与角色在同一空间"
            state["user_place_updated_at"] = now_ts
            state["user_place_source"] = "llm"
            self._mark_dirty(session_id)
            return True
        if loc in PLACE_TYPES:
            state["user_co_located"] = False
            state["user_place"] = loc
            state["user_place_label"] = PLACE_TYPES[loc]["label"]
            state["user_place_text"] = "生图前推断"
            state["user_place_updated_at"] = now_ts
            state["user_place_source"] = "llm"
            self._mark_dirty(session_id)
            return True
        return False  # unknown / 非法值：迟滞，保留旧状态直到过期

    def _world_relation_text(self, character_place: dict[str, Any], user_place: dict[str, Any] | None) -> str:
        if not user_place:
            return "用户当前位置未知；角色按日常动线行动。"
        if user_place.get("co_located"):
            return "用户和角色此刻在同一空间，适合写成 POV 或近距离第三人称的同框互动；不要写成角色独自一人的前摄自拍。"
        if user_place["key"] == character_place["key"]:
            return "用户和角色处于同类场所，可自然写成同地点互动；适合 POV 或第三人称近距离场景。"
        return "用户和角色不在同一地点；优先写成消息、自拍、通勤或约定见面的场景，不要强行瞬移到用户身边。"

    def _world_constraints(self, character_place: dict[str, Any], weather: Any) -> list[str]:
        constraints = []
        if self._is_bad_world_weather(weather):
            constraints.append("恶劣天气时减少公园、大街等户外停留，优先室内或通勤避雨场景")
        if character_place.get("public"):
            constraints.append("公开场合穿着和动作保持得体，亲密内容需要收敛")
        else:
            constraints.append("私密场合可以更生活化，也更适合 POV 或对镜场景")
        views = " / ".join(character_place.get("views") or [])
        if views:
            constraints.append(f"推荐视角: {views}")
        return constraints

    def _next_period_datetime(self, now: datetime) -> tuple[datetime, str]:
        """返回今天接下来一个时间段的代表时刻及其名称（用于推断角色稍后会去的地方）。"""
        current = self._get_time_period(now.hour)
        nxt = PERIOD_ORDER[(PERIOD_ORDER.index(current) + 1) % len(PERIOD_ORDER)]
        rep = PERIOD_REP_HOUR[nxt]
        candidate = now.replace(hour=rep, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate, nxt

    def _place_for_time(
        self,
        city: str,
        now: datetime,
        weather: Any,
        mode: str = "chat",
        profile: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """按给定时刻推断角色最可能在的单个地点。"""
        day = self._day_type(now)
        scores = self._routine_scores(now, bool(day["is_day_off"]), mode=mode)
        self._adjust_scores_for_weather(scores, weather, bool(day["is_day_off"]))
        self._apply_life_profile_to_scores(scores, profile)
        candidates = self._top_place_candidates(city, scores, count=1)
        if not candidates:
            candidates = self._top_place_candidates(city, {"home": 1}, count=1)
        return candidates[0] if candidates else None

    def build_world_state(
        self,
        session_id: str,
        user_text: str = "",
        weather: Any = None,
        now: datetime | None = None,
        mode: str = "chat",
    ) -> dict[str, Any]:
        if not self._world_runtime_enabled():
            return {}
        state = self._get_session_state(session_id) if session_id else {}
        now = now or self._session_now(session_id)
        city = self._get_session_cfg(session_id, "location", self.config.get("location", "上海"))
        time_context = self._get_time_context(session_id, now=now, weather=weather)
        profile = self._life_profile(session_id)
        day = self._day_type(now)
        scores = self._routine_scores(now, bool(day["is_day_off"]), mode=mode)
        self._adjust_scores_for_weather(scores, weather, bool(day["is_day_off"]))
        self._apply_life_profile_to_scores(scores, profile)
        candidates = self._top_place_candidates(city, scores, count=3)
        if not candidates:
            candidates = [self._top_place_candidates(city, {"home": 1}, count=1)[0]]
        character_place = candidates[0]
        # 对话/工具确立的角色位置在新鲜期内优先于时钟推断（跨上下文重置、推送/生图都据此保持连续）。
        persisted = self._active_character_place(state)
        if persisted:
            pinned = self._top_place_candidates(city, {persisted["key"]: 1}, count=1)
            if pinned:
                character_place = pinned[0]
        next_now, next_period = self._next_period_datetime(now)
        next_place = self._place_for_time(city, next_now, weather, mode=mode, profile=profile)
        user_place = self._active_user_place(state)
        if user_text:
            key, matched = self._infer_user_place(user_text)
            if key:
                user_place = {"key": key, "label": PLACE_TYPES[key]["label"], "text": matched or "", "updated_at": time.time()}
        catalog = getattr(self, "city_place_catalogs", {}).get(self._city_catalog_key(city), {})
        enhanced = bool(isinstance(catalog, dict) and catalog.get("places"))
        return {
            "city": city,
            "now": now,
            "weekday": now.strftime("%A"),
            "day_type": day["label"],
            "time_period": time_context.get("period") or self._get_time_period(now.hour),
            "time_context": time_context,
            "weather": self._weather_text(weather),
            "weather_is_bad": self._is_bad_world_weather(weather),
            "character_place": character_place,
            "character_candidates": candidates,
            "next_place": next_place,
            "next_time_period": next_period,
            "life_profile": profile,
            "user_place": user_place,
            "relation": self._world_relation_text(character_place, user_place),
            "constraints": self._world_constraints(character_place, weather),
            "spatial_override": self._get_session_cfg(session_id, "spatial_relationship", ""),
            "catalog_source": "城市增强目录" if enhanced else "基础场所目录",
        }

    @staticmethod
    def _format_life_profile(profile: dict[str, str] | None) -> str:
        profile = profile or {}
        age_label = {"minor": "未成年", "adult": "成年"}.get(profile.get("age_stage", ""), "")
        spec = OCCUPATION_ANCHORS.get(profile.get("day_anchor", ""))
        anchor_label = spec["desc"] if spec else ""
        parts = [p for p in (age_label, anchor_label) if p]
        return "·".join(parts)

    def _format_world_context(
        self,
        session_id: str,
        user_text: str = "",
        weather: Any = None,
        mode: str = "chat",
        now: datetime | None = None,
        pin_location: bool = True,
    ) -> str:
        if weather is None:
            cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                weather = cached.get("data")
        world = self.build_world_state(session_id, user_text=user_text, weather=weather, now=now, mode=mode)
        if not world:
            return ""
        now = world["now"]
        cp = world["character_place"]
        activities = "、".join((cp.get("activities") or [])[:3])
        current_line = f"{cp['label']}（{cp['name']}）"
        if activities:
            current_line += f"，此刻多半在{activities}"
        np = world.get("next_place")
        if np:
            next_line = f"{world.get('next_time_period', '')}多半会去 {np['label']}（{np['name']}）"
        else:
            next_line = "暂不确定"
        user = world.get("user_place")
        user_text_line = f"{user['label']}（来自: {user.get('text') or '近期发言'}）" if user else "未知"
        identity = self._format_life_profile(world.get("life_profile"))
        lines = [
            "当前世界状态（由现实时间、星期/节假日、天气、城市地点和动线自动推断；用户明确说的位置优先）:",
            f"- 城市/时间: {world['city']}，{now.strftime('%Y-%m-%d %H:%M')}，{world['day_type']}，{world['time_period']}",
            f"- 天气: {world['weather']}",
        ]
        if hasattr(self, "_format_time_context"):
            lines.append(f"- 季节/自然光: {self._format_time_context(session_id, now=now, weather=weather)}")
        if identity:
            lines.append(f"- 角色身份: {identity}")
        if pin_location:
            lines += [
                f"- 角色当前所在: {current_line}",
                f"- 接下来动线: {next_line}",
                f"- 用户位置: {user_text_line}",
                f"- 空间关系判断: {world['relation']}",
            ]
        else:
            # 对话进行中：不钉死时钟算出的具体地点与相对关系（否则会和对话已建立的位置打架、导致瞬移），
            # 只给“这个时段日常多半在哪一带”的倾向作背景，当前所在交给对话决定。
            lines.append(
                f"- 日常此时多半在 {cp['label']} 一带（仅背景倾向，当前位置以对话为准，不要据此瞬移）"
            )
            lines.append(f"- 用户位置: {user_text_line}")
        lines += [
            f"- 场景约束: {'；'.join(world['constraints'])}",
            f"- 地点来源: {world['catalog_source']}",
        ]
        if world.get("spatial_override"):
            lines.append(f"- 额外空间设定: {world['spatial_override']}（作为高级覆盖项，不替代自动动线）")
        return "\n".join(lines)

    def _normalize_city_place_payload(self, payload: Any) -> dict[str, list[str]]:
        if isinstance(payload, dict) and isinstance(payload.get("places"), dict):
            payload = payload["places"]
        if not isinstance(payload, dict):
            return {}
        normalized: dict[str, list[str]] = {}
        for key, values in payload.items():
            key = str(key).strip().lower()
            if key not in CITY_CATALOG_KEYS:
                continue
            if isinstance(values, str):
                values = re.split(r"[,，、\n;；]+", values)
            if not isinstance(values, list):
                continue
            cleaned = []
            for value in values:
                text = str(value).strip()
                if text and text not in cleaned:
                    cleaned.append(text[:40])
                if len(cleaned) >= 6:
                    break
            if cleaned:
                normalized[key] = cleaned
        return normalized

    def _city_catalog_is_fresh(self, city: str) -> bool:
        key = self._city_catalog_key(city)
        catalog = getattr(self, "city_place_catalogs", {}).get(key, {}) if key else {}
        if not isinstance(catalog, dict) or not catalog.get("places"):
            return False
        try:
            ttl = max(1, float(self.config.get("world_city_places_ttl_days", "30") or "30")) * 86400
        except Exception:
            ttl = 30 * 86400
        return time.time() - float(catalog.get("updated_at", 0) or 0) < ttl

    async def _ensure_city_place_catalog(self, city: str, force: bool = False) -> dict[str, Any]:
        city = (city or "").strip()
        key = self._city_catalog_key(city)
        if not city or not self._world_city_places_enabled():
            return {"status": "disabled", "city": city, "places": {}}
        if not force and self._city_catalog_is_fresh(city):
            return {"status": "cached", "city": city, "places": self._catalog_for_city(city)}
        if not self.has_llm_config("image"):
            return {"status": "basic", "city": city, "places": {}}
        system = (
            "你是城市生活场景资料整理器。请为指定城市生成适合角色扮演日常动线的代表性地点。"
            "只输出严格 JSON，不要解释。键名固定为 places，内部键只能使用: "
            "home, company, school, park, mall, street, cafe, restaurant, transit, convenience, cinema, hotel, hospital, gym, factory, farm, construction。"
            "每类给 2 到 5 个真实或城市中常见的代表地点、商圈、区域或设施名。不要编造过于具体的门牌。"
        )
        user = f"城市: {city}\n输出示例: {{\"places\":{{\"park\":[\"某公园\"],\"mall\":[\"某商圈\"]}}}}"
        try:
            text = await self._call_llm(
                system,
                user,
                temp=0.2,
                tag="city-places",
                purpose="image",
            )
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
            places = self._normalize_city_place_payload(parsed)
        except Exception as exc:
            logger.warning("city place catalog generation failed for %s: %s", city, exc)
            return {"status": "failed", "city": city, "places": {}}
        if not places:
            return {"status": "failed", "city": city, "places": {}}
        if not hasattr(self, "city_place_catalogs"):
            self.city_place_catalogs = {}
        self.city_place_catalogs[key] = {
            "city": city,
            "updated_at": time.time(),
            "places": places,
        }
        self._write_state()
        return {"status": "generated", "city": city, "places": places}
