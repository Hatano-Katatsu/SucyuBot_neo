from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


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
}

PLACE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("home", re.compile(r"(到家|回家|在家|家里|客厅|卧室|厨房|玄关|阳台|my home|at home)", re.I)),
    ("company", re.compile(r"(公司|办公室|上班|工位|写字楼|公司楼下|office|work)", re.I)),
    ("school", re.compile(r"(学校|教室|图书馆|上课|放学|校园|school|classroom|library)", re.I)),
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
        key = (state.get("user_place") or "").strip()
        if key not in PLACE_TYPES:
            return None
        try:
            ttl = max(0.25, float(self.config.get("world_user_place_ttl_hours", "4") or "4")) * 3600
        except Exception:
            ttl = 4 * 3600
        updated = float(state.get("user_place_updated_at", 0) or 0)
        if updated and time.time() - updated > ttl:
            return None
        return {
            "key": key,
            "label": state.get("user_place_label") or PLACE_TYPES[key]["label"],
            "text": state.get("user_place_text") or "",
            "updated_at": updated,
        }

    def _infer_user_place(self, text: str) -> tuple[str, str] | tuple[None, None]:
        text = text or ""
        if re.search(r"(不在家|没在家|不在公司|没在公司|不是在)", text):
            return None, None
        for key, pattern in PLACE_PATTERNS:
            match = pattern.search(text)
            if match:
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
        self._mark_dirty(session_id)
        return True

    def _world_relation_text(self, character_place: dict[str, Any], user_place: dict[str, Any] | None) -> str:
        if not user_place:
            return "用户当前位置未知；角色按日常动线行动。"
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
        day = self._day_type(now)
        scores = self._routine_scores(now, bool(day["is_day_off"]), mode=mode)
        self._adjust_scores_for_weather(scores, weather, bool(day["is_day_off"]))
        candidates = self._top_place_candidates(city, scores, count=3)
        if not candidates:
            candidates = [self._top_place_candidates(city, {"home": 1}, count=1)[0]]
        character_place = candidates[0]
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
            "time_period": self._get_time_period(now.hour),
            "weather": self._weather_text(weather),
            "weather_is_bad": self._is_bad_world_weather(weather),
            "character_place": character_place,
            "character_candidates": candidates,
            "user_place": user_place,
            "relation": self._world_relation_text(character_place, user_place),
            "constraints": self._world_constraints(character_place, weather),
            "spatial_override": self._get_session_cfg(session_id, "spatial_relationship", ""),
            "catalog_source": "城市增强目录" if enhanced else "基础场所目录",
        }

    def _format_world_context(
        self,
        session_id: str,
        user_text: str = "",
        weather: Any = None,
        mode: str = "chat",
    ) -> str:
        if weather is None:
            cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                weather = cached.get("data")
        world = self.build_world_state(session_id, user_text=user_text, weather=weather, mode=mode)
        if not world:
            return ""
        now = world["now"]
        candidates = " / ".join(
            f"{item['label']}({item['name']})" for item in world.get("character_candidates", [])
        )
        user = world.get("user_place")
        user_text_line = f"{user['label']}（来自: {user.get('text') or '近期发言'}）" if user else "未知"
        lines = [
            "当前世界状态（由现实时间、星期/节假日、天气、城市地点和动线自动推断；用户明确说的位置优先）:",
            f"- 城市/时间: {world['city']}，{now.strftime('%Y-%m-%d %H:%M')}，{world['day_type']}，{world['time_period']}",
            f"- 天气: {world['weather']}",
            f"- 角色动线: {candidates}",
            f"- 用户位置: {user_text_line}",
            f"- 空间关系判断: {world['relation']}",
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
            "home, company, school, park, mall, street, cafe, restaurant, transit, convenience, cinema, hotel, hospital, gym。"
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
