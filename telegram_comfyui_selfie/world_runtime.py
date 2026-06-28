from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from . import session_schema

import aiohttp

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
LOCATION_EXTRACT_TRIGGER_RE = re.compile(
    r"(我(?:现在|刚刚|刚|还|已经|正在)?(?:在|到|到了|回到|来到|坐在|躺在|站在|待在|留在|走到|进了).{0,12}(?:家|客厅|卧室|房间|门口|楼下|车里|公司|学校|餐厅|咖啡|星巴克|商场|车站|地铁|机场|医院|酒店|图书馆|电影院|公园|便利店|超市|海边|博物馆|店|街|路)|"
    r"刚到|已经到|到达|抵达|回家|到家|在家|出门|下楼|上楼|进门|到公司|在公司|还在公司|"
    r"到学校|在学校|到餐厅|在餐厅|到咖啡|在咖啡|到商场|在商场|到车站|在路上|在街上|"
    r"在地铁|在机场|在医院|在酒店|在图书馆|在电影院|在公园|在便利店|在超市|在海边|"
    r"\b(?:at|in|arrived at|got to|back home|at home|office|school|restaurant|cafe|mall|station|airport|hotel|hospital)\b)",
    re.IGNORECASE,
)

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
    # ---- 休闲/文化/景点类（对应高德 风景名胜110000 / 科教文化140000 / 体育休闲080000 等大类）----
    # 这些多为"目的地"而非每日固定动线：主要靠用户/模型显式钉位、城市目录召唤，节假日白天也会小幅进入候选。
    "museum": {
        "label": "博物馆",
        "examples": ["展厅", "馆内长廊", "展品前", "博物馆大厅", "馆外广场"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["看展", "参观", "拍照打卡", "歇脚"],
    },
    "landmark": {
        "label": "景点",
        "examples": ["观景台", "地标前", "景区广场", "石阶上", "栈道边"],
        "indoor": False,
        "public": True,
        "views": ["third", "selfie"],
        "activities": ["游览", "拍照打卡", "看风景", "散步"],
    },
    "temple": {
        "label": "寺庙神社",
        "examples": ["山门前", "鸟居下", "香炉旁", "祈愿牌前", "石阶上"],
        "indoor": False,
        "public": True,
        "views": ["third", "selfie"],
        "activities": ["参拜", "祈愿", "抽签", "散步"],
    },
    "library": {
        "label": "图书馆",
        "examples": ["书架间", "靠窗书桌", "自习区", "借阅台旁", "馆内楼梯"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["看书", "自习", "借书", "查资料"],
    },
    "zoo": {
        "label": "动物园/水族馆",
        "examples": ["展区前", "水族箱旁", "海洋馆隧道", "动物园入口", "温室花房"],
        "indoor": False,
        "public": True,
        "views": ["third", "selfie"],
        "activities": ["看动物", "拍照", "投喂", "散步"],
    },
    "amusement": {
        "label": "游乐园",
        "examples": ["摩天轮下", "旋转木马旁", "游乐园门口", "过山车前", "园内大街"],
        "indoor": False,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["玩游乐设施", "排队", "拍照打卡", "约会"],
    },
    "bar": {
        "label": "酒吧",
        "examples": ["吧台前", "卡座", "霓虹灯下", "酒吧门口", "驻唱舞台旁"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["喝酒", "听歌", "聊天", "夜晚放松"],
    },
    "ktv": {
        "label": "KTV",
        "examples": ["包厢沙发", "点歌屏前", "麦克风旁", "KTV走廊", "包厢门口"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "mirror", "third"],
        "activities": ["唱歌", "聚会", "玩闹", "夜晚消遣"],
    },
    "stadium": {
        "label": "体育馆/球场",
        "examples": ["看台", "场边", "入口通道", "球场边", "演出场馆外"],
        "indoor": False,
        "public": True,
        "views": ["third", "selfie"],
        "activities": ["看比赛", "看演出", "应援", "运动"],
    },
    "supermarket": {
        "label": "超市",
        "examples": ["生鲜区", "货架间", "购物车旁", "收银台前", "超市入口"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["采购", "买菜", "囤货", "逛超市"],
    },
    "bookstore": {
        "label": "书店",
        "examples": ["书架前", "阅读角", "新书台旁", "书店咖啡区", "落地窗边"],
        "indoor": True,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["看书", "选书", "歇脚", "拍照"],
    },
    "beach": {
        "label": "海边",
        "examples": ["沙滩上", "海浪边", "栈桥上", "礁石旁", "海滨步道"],
        "indoor": False,
        "public": True,
        "views": ["selfie", "third"],
        "activities": ["看海", "踏浪", "拍照", "散步"],
    },
    "salon": {
        "label": "美容美发",
        "examples": ["镜台前", "理发椅上", "美甲桌旁", "SPA房", "店内沙发"],
        "indoor": True,
        "public": True,
        "views": ["mirror", "selfie"],
        "activities": ["做头发", "美甲", "护理", "放松"],
    },
}

CITY_CATALOG_KEYS = set(PLACE_TYPES)

# 场所类目 → 高德 POI 关键字搜索用的中文关键字。只映射"有真实公共 POI"的类目；
# home/street/factory/farm/construction 没有有意义的可命名公共地点，留给 PLACE_TYPES 内置示例兜底。
AMAP_KEYWORDS: dict[str, str] = {
    "school": "学校", "park": "公园", "mall": "购物中心", "cafe": "咖啡厅",
    "restaurant": "餐厅", "transit": "地铁站", "convenience": "便利店",
    "cinema": "电影院", "hotel": "酒店", "hospital": "医院", "gym": "健身房",
    "company": "写字楼", "museum": "博物馆", "landmark": "景点", "temple": "寺庙",
    "library": "图书馆", "zoo": "动物园", "amusement": "游乐园", "bar": "酒吧",
    "ktv": "KTV", "stadium": "体育馆", "supermarket": "超市", "bookstore": "书店",
    "salon": "美容美发",
}

# 场所类目 → 谷歌 Places(New) 文本搜索关键词（英文）。高德覆盖不到的海外城市用谷歌兜全球。
# 同样只映射有真实公共 POI 的类目；home/street/factory/farm/construction 留给内置示例。
GOOGLE_KEYWORDS: dict[str, str] = {
    "school": "school", "park": "park", "mall": "shopping mall", "cafe": "cafe",
    "restaurant": "restaurant", "transit": "train station", "convenience": "convenience store",
    "cinema": "movie theater", "hotel": "hotel", "hospital": "hospital", "gym": "gym",
    "company": "office building", "museum": "museum", "landmark": "tourist attraction",
    "temple": "temple shrine", "library": "library", "zoo": "zoo aquarium",
    "amusement": "amusement park", "bar": "bar", "ktv": "karaoke", "stadium": "stadium",
    "supermarket": "supermarket", "bookstore": "bookstore", "salon": "beauty salon",
}

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

    def _should_run_location_extract(self, *texts: str) -> bool:
        """位置 LLM 抽取的便宜门控：没有地点/移动信号时不发起请求。"""
        combined = "\n".join(str(text or "") for text in texts if text)
        return bool(combined.strip() and LOCATION_EXTRACT_TRIGGER_RE.search(combined))

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
        # 休闲/文化目的地：仅节假日小幅加权，让博物馆、景点、海边、酒吧等偶尔进入候选，
        # 权重低于商场/公园不抢主导。日常这些地点主要靠用户/模型显式钉位或城市目录召唤。
        if is_day_off:
            if 9 <= hour < 18:
                for k in ("museum", "landmark", "zoo", "amusement", "bookstore", "library", "beach"):
                    scores[k] = scores.get(k, 0.0) + 1.0
            elif 18 <= hour < 24:
                for k in ("bar", "ktv", "stadium"):
                    scores[k] = scores.get(k, 0.0) + 1.0
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

    @staticmethod
    def _resolve_day_anchor(profile: dict[str, str] | None) -> str:
        """归一出角色实际的白天职场锚点。未成年即便被判成上班族也按在校学生处理。"""
        profile = profile or {}
        anchor = profile.get("day_anchor") or "unknown"
        age = profile.get("age_stage") or "unknown"
        if age == "minor" and anchor == "company":
            anchor = "school"
        return anchor

    def _profile_allowed_anchor_places(self, profile: dict[str, str] | None) -> set[str]:
        """角色按职业身份可以出现的身份锚定职场集合（公司/学校/工厂/田/工地）。

        无固定职场（主妇/自由职业/非人类设定）返回空集——这类角色不该出现在任何锚定职场。
        """
        anchor = self._resolve_day_anchor(profile)
        spec = OCCUPATION_ANCHORS.get(anchor) or OCCUPATION_ANCHORS["home"]
        return {key for key in spec["places"] if key in ANCHOR_ONLY_PLACES}

    def _apply_life_profile_to_scores(self, scores: dict[str, float], profile: dict[str, str] | None):
        """按角色职业重定向“白天工作时段”权重，并屏蔽身份不符的锚定职场。

        - 例行评分里的工作权重（公司+学校）会按角色职业的去向分布重新分配：上班族进公司、
          学生/教师进学校、工人进工厂、农民进田间、建筑工进工地、外卖/司机散到街道车站等。
        - 公司/学校/工厂/田间/工地属于身份锚定场所：只有正好是该职业的去向时才出现，否则清零，
          这样成年上班族不会出现在学校、未成年不会出现在公司、路人不会出现在车间或工地。
        - 无固定职场（主妇/自由职业/魅魔等设定或无法判断）时，把工作权重转到居家与休闲。
        """
        anchor = self._resolve_day_anchor(profile)
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

    @staticmethod
    def _within(updated_at: float, ttl_seconds: float | None = None, *, since: float = 0) -> bool:
        """统一的时效原语：判断 updated_at 是否仍“新鲜”。

        - `ttl_seconds=None`：不设年龄上限，只按 `since` 切点过滤（如照片只受短期重置边界约束）。
        - `since>0`：要求 updated_at 晚于该时刻（短期重置软边界）。

        刻意只做“算年龄/在不在窗口内”这一步——pin 的 strong/weak 分档、过滤条数上限、窗口
        策略等仍由各调用点自行决定（见 ②：薄原语不上收策略）。
        """
        ts = float(updated_at or 0)
        if not ts:
            return False
        if ttl_seconds is not None and time.time() - ts > ttl_seconds:
            return False
        if since and ts < float(since):
            return False
        return True

    def _active_user_place(self, state: dict[str, Any]) -> dict[str, Any] | None:
        try:
            ttl = max(0.25, float(self.config.get("world_user_place_ttl_hours", "4") or "4")) * 3600
        except Exception:
            ttl = 4 * 3600
        updated = session_schema.get_user_place_updated_at(state)
        if updated and not self._within(updated, ttl):
            return None
        if session_schema.get_user_co_located(state):
            return {
                "key": "__with_character__",
                "label": session_schema.get_user_place_label(state) or "与角色同处",
                "text": session_schema.get_user_place_text(state) or "",
                "updated_at": updated,
                "co_located": True,
            }
        key = session_schema.get_user_place(state)
        if key not in PLACE_TYPES:
            return None
        return {
            "key": key,
            "label": session_schema.get_user_place_label(state) or PLACE_TYPES[key]["label"],
            "text": session_schema.get_user_place_text(state) or "",
            "updated_at": updated,
            "co_located": False,
        }

    # ---- 角色位置持久化：对话/工具确立的位置在新鲜期内优先于时钟推断，消除跨上下文的位置漂移 ----
    def _active_character_place(self, state: dict[str, Any]) -> dict[str, Any] | None:
        try:
            ttl = max(0.25, float(self.config.get("world_character_place_ttl_hours", "4") or "4")) * 3600
        except Exception:
            ttl = 4 * 3600
        updated = session_schema.get_character_place_updated_at(state)
        if not self._within(updated, ttl):
            return None
        key = session_schema.get_character_place(state)
        if key not in PLACE_TYPES:
            return None
        # 权威分档（供生图侧区分"锁死"与"参考"）：
        # - strong：刚刚（< strong_hours，默认 1h）确认、轮次未过期（< stale_rounds，默认 8）、且高置信（≥0.8）。
        # - weak：仍在硬 TTL 内但已陈旧——时间过半、多轮未再确认、或本就是低置信抽取/冷启动回写。
        #   生图侧不再据此锁死，改作"参考 + 历史轨迹线索"，避免陈旧 pin 把角色卡死在某地。
        # - None：超过硬 TTL，完全回落时钟动线（由上面的 TTL 判定返回 None）。
        try:
            strong_hours = max(0.0, float(self.config.get("world_character_place_strong_hours", "1.0") or "1.0"))
        except Exception:
            strong_hours = 1.0
        try:
            stale_rounds = max(0, int(self.config.get("world_character_place_stale_rounds", "8") or "8"))
        except Exception:
            stale_rounds = 8
        age = time.time() - updated
        conf = session_schema.get_character_place_confidence(state)
        rounds_since = session_schema.get_rounds_since_location(state)
        authority = "strong" if (age <= strong_hours * 3600 and rounds_since < stale_rounds and conf >= 0.8) else "weak"
        return {
            "key": key,
            "label": session_schema.get_character_place_label(state) or PLACE_TYPES[key]["label"],
            "text": session_schema.get_character_place_text(state) or "",
            "name": session_schema.get_character_place_name(state),
            "updated_at": updated,
            "authority": authority,
            "confidence": conf,
            "age_hours": age / 3600.0,
            "rounds_since": rounds_since,
        }

    def _set_character_place(
        self,
        session_id: str,
        key: str,
        matched: str,
        confidence: float,
        *,
        source: str = "auto",
        name: str = "",
    ) -> bool:
        if key not in PLACE_TYPES:
            return False
        state = self._get_session_state(session_id)
        session_schema.set_character_place(
            state,
            key=key, label=PLACE_TYPES[key]["label"],
            text=(matched or "")[:40],
            name=(name or "").strip()[:40],
            updated_at=time.time(),
            confidence=confidence,
            rounds=0,  # 新一轮位置确认：清零距上次确认的轮数
        )
        # 位置历史轨迹（ring buffer，cap 20）。
        # 去重连续相同的地点——同一地点连续确认只记首条，避免一条 pin 把轨迹撑满。
        history = session_schema.get_character_place_history(state)
        if not history or history[-1].get("key") != key:
            session_schema.append_character_place_history(state, {
                "key": key,
                "label": PLACE_TYPES[key]["label"],
                "source": source,
                "confidence": confidence,
                "ts": time.time(),
            })
        self._mark_dirty(session_id)
        return True

    def _demote_character_place(self, state: dict[str, Any]):
        """把角色位置降级为 weak（不清空）：短期重置/新场景时不再钉死生图，但仍作背景，靠 4h TTL 自然老化。

        手段是把 `character_place_updated_at` 往后推到"strong 边界"（now - strong_hours），使
        `_active_character_place` 立刻判成 weak、但仍在硬 TTL 内。用 min() 保证只后移、绝不前移
        （已经陈旧的 pin 不会被刷新）。这样新场景的第一句位置声明会以高置信覆盖它，在覆盖前旧地点
        作为 weak 背景延续——连续过渡，而非瞬移或失忆。
        """
        updated = session_schema.get_character_place_updated_at(state)
        if not updated:
            return
        try:
            strong_hours = max(0.0, float(self.config.get("world_character_place_strong_hours", "1.0") or "1.0"))
        except Exception:
            strong_hours = 1.0
        session_schema.set_character_place_updated_at(
            state,
            min(updated, time.time() - strong_hours * 3600 - 1.0),
        )

    async def _update_character_place_from_text(self, session_id: str, text: str) -> bool:
        """用 LLM 从角色（助手）回复里判断角色此刻所在的具体地点并持久化。

        角色侧放弃正则（漏判多、误判多），改为每轮非空回复做一次轻量 LLM 分类。LLM 判成具体地点才写入
        （confidence 0.8），判成 unknown 或解析失败则不动 pin/计数。无 image-LLM 配置时直接返回——位置系统
        仍有 tool_update_location + 时钟动线兜底，不会瘫痪。锚定职场仍受职业身份门约束（见下方）。

        助手以角色身份发言，故"说话者此刻在哪"即角色位置。
        """
        if not session_id or not self._world_runtime_enabled() or not (text or "").strip():
            return False
        if not self._should_run_location_extract(text):
            return False
        if not self._bool_config("world_location_llm_extract", True) or not self.has_llm_config("image"):
            return False
        system = (
            "你专门判断角色扮演对话里【角色本人此刻所在的具体地点】。下面给你一段角色的发言，"
            "判断角色有没有交代自己现在身处哪个具体场所。只输出严格 JSON，不要解释。\n"
            "规则:\n"
            "- 只认角色作为【第一人称自述此刻所在】的明确交代（如“我现在在公司”“刚到家”“坐在星巴克”）。"
            "回忆、计划、提及他人位置、否定句（“不在家”）、反问（“你猜我在哪”）一律算 unknown。\n"
            "- 必须是具体场所，映射到枚举之一: home, company, school, park, mall, street, cafe, restaurant, "
            "transit, convenience, cinema, hotel, hospital, gym, factory, farm, construction, "
            "museum, landmark, temple, library, zoo, amusement, bar, ktv, stadium, supermarket, bookstore, beach, salon。"
            "无法判断或未交代填 unknown。\n"
            "- place_name 填角色自述的【具体地点名】（如\"上海海军博物馆\"、\"星巴克国金中心店\"、\"陆家嘴\"），没有具体名就填空字符串。\n"
            f"只输出: {{\"place\":\"home|company|school|park|mall|street|cafe|restaurant|transit|convenience|cinema|hotel|hospital|gym|factory|farm|construction|museum|landmark|temple|library|zoo|amusement|bar|ktv|stadium|supermarket|bookstore|beach|salon|unknown\",\"place_name\":\"具体地名或空\"}}"
        )
        try:
            raw = await self._call_llm(
                system,
                (text or "").strip()[:300],
                temp=0.1,
                tag="location-extract",
                purpose="image",
            )
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", (raw or "")).strip())
            key = str(parsed.get("place") or "").strip().lower()
            place_name = str(parsed.get("place_name") or "").strip()
        except Exception as exc:
            logger.warning("location LLM extract failed: %s", exc)
            return False
        if not key or key == "unknown" or key not in PLACE_TYPES:
            return False
        # 锚定职场（公司/学校/工厂/田/工地）仍受职业身份门约束：避免无固定职场角色被 LLM 抽到公司后钉死。
        # 模型显式声明换地点走 tool_update_location（0.95），不受此限。
        if key in ANCHOR_ONLY_PLACES and key not in self._profile_allowed_anchor_places(self._life_profile(session_id)):
            logger.debug("skip llm-extracted anchor place %s: 与角色职业身份不符", key)
            return False
        return self._set_character_place(session_id, key, text, 0.8, source="llm", name=place_name)

    def _infer_user_place_from_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        """从已有 checkpoint 摘要中推断用户位置，用于 user_place 过期/未知时的兜底。

        不调 LLM——仅用 PLACE_TYPES 标签/示例与 checkpoint 文本做关键词匹配。
        返回 dict 格式同 _active_user_place，或 None。
        """
        if not session_id or not self._world_runtime_enabled():
            return None
        cp_text = ""
        try:
            cp = self.app_store.get_checkpoint(session_id, self._context_character_key(session_id))
            cp_text = (cp.get("summary") or "").strip()
        except Exception:
            pass
        if not cp_text:
            state = self._get_session_state(session_id)
            cp_text = session_schema.get_checkpoint_summary(state) or ""
        if not cp_text:
            return None
        for pkey, pinfo in PLACE_TYPES.items():
            label = pinfo["label"]
            if label in cp_text:
                return {"key": pkey, "label": label, "co_located": False, "source": "checkpoint"}
            for ex in pinfo.get("examples", []):
                if ex in cp_text:
                    return {"key": pkey, "label": label, "co_located": False, "source": "checkpoint"}
        if any(w in cp_text for w in ("一起", "同处", "陪在", "在一起", "同一空间")):
            return {"key": "", "label": "与角色同处", "co_located": True, "source": "checkpoint"}
        return None

    async def tool_update_location(self, session_id: str, place: str = "") -> str:
        """聊天模型显式声明角色换到新地点时调用，持续生效，优先于时钟动线。"""
        place = (place or "").strip()
        if not place:
            return "未提供地点。"
        key = ""
        for pkey, pinfo in PLACE_TYPES.items():
            if pinfo["label"] in place or place in pinfo["label"]:
                key = pkey
                break
            if any(ex in place for ex in pinfo.get("examples", [])):
                key = pkey
                break
        if not key:
            return f"无法识别地点「{place[:30]}」，位置未更新。可用：家/公司/学校/商场/咖啡店/餐厅/公园/街道/车站/便利店等。"
        # place 是模型给的完整地名（如"上海海军博物馆"），整段存为具体地名；key 仅作类别用于动线规则。
        self._set_character_place(session_id, key, place, 0.95, source="tool", name=place)
        self._ulog(session_id, "MOVE", f"角色移动到 {PLACE_TYPES[key]['label']}（{place[:30]}）")
        return f"已记录角色当前在 {PLACE_TYPES[key]['label']}。"

    async def tool_update_user_location(self, session_id: str, place: str = "") -> str:
        """聊天模型从用户消息/上下文推断用户当前位置时调用，持续生效，覆盖冷启动未知状态。"""
        place = (place or "").strip()
        if not place:
            return "用户位置: 未提供地点。"
        co_located = any(w in place for w in ("同处", "一起", "同一空间", "在一起", "with_user", "with_character", "together"))
        if co_located:
            self._apply_llm_user_location(session_id, "with_user", True, text=f"工具推断：与角色同处（{place[:40]}）", source="tool")
            self._ulog(session_id, "ULOC", f"聊天 LLM 判定用户与角色同处（{place[:30]}）")
            return "已记录用户与角色同处。"
        key = ""
        for pkey, pinfo in PLACE_TYPES.items():
            if pinfo["label"] in place or place in pinfo["label"]:
                key = pkey
                break
            if any(ex in place for ex in pinfo.get("examples", [])):
                key = pkey
                break
        if not key:
            return f"无法识别用户地点「{place[:30]}」，未更新。可用：家/公司/学校/商场/咖啡店/餐厅/公园/街道/车站/便利店等。"
        self._apply_llm_user_location(session_id, key, False, text=f"工具推断：{place[:40]}", source="tool")
        self._ulog(session_id, "ULOC", f"聊天 LLM 判定用户在 {PLACE_TYPES[key]['label']}（{place[:30]}）")
        return f"已记录用户当前在 {PLACE_TYPES[key]['label']}。"

    def _apply_llm_user_location(
        self,
        session_id: str,
        user_location: str,
        co_located: bool,
        now: datetime | None = None,
        text: str = "",
        source: str = "llm",
    ) -> bool:
        """把 LLM 对【用户当前位置 / 是否与角色同处】的判断写入会话状态。

        带迟滞：判成 unknown（或非法值）时不清空旧状态，保留到 TTL 自然过期，
        避免视角在"同处/异地"之间来回抖动。具体地点或同处判断才覆盖。
        """
        if not session_id or not self._world_runtime_enabled():
            return False
        loc = re.sub(r"\s+", "", str(user_location or "").strip().lower())
        state = self._get_session_state(session_id)
        now_ts = now.timestamp() if isinstance(now, datetime) else time.time()
        if co_located or loc in ("with_user", "with_character", "together", "同处", "一起"):
            session_schema.set_user_place(
                state,
                key="", label="与角色同处", text=(text or "生图前判断：与角色在同一空间"),
                updated_at=now_ts, confidence=None, co_located=True, source=source,
            )
            self._mark_dirty(session_id)
            self._ulog(session_id, "LOC", f"用户位置判定 co_located=True user_location={user_location or '-'} → 与角色同处")
            return True
        if loc in PLACE_TYPES:
            session_schema.set_user_place(
                state,
                key=loc, label=PLACE_TYPES[loc]["label"], text=(text or "生图前推断"),
                updated_at=now_ts, confidence=None, co_located=False, source=source,
            )
            self._mark_dirty(session_id)
            self._ulog(session_id, "LOC", f"用户位置判定 co_located=False user_location={user_location or '-'} → 用户在 {PLACE_TYPES[loc]['label']}")
            return True
        self._ulog(session_id, "LOC", f"用户位置给出 co_located={co_located} user_location={user_location or '-'} → unknown/非法，迟滞保留旧状态")
        return False  # unknown / 非法值：迟滞，保留旧状态直到过期

    def _world_relation_text(self, character_place: dict[str, Any], user_place: dict[str, Any] | None) -> str:
        if not user_place:
            return "用户当前位置未知；角色按日常动线行动。"
        hedging = "（从近轮推断，用户本轮明确说的位置优先）" if user_place.get("source") == "checkpoint" else ""
        if user_place.get("co_located"):
            return f"用户和角色此刻应在同一空间{hedging}，适合写成 POV 或近距离第三人称的同框互动；不要写成角色独自一人的前摄自拍。"
        if user_place["key"] == character_place["key"]:
            return f"用户和角色处于同类场所{hedging}，可自然写成同地点互动；适合 POV 或第三人称近距离场景。"
        return f"用户和角色不在同一地点{hedging}；优先写成消息、自拍、通勤或约定见面的场景，不要强行瞬移到用户身边。"

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
        apply_persisted_place: bool = True,
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
        # apply_persisted_place=False 用于"按指定钟点预测一整天动线"的场景（如 WebUI 时间线）：
        # 持久 pin 是"此刻"的位置，新鲜度按真实墙钟判定，与传入的 now 无关；若对每个预测时段都套用，
        # 会把一整天都钉成同一个地点。预测应是纯时钟+职业动线，只有"此刻"才采用持久 pin。
        persisted = self._active_character_place(state) if apply_persisted_place else None
        if persisted:
            pkey = persisted["key"]
            conf = session_schema.get_character_place_confidence(state)
            # 低置信（自动抽取 0.8）钉到锚定职场时，仅在该场所时段合理（时钟+职业评分>0）才覆盖时钟，
            # 否则傍晚提一句“上班”会让角色深夜仍停在公司/学校。显式工具声明（0.95）尊重剧情、不受此限。
            if pkey in ANCHOR_ONLY_PLACES and conf < 0.9 and scores.get(pkey, 0.0) <= 0:
                persisted = None
        if persisted:
            pinned = self._top_place_candidates(city, {persisted["key"]: 1}, count=1)
            if pinned:
                character_place = pinned[0]
                # 明确说过的具体地名优先于目录示例（"约角色去上海海军博物馆"→显示这一家，而非该类里随便一家）。
                specific = (persisted.get("name") or "").strip()
                if specific and specific != character_place.get("label"):
                    character_place = {**character_place, "name": specific}
        next_now, next_period = self._next_period_datetime(now)
        next_place = self._place_for_time(city, next_now, weather, mode=mode, profile=profile)
        user_place = self._active_user_place(state)
        if not user_place:
            cp_inferred = self._infer_user_place_from_checkpoint(session_id)
            if cp_inferred:
                cp_key = cp_inferred.get("key", "")
                cp_label = PLACE_TYPES.get(cp_key, {}).get("label", "") if cp_key else ""
                user_disagrees = False
                if user_text:
                    ut = user_text
                    if cp_key and cp_label and cp_label in ut:
                        pass  # 用户消息印证了 checkpoint 推断
                    else:
                        for pk, pi in PLACE_TYPES.items():
                            if pk == cp_key:
                                continue
                            if pi["label"] in ut:
                                user_disagrees = True
                                break
                if not user_disagrees:
                    user_place = cp_inferred
        place_history = session_schema.get_character_place_history(state)
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
            "character_place_history": place_history,
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

    @staticmethod
    def _format_world_user_place_line(user: dict[str, Any] | None) -> str:
        if user:
            cp_source = user.get("source") or ""
            if cp_source == "checkpoint":
                return f"{user['label']}（从近轮对话推断，可能已变化；用户本轮明确说的位置优先）"
            if cp_source == "llm":
                return f"{user['label']}（从生图/发言推断：{user.get('text') or ''}）"
            return f"{user['label']}（{user.get('text') or '已确认'}）"
        return "未知"

    def _format_world_semistable_context(
        self,
        session_id: str,
        weather: Any = None,
        mode: str = "chat",
        now: datetime | None = None,
        pin_location: bool = True,
    ) -> str:
        """聊天用低频世界模板：只保留 session 内不变的世界规则。

        城市/日期/天气/季节自然光等会日内随时钟漂移的字段已移到动态尾部
        （见 _format_world_conditions_context），避免常驻槽内容随时间滚动
        而作废后面的 checkpoint+历史前缀缓存，并避免触发多余的强制 checkpoint。
        """
        if weather is None:
            cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                weather = cached.get("data")
        world = self.build_world_state(session_id, user_text="", weather=weather, now=now, mode=mode)
        if not world:
            return ""
        identity = self._format_life_profile(world.get("life_profile"))
        lines = [
            "世界状态规则（低频模板；城市/天气/季节自然光/精确时间/动线预判见动态尾部。"
            "按以下优先级确定位置: 用户本轮声明 > 工具/系统记录 > 时钟动线推断；用户文字明确说的位置始终优先）:",
        ]
        if identity:
            lines.append(f"- 角色身份: {identity}")
        lines.append(f"- 地点来源: {world['catalog_source']}")
        if world.get("spatial_override"):
            lines.append(f"- 额外空间设定: {world['spatial_override']}（作为高级覆盖项，不替代自动动线）")
        lines.append(
            "当前对话已建立的地点优先：如果对话里你已经处在某个地点（在家、在车站、在仓库等），或刚说过自己在哪，"
            "就保持那个地点不变，不要因为尾部动线预判显示的时间点不同而擅自挪到别处。"
            "只有开启全新话题、对话出现明显时间跳跃、或需要交代独自近况时，才依据动线更新所在地。"
            "无论如何不要无理由瞬移；与用户不在同一地点时，用消息、自拍、电话或约定见面推进。"
        )
        return "\n".join(lines)

    def _format_world_conditions_context(
        self,
        session_id: str,
        weather: Any = None,
        mode: str = "chat",
        now: datetime | None = None,
    ) -> str:
        """动态尾部世界条件：城市/日期/天气/季节自然光等日内随时钟漂移的字段。

        这些放在非缓存尾部（system_dynamic），让它们的变化不再作废常驻前缀，
        模型每轮仍能看到当前真实的天气/光线（与移动前展示内容一致，只换了位置）。
        """
        if weather is None:
            cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                weather = cached.get("data")
        world = self.build_world_state(session_id, user_text="", weather=weather, now=now, mode=mode)
        if not world:
            return ""
        now = world["now"]
        lines = [
            "世界当前条件（动态；随时间/天气变化，不影响前缀缓存）:",
            f"- 城市/日期: {world['city']}，{now.strftime('%Y-%m-%d')}，{world['day_type']}",
            f"- 天气: {world['weather']}",
        ]
        if hasattr(self, "_format_time_context"):
            lines.append(f"- 季节/自然光: {self._format_time_context(session_id, now=now, weather=weather)}")
        return "\n".join(lines)

    def _format_world_dynamic_context(
        self,
        session_id: str,
        user_text: str = "",
        weather: Any = None,
        mode: str = "chat",
        now: datetime | None = None,
        pin_location: bool = True,
    ) -> str:
        """聊天用高频世界动态：动线预判、本轮用户位置和空间关系。"""
        if weather is None:
            cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                weather = cached.get("data")
        world = self.build_world_state(session_id, user_text=user_text, weather=weather, now=now, mode=mode)
        if not world:
            return ""
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
        user_text_line = self._format_world_user_place_line(world.get("user_place"))
        lines = [
            "本轮动线与位置动态（高频；用户本轮声明优先）:",
        ]
        if pin_location:
            lines += [
                f"- 角色当前所在: {current_line}",
                f"- 接下来动线: {next_line}",
            ]
        else:
            lines.append(
                f"- 日常此时多半在 {cp['label']} 一带（仅背景倾向，当前位置以对话为准，不要据此瞬移）"
            )
        lines += [
            f"- 用户位置: {user_text_line}",
            f"- 场景约束: {'；'.join(world['constraints'])}",
        ]
        if pin_location:
            lines.append(f"- 空间关系判断: {world['relation']}")
        return "\n".join(lines)

    def _format_world_context(
        self,
        session_id: str,
        user_text: str = "",
        weather: Any = None,
        mode: str = "chat",
        now: datetime | None = None,
        pin_location: bool = True,
        apply_persisted_place: bool = True,
    ) -> str:
        if weather is None:
            cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                weather = cached.get("data")
        world = self.build_world_state(
            session_id,
            user_text=user_text,
            weather=weather,
            now=now,
            mode=mode,
            apply_persisted_place=apply_persisted_place,
        )
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
        user_text_line = self._format_world_user_place_line(world.get("user_place"))
        identity = self._format_life_profile(world.get("life_profile"))
        lines = [
            "当前世界状态（按以下优先级确定位置: 用户本轮声明 > 工具/系统记录 > 时钟动线推断；用户文字明确说的位置始终优先）:",
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

    def _external_http_proxy(self) -> tuple[str | None, aiohttp.BaseConnector | None]:
        """把 Telegram 代理配置复用到外部 HTTP 请求（高德/谷歌 POI）。

        返回 (proxy_url, connector)。HTTP(S) 代理直接传给请求参数；SOCKS 代理使用
        ProxyConnector。未启用代理时返回 (None, None)，由 trust_env 读取环境变量兜底。
        """
        proxy = self._telegram_proxy_url()
        if not proxy:
            return None, None
        if proxy.lower().startswith(("http://", "https://")):
            return proxy, None
        return None, self._telegram_proxy_connector()

    def _amap_enabled(self) -> bool:
        return bool(str(self.config.get("amap_api_key", "") or "").strip()) and self._bool_config("amap_poi_enabled", True)

    async def _fetch_amap_places(self, city: str) -> dict[str, list[str]]:
        """用高德 POI 关键字搜索为城市拉取各类目的真实地点名。

        每个类目一次请求（限定城市），失败/无结果的类目直接留空，由 PLACE_TYPES 内置示例兜底。
        只用城市级关键字搜索，不上传用户经纬度，避免泄露用户精确位置。
        """
        api_key = str(self.config.get("amap_api_key", "") or "").strip()
        if not api_key or not city:
            return {}
        try:
            per_type = max(2, min(10, int(self.config.get("amap_poi_per_type", 5) or 5)))
        except Exception:
            per_type = 5
        base = "https://restapi.amap.com/v3/place/text"
        sem = asyncio.Semaphore(3)  # 控制并发，照顾高德个人配额 QPS
        results: dict[str, list[str]] = {}

        async def fetch_one(session: aiohttp.ClientSession, place_key: str, keyword: str):
            params = {
                "key": api_key, "keywords": keyword, "city": city, "citylimit": "true",
                "offset": str(per_type), "page": "1", "extensions": "base", "output": "json",
            }
            try:
                async with sem:
                    async with session.get(base, params=params, **kwargs) as resp:
                        if resp.status != 200:
                            return
                        data = await resp.json(content_type=None)
            except Exception as exc:
                logger.debug("amap poi fetch failed %s/%s: %s", city, place_key, exc)
                return
            if str(data.get("status")) != "1":
                logger.debug("amap poi non-ok %s/%s: %s", city, place_key, data.get("info"))
                return
            names: list[str] = []
            for poi in data.get("pois", []) or []:
                name = str(poi.get("name") or "").strip()
                if name and name not in names:
                    names.append(name[:40])
                if len(names) >= per_type:
                    break
            if names:
                results[place_key] = names

        timeout = aiohttp.ClientTimeout(total=25)
        proxy, connector = self._external_http_proxy()
        kwargs: dict[str, Any] = {"proxy": proxy} if proxy else {}
        async with aiohttp.ClientSession(
            connector=connector, trust_env=(proxy is None and connector is None), timeout=timeout
        ) as session:
            await asyncio.gather(*(fetch_one(session, k, kw) for k, kw in AMAP_KEYWORDS.items()))
        return results

    async def _classify_city_region(self, city: str) -> str:
        """让 LLM 判定城市属于中国大陆还是海外，决定 POI 来源（高德管中国、谷歌管海外）。结果按城市缓存。

        返回 "china" / "overseas" / ""（无 image-LLM 或判定失败时返回空，交上层默认策略）。
        比依赖高德地理编码更稳——高德会把"神户/东京"这类海外名模糊匹配到同名的中国村庄/兴趣点。
        """
        city = (city or "").strip()
        if not city:
            return ""
        cache = getattr(self, "_city_region_cache", None)
        if cache is None:
            cache = self._city_region_cache = {}
        ck = self._city_catalog_key(city)
        if ck in cache:
            return cache[ck]
        if not self.has_llm_config("image"):
            return ""
        system = (
            "判断给定地名主要位于【中国大陆】还是【海外】。香港、澳门、台湾及其它国家都算 overseas。"
            "只输出严格 JSON，不要解释: {\"region\":\"china|overseas\"}"
        )
        try:
            raw = await self._call_llm(system, city, temp=0.0, tag="city-region", purpose="image")
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", (raw or "")).strip())
            region = str(parsed.get("region") or "").strip().lower()
        except Exception as exc:
            logger.warning("city region classify failed %s: %s", city, exc)
            return ""
        if region not in ("china", "overseas"):
            return ""
        cache[ck] = region
        return region

    def _google_places_enabled(self) -> bool:
        return bool(str(self.config.get("google_places_api_key", "") or "").strip()) and self._bool_config("google_places_enabled", True)

    async def _fetch_google_places(self, city: str) -> dict[str, list[str]]:
        """用谷歌 Places(New) 文本搜索为城市拉取各类目真实地点名（全球覆盖，给高德管不到的海外城市兜底）。

        每类一次 searchText 请求（textQuery="<英文关键词> in <城市>"）。失败/无结果的类目留空，由内置示例兜底。
        """
        api_key = str(self.config.get("google_places_api_key", "") or "").strip()
        if not api_key or not city:
            return {}
        try:
            per_type = max(2, min(20, int(self.config.get("amap_poi_per_type", 5) or 5)))
        except Exception:
            per_type = 5
        lang = str(self.config.get("google_places_language", "") or "").strip()
        url = "https://places.googleapis.com/v1/places:searchText"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName",  # 只取名字，控制计费档位
        }
        sem = asyncio.Semaphore(3)
        results: dict[str, list[str]] = {}

        async def fetch_one(session: aiohttp.ClientSession, place_key: str, keyword: str):
            body: dict[str, Any] = {"textQuery": f"{keyword} in {city}", "pageSize": per_type}
            if lang:
                body["languageCode"] = lang
            try:
                async with sem:
                    async with session.post(url, json=body, headers=headers, **kwargs) as resp:
                        if resp.status != 200:
                            logger.debug("google places http %s %s/%s", resp.status, city, place_key)
                            return
                        data = await resp.json(content_type=None)
            except Exception as exc:
                logger.debug("google places fetch failed %s/%s: %s", city, place_key, exc)
                return
            names: list[str] = []
            for poi in data.get("places", []) or []:
                name = str(((poi.get("displayName") or {}).get("text")) or "").strip()
                if name and name not in names:
                    names.append(name[:40])
                if len(names) >= per_type:
                    break
            if names:
                results[place_key] = names

        timeout = aiohttp.ClientTimeout(total=25)
        proxy, connector = self._external_http_proxy()
        kwargs = {"proxy": proxy} if proxy else {}
        async with aiohttp.ClientSession(
            connector=connector, trust_env=(proxy is None and connector is None), timeout=timeout
        ) as session:
            await asyncio.gather(*(fetch_one(session, k, kw) for k, kw in GOOGLE_KEYWORDS.items()))
        return results

    def _store_city_catalog(self, key: str, city: str, places: dict[str, list[str]], source: str):
        if not hasattr(self, "city_place_catalogs"):
            self.city_place_catalogs = {}
        catalog = {
            "city": city,
            "updated_at": time.time(),
            "places": places,
            "source": source,
        }
        self.city_place_catalogs[key] = catalog
        self.app_store.save_city_catalog(key, catalog)

    async def _ensure_city_place_catalog(self, city: str, force: bool = False) -> dict[str, Any]:
        city = (city or "").strip()
        key = self._city_catalog_key(city)
        if not city or not self._world_city_places_enabled():
            return {"status": "disabled", "city": city, "places": {}}
        if not force and self._city_catalog_is_fresh(city):
            return {"status": "cached", "city": city, "places": self._catalog_for_city(city)}
        # 由大模型判定中国/海外，决定 POI 来源优先级（高德管中国、谷歌管全球）。
        # china: 高德优先、谷歌兜底；overseas/未知: 只用谷歌——绝不让高德对海外城市出手，
        # 否则会把同名中国地点（神户→河北、东京→广西）当成结果污染目录。
        region = await self._classify_city_region(city)
        providers: list[str] = []
        if region == "china":
            if self._amap_enabled():
                providers.append("amap")
            if self._google_places_enabled():
                providers.append("google")
        else:  # overseas 或未知
            if self._google_places_enabled():
                providers.append("google")
        for prov in providers:
            if prov == "amap":
                places = self._normalize_city_place_payload(await self._fetch_amap_places(city))
            else:
                places = self._normalize_city_place_payload(await self._fetch_google_places(city))
            if places:
                self._store_city_catalog(key, city, places, source=prov)
                return {"status": prov, "city": city, "places": places}
            logger.info("%s 无结果，继续回落（region=%s）: %s", prov, region or "unknown", city)
        if not self.has_llm_config("image"):
            return {"status": "basic", "city": city, "places": {}}
        system = (
            "你是城市生活场景资料整理器。请为指定城市生成适合角色扮演日常动线的代表性地点。"
            "只输出严格 JSON，不要解释。键名固定为 places，内部键只能使用: "
            "home, company, school, park, mall, street, cafe, restaurant, transit, convenience, cinema, hotel, hospital, gym, factory, farm, construction, "
            "museum, landmark, temple, library, zoo, amusement, bar, ktv, stadium, supermarket, bookstore, beach, salon。"
            "每类给 2 到 5 个真实或城市中常见的代表地点、商圈、区域或设施名。不要编造过于具体的门牌。"
            "没有对应地点的类别（如该城市没有海边 beach）可以省略不输出。"
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
        self._store_city_catalog(key, city, places, source="llm")
        return {"status": "generated", "city": city, "places": places}
