from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from . import appearance as appearance_rules
from . import session_schema


logger = logging.getLogger(__name__)


WARDROBE_STATE_EVENT_PREFIX = (
    "衣橱状态（系统记录，保留到 checkpoint/历史溢出统一裁剪；这是当前真实着装，后续对话与配图以此为准，不要主动复述）："
)

_CJK_RE = re.compile(r"[一-鿿]")


def _HAS_CJK(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))

CHAT_VISUAL_NOISE_TAGS = {
    "masterpiece", "best quality", "absurdres", "highres", "detailed illustration", "anime coloring",
    "clean lineart", "soft cel shading", "score_9", "score_8", "score_7", "safe", "sensitive",
    "1girl", "1boy", "girl", "boy", "woman", "man", "solo",
}
PERSISTENT_ACCESSORY_FAMILY_TERMS: dict[str, tuple[str, ...]] = {
    "glasses": ("glasses", "sunglasses", "spectacles"),
    "necklace": ("necklace",),
    "earring": ("earring", "earrings"),
    "bracelet": ("bracelet",),
    "ring": ("ring",),
    "hair_clip": ("hair clip", "hairclip", "hairpin", "clip"),
    "ribbon": ("ribbon",),
    "bow": ("bow",),
    "scarf": ("scarf",),
    "collar": ("collar",),
    "choker": ("choker",),
    "hat": ("hat", "cap"),
    "crown": ("crown", "tiara"),
    "watch": ("watch",),
    "belt": ("belt",),
    "glove": ("glove",),
    "mask": ("mask",),
    "veil": ("veil",),
}

class AppearanceRuntimeMixin:
    """角色画风、稳定外观、衣柜状态及换装工具运行时。"""

    def _effective_dynamic_appearance(self, session_id: str = "") -> str:
        """当前临时穿搭。全局默认 dynamic_appearance 只属于默认角色（魅魔）；
        一旦设了角色（OC/既有），就不再回退全局默认，避免默认服装串到东云绘名这类角色身上——
        既有角色没有自带初始穿搭时返回空，交给画面规划器按场景决定。"""
        state = self._get_session_state(session_id) if session_id else {}
        own = session_schema.get_outfit(state).strip() if state else ""
        if own:
            return own
        if session_id and self._is_character_set(session_id):
            return ""
        return self.config.get("dynamic_appearance", "")

    def _allow_llm_change_appearance(self, session_id: str) -> bool:
        state = self._get_session_state(session_id)
        override = session_schema.get_character_value(state, "custom_allow_llm_change_appearance")
        if isinstance(override, bool):
            return override
        if isinstance(override, str) and override.strip():
            return override.strip().lower() in ("true", "1", "yes", "on", "开", "允许", "启用")
        return bool(self.config.get("allow_llm_change_appearance", True))

    def _normalize_style_pool(self) -> list[str]:
        raw = self.config.get("style_pool") or self.config.get("style_prefix") or "@00 gx4"
        if isinstance(raw, str):
            parts = re.split(r"[\n;；]+", raw)
        elif isinstance(raw, list):
            parts = raw
        else:
            parts = []
        pool, seen = [], set()
        for item in parts:
            style = str(item).strip()
            if style and style.lower() not in seen:
                pool.append(style)
                seen.add(style.lower())
        if not pool:
            pool = ["@00 gx4"]
        current = str(self.config.get("current_style", "")).strip()
        if current not in pool:
            self.config["current_style"] = pool[0]
        self.config["style_pool"] = "\n".join(pool)
        return pool

    def _get_current_style(self, session_id: str = "") -> str:
        pool = self._normalize_style_pool()
        if session_id:
            state = self._get_session_state(session_id)
            custom = str(session_schema.get_character_value(state, "custom_current_style", "")).strip()
            if custom or self._is_character_set(session_id):
                return custom
        current = str(self.config.get("current_style", "")).strip()
        return current if current in pool else pool[0]

    def _set_current_style(self, session_id: str, style: str):
        style = (style or "").strip()
        if session_id:
            state = self._get_session_state(session_id)
            session_schema.set_character_value(state, "custom_current_style", style)
            if hasattr(self, "_snapshot_character"):
                self._snapshot_character(state)
            self._save_session_state(session_id, state)
        else:
            pool = self._normalize_style_pool()
            if style and style not in pool:
                pool.append(style)
                self.config["style_pool"] = "\n".join(pool)
            self.config["current_style"] = style
            self.save_config()

    def _ensure_style_pool_entry(self, style: str) -> bool:
        """把角色卡里出现的新画风补进全局画风池，供其他用户参考。"""
        style = (style or "").strip()
        if not style:
            return False
        pool = self._normalize_style_pool()
        if any(style.lower() == item.lower() for item in pool):
            return False
        pool.append(style)
        self.config["style_pool"] = "\n".join(pool)
        self.save_config()
        return True

    # Appearance / prompt
    # ---------------------------------------------------------------------
    def _load_keywords(self, key: str, defaults: list[str]) -> list[str]:
        return appearance_rules.load_keywords(self.config, key, defaults)

    @property
    def _outfit_kw(self):
        if not hasattr(self, "_cached_outfit_kw"):
            self._cached_outfit_kw = appearance_rules.outfit_keywords(self.config)
        return self._cached_outfit_kw

    @property
    def _accessory_kw(self):
        if not hasattr(self, "_cached_accessory_kw"):
            self._cached_accessory_kw = appearance_rules.accessory_keywords(self.config)
        return self._cached_accessory_kw

    def _parse_appearance(self, appearance: str) -> dict[str, list[str]]:
        return appearance_rules.parse_appearance(appearance, self._outfit_kw, self._accessory_kw)

    @staticmethod
    def _slots_to_string(slots: dict[str, list[str]]) -> str:
        return appearance_rules.slots_to_string(slots)

    @staticmethod
    def _remove_tag(text: str, tag: str) -> str:
        return appearance_rules.remove_tag(text, tag)

    def _merge_appearance(self, current_tags: str, new_tags: str, mode: str = "merge") -> str:
        return appearance_rules.merge_appearance(current_tags, new_tags, self._outfit_kw, self._accessory_kw, mode=mode)

    def _inject_appearance(self, char: str, session_id: str = "") -> str:
        return appearance_rules.inject_appearance(self, char, session_id)

    def _effective_visual_prompt_tags(self, session_id: str) -> str:
        state = self._get_session_state(session_id)
        if self._is_character_set(session_id):
            base = session_schema.get_character_value(state, "custom_positive_prefix", "") or self.config.get("positive_prefix", "")
        else:
            base = self._get_session_cfg(session_id, "positive_prefix", "")
        return self._inject_appearance(base, session_id).strip()

    @staticmethod
    def _clean_chat_visual_tags(tags: list[str], limit: int = 8) -> list[str]:
        kept: list[str] = []
        seen = set()
        for tag in tags:
            text = re.sub(r"\s+", " ", (tag or "").replace("_", " ").strip(" ,"))
            key = text.lower()
            if not key or key in CHAT_VISUAL_NOISE_TAGS or key.startswith("score_") or key.startswith("@"):
                continue
            if key in seen:
                continue
            kept.append(text)
            seen.add(key)
            if len(kept) >= limit:
                break
        return kept

    @staticmethod
    def _is_worn_or_carried_item(tag: str) -> bool:
        return bool(re.search(
            r"(glasses|necklace|earring|bracelet|ring|clip|hairpin|ribbon|scarf|collar|choker|"
            r"hat|cap|crown|tiara|watch|belt|bag|bow|glove|mask|veil|sword|blade|gun|staff|"
            r"wand|banner|flag|shield|cape|boots?|armor|ornament|accessor)",
            tag.lower(),
        ))

    @staticmethod
    def _persistent_accessory_family(tag: str) -> str:
        low = str(tag or "").lower()
        for family, terms in PERSISTENT_ACCESSORY_FAMILY_TERMS.items():
            if any(term in low for term in terms):
                return family
        return ""

    def _resolve_persistent_accessory_removals(
        self,
        state: dict[str, Any],
        clothing_off: str,
        *sources: str,
    ) -> list[str]:
        raw = (clothing_off or "").strip()
        if not raw:
            return []
        wardrobe = self._get_wardrobe(state)
        current_accessories = [
            appearance_rules.normalize_appearance_tag(tag)
            for tag in (wardrobe.get("accessory") or "").split(",")
            if tag.strip()
        ]
        if not current_accessories:
            return []
        requested = [
            appearance_rules.normalize_appearance_tag(tag)
            for tag in re.split(r"[,;]+", raw)
            if tag.strip()
        ]
        requested = [tag for tag in requested if self._persistent_accessory_family(tag)]
        if not requested:
            return []
        matched: list[str] = []
        seen: set[str] = set()
        for acc in current_accessories:
            acc_low = acc.lower()
            acc_family = self._persistent_accessory_family(acc_low)
            if not acc_family:
                continue
            for token in requested:
                tok_low = token.lower()
                tok_family = self._persistent_accessory_family(tok_low)
                if not tok_family:
                    continue
                if tok_low in acc_low or acc_low in tok_low or tok_family == acc_family:
                    if acc_low not in seen:
                        matched.append(acc)
                        seen.add(acc_low)
                    break
        return matched

    def _persist_removed_accessories_from_image(
        self,
        session_id: str,
        clothing_off: str,
        *sources: str,
    ) -> str:
        state = self._get_session_state(session_id)
        remove_tags = self._resolve_persistent_accessory_removals(state, clothing_off, *sources)
        if not remove_tags:
            return ""
        wardrobe_before = self._get_wardrobe(state)
        wardrobe_after = appearance_rules.apply_wardrobe_change(
            wardrobe_before,
            {"accessory_remove": ", ".join(remove_tags)},
        )
        if wardrobe_after == wardrobe_before:
            return ""
        session_schema.set_wardrobe(state, wardrobe_after)
        rendered = appearance_rules.render_wardrobe(wardrobe_after)
        session_schema.set_outfit(state, rendered)
        self._save_session_state(session_id, state)
        self._ulog(
            session_id,
            "WARDROBE",
            f'图像后持久化 accessory_remove={remove_tags} 来源=clothing_off="{clothing_off[:80]}" | 结果="{rendered[:140]}"',
        )
        return rendered

    def _chat_visible_appearance_context(self, session_id: str) -> str:
        effective = self._effective_visual_prompt_tags(session_id)
        if not effective:
            return ""
        state = self._get_session_state(session_id)
        dynamic_slots = self._parse_appearance(self._effective_dynamic_appearance(session_id))
        slots = self._parse_appearance(effective)
        hair = self._clean_chat_visual_tags(slots.get("hair", []), limit=6)
        eyes = self._clean_chat_visual_tags(slots.get("eyes", []), limit=4)
        outfit_source = dynamic_slots.get("outfit") or slots.get("outfit", [])
        outfit = self._clean_chat_visual_tags(outfit_source, limit=8)
        accessories = self._clean_chat_visual_tags(slots.get("accessory", []), limit=8)
        other = self._clean_chat_visual_tags(slots.get("other", []), limit=12)

        carried = [tag for tag in other if self._is_worn_or_carried_item(tag)]
        other = [tag for tag in other if tag not in carried]
        accessories = self._clean_chat_visual_tags(accessories + carried, limit=10)

        lines = []
        for label, values in (
            ("发型/发色", hair),
            ("眼睛", eyes),
            ("穿搭", outfit),
            ("配饰/随身物", accessories),
            ("其他显著特征", other),
        ):
            if values:
                lines.append(f"- {label}: {', '.join(values)}")
        return "\n".join(lines)

    async def _translate_appearance_tags(self, text: str) -> str:
        if not self.has_llm_config("image"):
            return text
        system = "你是 danbooru 标签翻译器。把中文外观、穿搭、发型、瞳色、配饰描述翻译成英文标签，逗号分隔。只输出标签。"
        try:
            return (await self._call_llm(system, text, temp=0.3, tag="appearance-translate", purpose="image")).strip() or text
        except Exception as exc:
            logger.warning("外观标签翻译失败: %s", exc)
            return text

    def _get_wardrobe(self, state: dict) -> dict:
        """取当前衣柜。衣柜与扁平 dynamic_appearance 不一致时（老数据无衣柜、或 webui 直接改了扁平串）
        以扁平串为准重新分槽——保证两者始终同步。"""
        wardrobe = session_schema.get_wardrobe(state)
        dyn = session_schema.get_outfit(state).strip()
        if not dyn:
            return {}
        if appearance_rules.render_wardrobe(wardrobe) != appearance_rules.normalize_appearance_text(dyn):
            wardrobe = appearance_rules.seed_wardrobe_from_text(dyn, self._outfit_kw, self._accessory_kw)
        return wardrobe

    def _wardrobe_closet_context(self, session_id: str) -> str:
        """给聊天模型看的衣橱清单（按槽位的中文名），让角色知道自己有哪些衣服。"""
        state = self._get_session_state(session_id)
        return appearance_rules.closet_summary(session_schema.get_closet(state))

    async def _classify_wardrobe_change(self, description: str, current_summary: str = "", closet_brief: str = "") -> dict:
        """大模型把一次换装描述拆解到固定衣柜槽位（含穿/脱/换意图），返回结构化 JSON。
        若用户点名衣橱里已有的衣服，用其英文标签填对应槽位；并给新穿上的衣物起个简短中文名（names）。"""
        system = (
            "你是角色换装分类器。把用户或角色描述的外观变化拆解到固定衣柜槽位，"
            "每个涉及的槽位填英文 danbooru 标签（可多件用逗号），不涉及的槽位留空。只输出 JSON，不要解释。\n"
            "服装槽位: dress, top, bottom, outerwear, bra, panties, legwear, footwear；外观槽位: hair(临时发型/发色), eyes(瞳色), other(其它视觉补充)。规则:\n"
            "- 连衣裙类（连衣裙/旗袍/和服/泳衣连体/jumpsuit/bodysuit）填 dress，系统会自动覆盖 top+bottom，不要再填 top/bottom。\n"
            "- 上半身衣物→top；下半身（裤/裙/短裤）→bottom；外套/夹克/大衣/开衫→outerwear；胸罩→bra；内裤→panties；袜/丝袜/连裤袜→legwear；鞋→footwear。\n"
            "- 眼镜/项链/耳环/手套/帽子/choker 等配饰：要戴上的填 accessory_add，要摘掉的填 accessory_remove。\n"
            "- 单件衣物的状态变化不要删除衣柜本体，写进 states：半脱/滑落/拉开/褪到一半→half_off；撕破/破损/裂开→damaged；脱掉/褪下/暂时不穿某一层→removed；整理好/穿回去/恢复正常→normal。\n"
            "- 全裸/脱光/把衣服都脱了：reset_all=true，让系统清空当前穿搭。\n"
            "- remove 只用于明确要求清空某槽位/以后不穿这个槽位，或发饰/配饰等非服装槽位的物理移除；普通剧情里的脱外套/脱内衣应写 states，不写 remove。\n"
            "- 若用户/剧情点名【衣橱里已有的衣服】（见下方清单），直接用清单里的英文标签填进对应槽位。\n"
            "- names：给本次新穿上的每个服装槽位起个简短中文名（如 dress→\"碎花连衣裙\"），用于衣橱收藏；没新衣物则留空。\n"
            "严格 JSON: {\"dress\":\"\",\"top\":\"\",\"bottom\":\"\",\"outerwear\":\"\",\"bra\":\"\",\"panties\":\"\",\"legwear\":\"\",\"footwear\":\"\",\"hair\":\"\",\"eyes\":\"\",\"other\":\"\",\"accessory_add\":\"\",\"accessory_remove\":\"\",\"remove\":[],\"states\":{},\"reset_all\":false,\"names\":{}}"
        )
        user = (
            f"当前衣柜（穿在身上）:\n{current_summary or '（空）'}\n\n"
            f"衣橱收藏（已有的衣服，可点名复穿）:\n{closet_brief or '（空）'}\n\n"
            f"要应用的外观变化: {description}"
        )
        text = await self._call_llm(system, user, temp=0.2, tag="wardrobe-classify", purpose="image", disable_thinking=True)
        parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
        if not isinstance(parsed, dict):
            raise ValueError("wardrobe classify did not return an object")
        return parsed

    async def _classify_wardrobe_items(self, state: dict, description: str) -> dict:
        """把一段衣物描述拆成 槽位→英文标签（不改动 state），给 WebUI「只存衣橱、暂不换上」用。
        LLM 分槽失败时回退关键词分槽，与 _wardrobe_apply_to_state 的兜底一致。"""
        desc = (description or "").strip()
        try:
            return await self._classify_wardrobe_change(
                desc,
                appearance_rules.wardrobe_summary(self._get_wardrobe(state)),
                appearance_rules.closet_brief_for_llm(session_schema.get_closet(state)),
            )
        except Exception as exc:
            logger.warning("wardrobe classify failed, fallback to keyword slotting: %s", exc)
            if re.search(r"[a-zA-Z]{3,}", desc) and not _HAS_CJK(desc):
                tags = desc
            else:
                tags = await self._translate_appearance_tags(desc)
            return appearance_rules.seed_wardrobe_from_text(tags, self._outfit_kw, self._accessory_kw)

    @staticmethod
    def _wardrobe_closet_display_name(description: str, slot: str, tags: str, names: dict, changed_slots: list[str]) -> str:
        """为衣橱条目选择给用户看的名字；英文 tags 只作为生图语义，不该吞掉用户输入名。"""
        raw = str((names or {}).get(slot) or "").strip()
        tag_norm = appearance_rules.normalize_appearance_text(tags or "")
        raw_norm = appearance_rules.normalize_appearance_text(raw)
        if raw and (_HAS_CJK(raw) or raw_norm != tag_norm):
            return raw[:40]

        desc = str(description or "").strip()
        if len(changed_slots) == 1 and _HAS_CJK(desc):
            cleaned = re.sub(
                r"^(?:请|帮我|给她|给角色|把|将|添加|加入|保存|收藏|存进|换上|穿上|穿|换|一件|一个|一条)\s*",
                "",
                desc,
            )
            cleaned = re.sub(r"(?:到|进)?(?:衣柜|衣橱|收藏|里面|里)$", "", cleaned).strip(" ，,。；;：:")
            if cleaned:
                return cleaned[:40]
        return raw

    async def _wardrobe_apply_to_state(self, state: dict, description: str, *, replace: bool = False, session_id: str = "") -> str:
        """把一次换装应用到 state（改 wardrobe + dynamic_appearance），不落盘——由调用方保存。"""
        desc = (description or "").strip()
        if desc.lower() in ("reset", "none", "clear", "无", "", "重置", "默认"):
            session_schema.set_wardrobe(state, {})
            session_schema.set_outfit(state, "")
            session_schema.clear_wardrobe_item_states(state)
            session_schema.clear_public_fallback_outfit(state)
            session_schema.clear_nudity(state)
            if session_id:
                self._ulog(session_id, "WARDROBE", f'desc="{desc[:80]}" → reset 清空全部穿搭')
            return ""
        if desc.lower() in ("恢复", "还原", "整理好", "穿好"):
            session_schema.clear_wardrobe_item_states(state)
            rendered = appearance_rules.render_wardrobe(self._get_wardrobe(state))
            if rendered.strip():
                session_schema.clear_nudity(state)
            if session_id:
                self._ulog(session_id, "WARDROBE", f'desc="{desc[:80]}" → 清除衣物状态')
            return rendered
        if self._TEMPORARY_NUDITY_RE.search(desc) and not self._PUT_ON_RE.search(desc):
            session_schema.set_wardrobe(state, {})
            session_schema.set_outfit(state, "")
            session_schema.clear_wardrobe_item_states(state)
            session_schema.clear_public_fallback_outfit(state)
            session_schema.set_nudity(state, "completely nude", at=time.time())
            if session_id:
                self._ulog(session_id, "WARDROBE", f'desc="{desc[:80]}" → 全裸/脱光清空全部穿搭')
            return ""
        wardrobe = {} if replace else self._get_wardrobe(state)
        closet = session_schema.get_closet(state)
        change: dict = {}
        try:
            change = await self._classify_wardrobe_change(
                desc, appearance_rules.wardrobe_summary(wardrobe), appearance_rules.closet_brief_for_llm(closet)
            )
            # 分类期间 WebUI/另一轮可能已修改衣柜；只把本轮增量应用到最新状态，避免 await 后整体覆盖。
            wardrobe = {} if replace else self._get_wardrobe(state)
            closet = session_schema.get_closet(state)
            wardrobe = appearance_rules.apply_wardrobe_change(wardrobe, change)
            # 守卫：非裸体语义下 reset_all 但没穿任何新衣服，多半是分类器误判，不清空衣柜。
            if change.get("reset_all") and not any(
                str(change.get(s) or "").strip()
                for s in appearance_rules.WARDROBE_CLOTHING_SLOTS
            ) and not self._TEMPORARY_NUDITY_RE.search(desc):
                if session_id:
                    self._ulog(session_id, "WARDROBE", f"拦截 reset_all 无新衣: desc=\"{desc[:120]}\"")
                return ""
        except Exception as exc:
            logger.warning("wardrobe classify failed, fallback to keyword slotting: %s", exc)
            if re.search(r"[a-zA-Z]{3,}", desc) and not _HAS_CJK(desc):
                tags = desc
            else:
                tags = await self._translate_appearance_tags(desc)
            wardrobe = {} if replace else self._get_wardrobe(state)
            closet = session_schema.get_closet(state)
            seed = appearance_rules.seed_wardrobe_from_text(tags, self._outfit_kw, self._accessory_kw)
            remove_intent = bool(self._TAKE_OFF_RE.search(desc)) and not self._PUT_ON_RE.search(desc)
            if remove_intent:
                matched_slots = [
                    slot for slot, worn in wardrobe.items()
                    if slot in appearance_rules.WARDROBE_CLOTHING_SLOTS
                    and any(
                        token and token in f"{desc.lower()} {tags.lower()}"
                        for token in re.split(r"[,\s]+", str(worn or "").lower())
                        if len(token) >= 3
                    )
                ]
                if not matched_slots and len(wardrobe) == 1:
                    matched_slots = list(wardrobe)
                change = {"remove": matched_slots, "states": {slot: "removed" for slot in matched_slots}}
                wardrobe = appearance_rules.apply_wardrobe_change(wardrobe, change)
            else:
                change = {slot: val for slot, val in seed.items()}
                wardrobe = appearance_rules.apply_wardrobe_seed(wardrobe, seed)
        if replace:
            session_schema.clear_public_fallback_outfit(state)
        # 自动收藏：仅把【本次新穿上】的服装存进衣橱（用应用后的标签，含点名复穿时解析出的标签）。
        names = change.get("names") if isinstance(change.get("names"), dict) else {}
        changed_slots = [
            slot for slot in appearance_rules.WARDROBE_CLOTHING_SLOTS
            if str(change.get(slot) or "").strip()
        ]
        now = time.time()
        for slot in changed_slots:
            tags = (wardrobe.get(slot) or "").strip()
            if tags:
                name = self._wardrobe_closet_display_name(desc, slot, tags, names, changed_slots)
                closet = appearance_rules.closet_add(closet, name, slot, tags, now=now)
        session_schema.set_closet(state, closet)
        session_schema.set_wardrobe(state, wardrobe)
        state_changes = change.get("states") if isinstance(change.get("states"), dict) else {}
        clear_state_slots = set(changed_slots)
        clear_state_slots.update(str(slot or "").strip() for slot in (change.get("remove") or []) if str(slot or "").strip())
        if clear_state_slots:
            session_schema.clear_wardrobe_item_states(state, clear_state_slots)
        for slot, value in state_changes.items():
            slot = str(slot or "").strip()
            if slot not in appearance_rules.WARDROBE_CLOTHING_SLOTS:
                continue
            if not str(wardrobe.get(slot) or "").strip():
                session_schema.clear_wardrobe_item_states(state, [slot])
                continue
            session_schema.set_wardrobe_item_state(state, slot, value)
        session_schema.prune_wardrobe_item_states(state, wardrobe)
        rendered = appearance_rules.render_wardrobe(wardrobe)
        session_schema.set_outfit(state, rendered)
        # 她重新穿上了衣服 → 解除持久裸体态（换装是"穿回衣服"的明确叙事事件）。
        if rendered.strip():
            session_schema.clear_nudity(state)
        if session_id:
            slots = {k: v for k, v in change.items() if k != "names" and v not in ("", [], False, None)}
            self._ulog(session_id, "WARDROBE", f'desc="{desc[:80]}" replace={replace} → 分槽={slots} | 结果="{rendered[:140]}"')
        return rendered

    async def _apply_wardrobe(self, session_id: str, description: str, *, replace: bool = False) -> str:
        """换装统一入口：分槽（LLM 主判，关键词兜底）→ 应用规则 → 渲染回 dynamic_appearance 并持久化。"""
        state = self._get_session_state(session_id)
        rendered = await self._wardrobe_apply_to_state(state, description, replace=replace, session_id=session_id)
        self._save_session_state(session_id, state)
        return rendered or "（已清空）"

    _TEMPORARY_NUDITY_RE = re.compile(
        r"\b(?:脱[光精]|全裸|裸体|一[丝条]不[挂卦]|脱[得掉][精光一].*|"
        r"nude|naked|strip(?:\s+naked)?|get\s+naked|take\s+off\s+(?:everything|all|clothes)|"
        r"completely\s+(?:nude|naked|undressed)|stark\s+naked|nothing\s+on|no\s+clothes)\b",
        re.IGNORECASE,
    )
    _PUT_ON_RE = re.compile(
        r"\b(?:换[上穿]|穿[上回]|put\s+on|wear|change\s+(?:into|to)|换上)", re.IGNORECASE
    )
    _TAKE_OFF_RE = re.compile(
        r"(?:脱掉|脱下|褪下|摘掉|取下|不穿|take\s+off|remove|without)", re.IGNORECASE
    )

    @staticmethod
    def _coerce_wardrobe_tool_items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def _wardrobe_state_snapshot(self, session_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = state if state is not None else self._get_session_state(session_id)
        wardrobe = {
            slot: appearance_rules.normalize_appearance_text(str(value or ""))
            for slot, value in self._get_wardrobe(state).items()
            if slot in appearance_rules.WARDROBE_RENDER_ORDER and str(value or "").strip()
        }
        states = {
            slot: value
            for slot, value in session_schema.get_wardrobe_item_states(state).items()
            if slot in appearance_rules.WARDROBE_CLOTHING_SLOTS and slot in wardrobe
        }
        nudity = session_schema.get_nudity(state)
        nudity_at = session_schema.get_nudity_at(state)
        visual_context = self._chat_visible_appearance_context(session_id)
        closet_context = self._wardrobe_closet_context(session_id)
        signature_payload = {
            "wardrobe": wardrobe,
            "item_states": states,
            "nudity": nudity,
            "nudity_at": nudity_at,
            "visual_context": visual_context,
            "closet_context": closet_context,
        }
        signature = hashlib.sha1(
            json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "version": 1,
            "wardrobe": wardrobe,
            "item_states": states,
            "outfit": appearance_rules.render_wardrobe(wardrobe),
            "nudity": nudity,
            "nudity_at": nudity_at,
            "state_signature": signature,
            "visual_context": visual_context,
            "closet_context": closet_context,
        }

    @staticmethod
    def _format_wardrobe_state_system_message(snapshot: dict[str, Any]) -> dict[str, str]:
        return {
            "role": "system",
            "content": (
                f"{WARDROBE_STATE_EVENT_PREFIX}\n"
                f"state_json: {json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
            ),
        }

    @staticmethod
    def _parse_wardrobe_state_system_message(message: dict[str, Any]) -> dict[str, Any] | None:
        if str(message.get("role") or "") != "system":
            return None
        content = str(message.get("content") or "")
        if not content.startswith(WARDROBE_STATE_EVENT_PREFIX) or "state_json:" not in content:
            return None
        try:
            parsed = json.loads(content.split("state_json:", 1)[1].strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _capture_wardrobe_semistable_before_tool(self, session_id: str, state: dict[str, Any]) -> bool:
        if session_schema.get_wardrobe_semistable_snapshot(state):
            return False
        baseline = session_schema.get_wardrobe_observed_snapshot(state)
        if not baseline:
            baseline = self._wardrobe_state_snapshot(session_id, state)
        session_schema.set_wardrobe_semistable_snapshot(state, baseline)
        return True

    def _record_external_wardrobe_change_before_user(self, session_id: str) -> bool:
        """WebUI/命令直接改衣橱后，在下一条 user 入历史前补一条统一状态事件。"""
        state = self._get_session_state(session_id)
        current = self._wardrobe_state_snapshot(session_id, state)
        observed = session_schema.get_wardrobe_observed_snapshot(state)
        if not observed:
            # 首次建立前缀基线；此时没有可比较的旧上下文，不制造伪变更事件。
            session_schema.set_wardrobe_observed_snapshot(state, current)
            self._save_session_state(session_id, state)
            return False

        represented_signature = str(observed.get("state_signature") or "")
        for message in reversed(session_schema.get_chat_history(state)):
            parsed = self._parse_wardrobe_state_system_message(message)
            if parsed is not None:
                represented_signature = str(parsed.get("state_signature") or "")
                break
        current_signature = str(current.get("state_signature") or "")
        if current_signature and current_signature == represented_signature:
            return False
        if not session_schema.get_wardrobe_semistable_snapshot(state):
            session_schema.set_wardrobe_semistable_snapshot(state, observed)
        session_schema.set_wardrobe_observed_snapshot(state, current)
        self._append_chat_history_messages(
            session_id,
            [self._format_wardrobe_state_system_message(current)],
        )
        self._ulog(session_id, "WARDROBE", "检测到聊天外衣橱变更，已在本轮 user 前追加衣橱状态 system 事件")
        return True

    def _pending_wardrobe_history_bucket(self) -> dict[str, dict[str, str]]:
        bucket = getattr(self, "_pending_wardrobe_history_messages", None)
        if not isinstance(bucket, dict):
            bucket = {}
            self._pending_wardrobe_history_messages = bucket
        return bucket

    def _queue_pending_wardrobe_history_message(self, session_id: str, snapshot: dict[str, Any]) -> None:
        # 同一轮有多次换装时只保留最终快照，历史无需回放中间态。
        self._pending_wardrobe_history_bucket()[session_id] = self._format_wardrobe_state_system_message(snapshot)

    def _take_pending_wardrobe_history_messages(self, session_id: str) -> list[dict[str, str]]:
        message = self._pending_wardrobe_history_bucket().pop(session_id, None)
        return [message] if isinstance(message, dict) else []

    def _apply_wardrobe_state_snapshot(self, state: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        raw_wardrobe = snapshot.get("wardrobe")
        if not isinstance(raw_wardrobe, dict):
            return False
        wardrobe = {
            slot: appearance_rules.normalize_appearance_text(str(value or ""))
            for slot, value in raw_wardrobe.items()
            if slot in appearance_rules.WARDROBE_RENDER_ORDER and str(value or "").strip()
        }
        session_schema.set_wardrobe(state, wardrobe)
        session_schema.set_outfit(state, appearance_rules.render_wardrobe(wardrobe))
        session_schema.clear_wardrobe_item_states(state)
        raw_states = snapshot.get("item_states")
        if isinstance(raw_states, dict):
            for slot, value in raw_states.items():
                if slot in appearance_rules.WARDROBE_CLOTHING_SLOTS and slot in wardrobe:
                    session_schema.set_wardrobe_item_state(state, slot, value)
        session_schema.prune_wardrobe_item_states(state, wardrobe)
        nudity = str(snapshot.get("nudity") or "").strip()
        if nudity:
            session_schema.set_nudity(state, nudity, at=float(snapshot.get("nudity_at") or 0) or time.time())
        else:
            session_schema.clear_nudity(state)
        return True

    def _sync_wardrobe_checkpoint_events(
        self,
        session_id: str,
        state: dict[str, Any],
        pending: list[dict[str, Any]],
        overflow: list[dict[str, Any]],
    ) -> bool:
        pending_events = [
            (index, parsed)
            for index, message in enumerate(pending)
            if (parsed := self._parse_wardrobe_state_system_message(message)) is not None
        ]
        if not pending_events:
            return False
        latest_index, latest_snapshot = pending_events[-1]
        current_before = self._wardrobe_state_snapshot(session_id, state)
        observed_before = session_schema.get_wardrobe_observed_snapshot(state)
        has_unrecorded_external_change = bool(
            observed_before
            and current_before.get("state_signature")
            and current_before.get("state_signature") != observed_before.get("state_signature")
        )
        if has_unrecorded_external_change:
            # WebUI/命令可能刚写入了比历史事件更新的真实状态；不能被旧 system 快照回滚。
            changed = False
        else:
            changed = self._apply_wardrobe_state_snapshot(state, latest_snapshot)
            session_schema.set_wardrobe_observed_snapshot(state, latest_snapshot)
        overflow_events = [
            (index, parsed)
            for index, message in enumerate(overflow)
            if (parsed := self._parse_wardrobe_state_system_message(message)) is not None
        ]
        if overflow_events:
            overflow_index, overflow_snapshot = overflow_events[-1]
            if latest_index <= len(overflow) - 1:
                # 所有衣橱事件都已折叠，半稳定层可直接追上真实数据。
                session_schema.clear_wardrobe_semistable_snapshot(state)
            else:
                # 仍有更新事件留在未折叠历史：半稳定层只推进到已 checkpoint 的最后状态。
                session_schema.set_wardrobe_semistable_snapshot(state, overflow_snapshot)
            changed = True
        return changed

    def _restore_wardrobe_after_history_retract(
        self,
        state: dict[str, Any],
        remaining: list[dict[str, Any]],
        removed: list[dict[str, Any]],
    ) -> bool:
        if not any(self._parse_wardrobe_state_system_message(message) is not None for message in removed):
            return False
        target = None
        for message in reversed(remaining):
            target = self._parse_wardrobe_state_system_message(message)
            if target is not None:
                break
        if target is None:
            frozen = session_schema.get_wardrobe_semistable_snapshot(state)
            if isinstance(frozen.get("wardrobe"), dict):
                target = frozen
        if not isinstance(target, dict) or not self._apply_wardrobe_state_snapshot(state, target):
            return False
        session_schema.set_wardrobe_observed_snapshot(state, target)
        session_schema.clear_wardrobe_semistable_snapshot(state)
        return True

    def _wardrobe_tool_result(self, snapshot: dict[str, Any]) -> str:
        outfit = str(snapshot.get("outfit") or "").strip() or "（无穿着）"
        states = snapshot.get("item_states") if isinstance(snapshot.get("item_states"), dict) else {}
        state_text = "、".join(f"{slot}={value}" for slot, value in states.items()) or "全部正常"
        compact = {
            key: snapshot.get(key)
            for key in ("version", "wardrobe", "item_states", "outfit", "nudity", "state_signature")
        }
        return (
            f"衣橱已更新。最新着装: {outfit}；部件状态: {state_text}。\n"
            f"state_json: {json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _apply_structured_wardrobe_items(
        self,
        state: dict[str, Any],
        items: Any,
        *,
        mode: str = "merge",
        clear_all: bool = False,
        session_id: str = "",
    ) -> tuple[bool, str]:
        normalized_items = self._coerce_wardrobe_tool_items(items)
        if not normalized_items and not clear_all:
            return False, "没有有效的衣物操作，衣橱未改变。"
        if clear_all:
            session_schema.set_wardrobe(state, {})
            session_schema.set_outfit(state, "")
            session_schema.clear_wardrobe_item_states(state)
            session_schema.clear_public_fallback_outfit(state)
            session_schema.set_nudity(state, "completely nude", at=time.time())
            return True, ""

        wardrobe = {} if mode == "replace" else self._get_wardrobe(state)
        closet = session_schema.get_closet(state)
        change: dict[str, Any] = {"states": {}, "remove": []}
        names: dict[str, str] = {}
        valid_count = 0
        accessory_add: list[str] = []
        accessory_remove: list[str] = []
        worn_slots: list[str] = []
        for item in normalized_items:
            slot = str(item.get("slot") or "").strip().lower()
            action = str(item.get("action") or "wear").strip().lower().replace("-", "_")
            tags = appearance_rules.normalize_appearance_text(str(item.get("tags") or ""))
            state_value = str(item.get("state") or "").strip()
            if slot not in appearance_rules.WARDROBE_RENDER_ORDER:
                continue
            if action in {"wear", "set", "put_on"}:
                if not tags:
                    continue
                if slot == "accessory":
                    accessory_add.append(tags)
                else:
                    change[slot] = tags
                if slot in appearance_rules.WARDROBE_CLOTHING_SLOTS:
                    worn_slots.append(slot)
                    name = str(item.get("name") or "").strip()
                    if name:
                        names[slot] = name[:40]
                if state_value and slot in appearance_rules.WARDROBE_CLOTHING_SLOTS:
                    change["states"][slot] = state_value
                valid_count += 1
            elif action in {"remove", "take_off", "delete"}:
                if slot == "accessory" and tags:
                    accessory_remove.append(tags)
                else:
                    change["remove"].append(slot)
                valid_count += 1
            elif action in {"set_state", "state", "restore"}:
                if slot not in appearance_rules.WARDROBE_CLOTHING_SLOTS:
                    continue
                change["states"][slot] = "normal" if action == "restore" else (state_value or "normal")
                valid_count += 1
        if not valid_count:
            return False, "没有有效的衣物操作，衣橱未改变。"
        if accessory_add:
            change["accessory_add"] = ", ".join(accessory_add)
        if accessory_remove:
            change["accessory_remove"] = ", ".join(accessory_remove)
        wardrobe = appearance_rules.apply_wardrobe_change(wardrobe, change)
        now = time.time()
        for slot in worn_slots:
            tags = str(wardrobe.get(slot) or "").strip()
            if tags:
                name = names.get(slot) or tags
                closet = appearance_rules.closet_add(closet, name, slot, tags, now=now)
        session_schema.set_closet(state, closet)
        session_schema.set_wardrobe(state, wardrobe)
        if mode == "replace":
            session_schema.clear_wardrobe_item_states(state)
            session_schema.clear_public_fallback_outfit(state)
        clear_slots = set(worn_slots)
        clear_slots.update(str(slot or "") for slot in change.get("remove") or [])
        session_schema.clear_wardrobe_item_states(state, clear_slots)
        for slot, value in change.get("states", {}).items():
            if slot in wardrobe:
                session_schema.set_wardrobe_item_state(state, slot, value)
        session_schema.prune_wardrobe_item_states(state, wardrobe)
        rendered = appearance_rules.render_wardrobe(wardrobe)
        session_schema.set_outfit(state, rendered)
        if rendered:
            session_schema.clear_nudity(state)
        if session_id:
            self._ulog(session_id, "WARDROBE", f"结构化批量换装 mode={mode} items={normalized_items} result={rendered[:160]}")
        return True, ""

    async def tool_change_appearance(
        self,
        session_id: str,
        description: str = "",
        mode: str = "merge",
        *,
        items: Any = None,
        clear_all: bool = False,
    ) -> str:
        allow = self._allow_llm_change_appearance(session_id)
        desc = (description or "").strip()
        structured_items = self._coerce_wardrobe_tool_items(items)
        self._ulog(session_id, "WARDROBE", f'模型调用 change_appearance allow={"on" if allow else "off"} mode={mode} items={len(structured_items)} desc="{desc[:100]}"')
        if not allow:
            return "当前会话已关闭模型自主修改外型，dynamic_appearance 未改变。"
        state = self._get_session_state(session_id)
        captured = self._capture_wardrobe_semistable_before_tool(session_id, state)
        if structured_items or clear_all:
            changed, error = self._apply_structured_wardrobe_items(
                state,
                structured_items,
                mode="replace" if mode == "replace" else "merge",
                clear_all=bool(clear_all),
                session_id=session_id,
            )
            if not changed:
                if captured:
                    session_schema.clear_wardrobe_semistable_snapshot(state)
                self._save_session_state(session_id, state)
                return error
            self._save_session_state(session_id, state)
        elif desc:
            await self._apply_wardrobe(session_id, desc, replace=(mode == "replace"))
            state = self._get_session_state(session_id)
        else:
            if captured:
                session_schema.clear_wardrobe_semistable_snapshot(state)
            self._save_session_state(session_id, state)
            return "没有有效的衣物操作，衣橱未改变。"
        snapshot = self._wardrobe_state_snapshot(session_id, state)
        session_schema.set_wardrobe_observed_snapshot(state, snapshot)
        self._save_session_state(session_id, state)
        self._queue_pending_wardrobe_history_message(session_id, snapshot)
        return self._wardrobe_tool_result(snapshot)
