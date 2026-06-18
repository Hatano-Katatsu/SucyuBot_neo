from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Any

import aiohttp

from .appearance import infer_gender_from_prefix, inject_appearance
from .defaults import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


PHONE_TERMS = ("phone", "smartphone", "cellphone", "mobile phone", "手机")
MIRROR_TERMS = ("mirror", "mirror reflection", "mirror selfie", "镜子", "对镜")


def _append_negatives(negative: str, *terms: str) -> str:
    seen = {item.strip().lower() for item in negative.split(",") if item.strip()}
    additions = []
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen:
            additions.append(term.strip())
            seen.add(key)
    if additions:
        negative = f"{negative}, {', '.join(additions)}" if negative else ", ".join(additions)
    return negative


def _remove_negatives(negative: str, *terms: str) -> str:
    banned = {term.strip().lower() for term in terms if term.strip()}
    kept = []
    for item in [part.strip() for part in negative.split(",") if part.strip()]:
        if item.lower() not in banned:
            kept.append(item)
    return ", ".join(kept)


def _infer_prompt_view(scene_desc: str) -> str:
    text = scene_desc.strip().lower()
    if "mirror reflection" in text or "mirror selfie" in text:
        return "mirror"
    if text.startswith("a front-camera selfie") or text.startswith("a selfie of"):
        return "selfie"
    if text.startswith("first-person pov"):
        return "pov"
    return ""


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _strip_non_mirror_camera_artifacts(scene_desc: str) -> str:
    text = scene_desc
    phrase_patterns = [
        r"\b(?:while\s+)?(?:the\s+)?(?:other|another|one)\s+(?:hand\s+)?(?:is\s+)?(?:idly\s+|casually\s+)?(?:holds?|holding|grips?|gripping|checks?|checking|scrolls?\s+through|scrolling\s+through|plays?\s+with|using)\s+(?:a\s+|an\s+|one\s+|her\s+|his\s+|the\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:one|another|the other)\s+hand\s+(?:is\s+)?(?:on|near|around)\s+(?:a\s+|one\s+|her\s+|his\s+|the\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\bholding\s+(?:a\s+|one\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:smartphone|phone|cellphone|mobile phone)\s+in\s+(?:her\s+)?hand\b",
        r"\bvisible\s+(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:smartphone|phone|cellphone|mobile phone)\s+screen\b",
        r"\bwith\s+(?:a\s+|one\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\bmirror\s+selfie\b",
        r"\bmirror\s+reflection\b",
        r"\bin\s+(?:a\s+)?mirror\b",
    ]
    for pattern in phrase_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:smartphone|phone|cellphone|mobile phone)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmirror\b", "", text, flags=re.IGNORECASE)
    text = text.replace("手机", "").replace("镜子", "").replace("对镜", "")
    text = re.sub(
        r"\b(?:while\s+)?(?:the\s+)?(?:other|another|one)\s+(?:hand\s+)?(?:is\s+)?(?:idly\s+|casually\s+)?(?:holds?|holding|grips?|gripping|checks?|checking|scrolls?\s+through|scrolling\s+through|plays?\s+with|using)\s+(?:a|an|one|her|his|the)?\s*(?=[,.;]|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(?:with|holding|holds?|using|uses?|gripping|grips?)\s+(?:a|an|one|her|his|the)\s*(?=[,.;]|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*,+", ", ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"(^|,\s*)(?:and\s*)?(?=,|$)", "", text, flags=re.IGNORECASE)
    return text.strip(" ,")


SECOND_PERSON_VISUAL_SUBJECT_RE = re.compile(
    r"^(?P<prefix>\s*(?:(?:first-person\s+pov|pov)[^,]*,\s*)?"
    r"(?:looking\s+at\s+a\s+(?:woman|girl|man|boy),\s*)?"
    r"(?:solo,\s*)?)(?:you|user)\b",
    re.IGNORECASE,
)

SECOND_PERSON_SUBJECT_ACTION_RE = re.compile(
    r"\b(?:you|user)\s+(?=(?:are|lounge|sit|stand|lie|lean|kneel|crouch|wear|curl|rest|pose|look|wait|hold|twirl|play|smile|sleep)\b)",
    re.IGNORECASE,
)


def _normalize_second_person_visual_subject(scene_desc: str) -> str:
    text = (scene_desc or "").strip()
    if not text:
        return text

    text = SECOND_PERSON_VISUAL_SUBJECT_RE.sub(lambda m: f"{m.group('prefix')}The character", text, count=1)
    text = SECOND_PERSON_SUBJECT_ACTION_RE.sub("the character ", text)
    text = re.sub(
        r"\byour\s+(hair|face|body|shoulder|shoulders|chest|waist|leg|legs|shirt|dress|clothes|outfit|hand|hands|arm|arms|eyes|mouth)\b",
        r"the character's \1",
        text,
        flags=re.IGNORECASE,
    )

    verb_fixes = {
        "are": "is",
        "lounge": "lounges",
        "sit": "sits",
        "stand": "stands",
        "lie": "lies",
        "lean": "leans",
        "kneel": "kneels",
        "crouch": "crouches",
        "wear": "wears",
        "curl": "curls",
        "rest": "rests",
        "pose": "poses",
        "look": "looks",
        "wait": "waits",
        "hold": "holds",
        "twirl": "twirls",
        "play": "plays",
        "smile": "smiles",
        "sleep": "sleeps",
    }
    for base, fixed in verb_fixes.items():
        text = re.sub(rf"\b(The character|the character)\s+{base}\b", rf"\1 {fixed}", text, flags=re.IGNORECASE)
    return text


def _visual_character_identity(state: dict[str, Any]) -> tuple[str, str]:
    character = (state.get("custom_character") or "").strip()
    series = (state.get("custom_series") or "").strip()
    if character and series:
        return character, series
    return "", ""


def _strip_non_visual_role_names(service: Any, state: dict[str, Any], session_id: str, scene_desc: str) -> str:
    character, series = _visual_character_identity(state)
    if character and series:
        return scene_desc

    names = {
        (state.get("custom_character") or "").strip(),
        (service.config.get("bot_name") or "").strip(),
    }
    if session_id:
        names.add((service._get_session_cfg(session_id, "bot_name", "") or "").strip())
    names.discard("")

    aliases: set[str] = set()
    default_role_name = "\u857e\u4f0a"
    if default_role_name in names:
        aliases.update({"Rey", "Rei", "Lei"})

    text = scene_desc
    for name in sorted(names, key=len, reverse=True):
        if not name:
            continue
        if name.isascii():
            aliases.add(name)
        else:
            text = text.replace(f"{name}\u7684", "\u89d2\u8272\u7684")
            text = text.replace(name, "\u89d2\u8272")

    for alias in sorted(aliases, key=len, reverse=True):
        escaped = re.escape(alias)
        text = re.sub(rf"\b{escaped}'s\b", "the character's", text, flags=re.IGNORECASE)
        text = re.sub(rf"\b{escaped}\b", "the character", text, flags=re.IGNORECASE)
    return text


def view_opener(view: str, gender: str = "girl") -> str:
    subj = "man" if gender == "boy" else "woman"
    count = "1boy" if gender == "boy" else "1girl"
    return {
        "selfie": f"A front-camera selfie of a {subj}, solo, upper body framing, looking at viewer",
        "mirror": f"A mirror reflection of a {subj}, solo, single reflected body, only mirror reflection is visible, no foreground person, holding one smartphone with one hand, looking at viewer through the mirror",
        "pov": f"First-person POV, looking at a {subj}, solo, eye contact with the viewer",
        "third": f"{count}, solo",
    }.get(view, "")


def build_prompt(service: Any, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[str, str]:
    state = service._get_session_state(session_id) if session_id else {}
    scene_desc = _normalize_second_person_visual_subject(scene_desc)
    scene_desc = _strip_non_visual_role_names(service, state, session_id, scene_desc)
    scene_lower = scene_desc.lower()
    sex_keywords = ["sex", "make love", "penetration", "vaginal", "missionary", "doggystyle", "cowgirl", "naked together"]
    is_sex_scene = any(k in scene_lower for k in sex_keywords)
    is_ntr_scene = is_ntr or any(k in scene_lower for k in ["ntr", "netorare", "cuckold", "split screen"])

    purity = service._get_purity(session_id) if session_id else 1
    safety = service._get_effective_safety(session_id) if session_id else {"tag": None, "level": 1}
    current_style = service._get_current_style(session_id)
    if service._is_character_set(session_id):
        # 兜底：角色态但身体特征被清空（半重置残留）时回退全局 positive_prefix。
        char = state.get("custom_positive_prefix", "") or service.config.get("positive_prefix", "")
    else:
        char = service._get_session_cfg(session_id, "positive_prefix", "")
    char = inject_appearance(service, char, session_id)

    quality = "masterpiece, best quality, absurdres, score_9, score_8, anime coloring, clean lineart, soft cel shading, detailed illustration"
    if safety.get("tag"):
        quality += f", {safety['tag']}"
    male = infer_gender_from_prefix(char) == "boy"
    count = "1boy, solo" if male else "1girl, solo"
    if is_ntr:
        count = re.sub(r"\bsolo\b,?\s*", "", count).strip(", ")
    character, series = _visual_character_identity(state)
    artist = current_style if current_style.startswith("@") else ""
    style_general = current_style if current_style and not current_style.startswith("@") else ""

    neg = service.config.get("negative_prompt", DEFAULT_CONFIG["negative_prompt"])
    neg = _append_negatives(
        neg,
        "extra hands", "three hands", "three arms", "extra arms", "duplicate hands", "duplicate arms",
        "malformed hands", "poorly drawn hands", "extra digits", "duplicated limbs",
    )
    if state.get("custom_positive_prefix"):
        strip = {"clothes", "clothing"}
        if male:
            strip |= {"male", "boy", "man", "1boy"}
        neg = ", ".join(t.strip() for t in neg.split(",") if t.strip() and t.strip().lower() not in strip)
    if "2girls" not in neg.lower():
        neg += ", 2girls, multiple girls, extra girls"
    if is_ntr:
        neg = ", ".join(t for t in [x.strip() for x in neg.split(",")] if t.lower() not in {"male", "boy", "man", "1boy"})
    elif not male and "male" not in neg.lower():
        neg += ", male, boy, man"

    prompt_view = _infer_prompt_view(scene_desc)
    if is_sex_scene and not is_ntr_scene:
        for tag in ["selfie", "pov", "holding phone", "arm extended", "mirror selfie", "phone"]:
            scene_desc = re.sub(r"\b" + re.escape(tag) + r"\b", "", scene_desc, flags=re.IGNORECASE)
        scene_desc += ", third-person perspective, medium shot, side view"
        neg = _append_negatives(neg, "selfie", "pov", "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "arm extended")
    else:
        has_phone = _contains_any(scene_desc, PHONE_TERMS)
        has_mirror = _contains_any(scene_desc, MIRROR_TERMS)
        if prompt_view == "mirror" or ("mirror selfie" in scene_desc.lower() and has_phone):
            neg = _remove_negatives(neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "visible phone", "phone in hand")
            scene_desc += ", mirror reflection, single reflected body, only mirror reflection is visible, no foreground person"
            neg = _append_negatives(neg, "foreground person", "person outside mirror", "second body", "duplicate body", "multiple reflections", "two phones", "multiple phones")
        elif prompt_view in {"selfie", "pov"}:
            scene_desc = _strip_non_mirror_camera_artifacts(scene_desc)
            neg = _append_negatives(
                neg,
                "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "visible phone",
                "phone in hand", "mirror", "mirror reflection", "mirror selfie",
            )
        elif not has_phone and not has_mirror and not is_ntr_scene:
            neg = _append_negatives(neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "visible phone")

    effective = safety.get("level", purity)
    if purity <= 2:
        neg = ", ".join(t for t in [x.strip() for x in neg.split(",")] if t.lower() not in {"child", "loli", "censor bar", "mosaic", "pixelated"})
    elif purity <= 7:
        if effective > 5:
            neg += ", nsfw, explicit, naked, nude, sex"
    elif purity <= 9:
        neg += ", nsfw, explicit, naked, nude, sex, suggestive, lewd, ecchi, revealing clothes"
    else:
        neg += ", nsfw, explicit, naked, nude, sex, suggestive, lewd, ecchi, cleavage, bikini, lingerie, underwear"

    general = ", ".join(p for p in [char, style_general, scene_desc] if p)
    modules = [quality, count, character, series, artist, general]
    seen, deduped = set(), []
    for module in modules:
        kept = []
        for tag in [t.strip() for t in module.split(",") if t.strip()]:
            if tag.lower() not in seen:
                kept.append(tag)
                seen.add(tag.lower())
        if kept:
            deduped.append(", ".join(kept))
    return ", ".join(deduped), neg


def build_workflow(service: Any, positive: str, negative: str, seed: int) -> dict[str, Any]:
    wf_file = service.config.get("comfyui_workflow_file", "")
    if wf_file:
        try:
            raw = Path(wf_file).read_text(encoding="utf-8")
            wf = json.loads(raw)
            replacements = {
                "{{positive}}": positive,
                "{{negative}}": negative,
                "{{seed}}": str(seed),
                "{{width}}": str(int(service.config.get("width", "1024"))),
                "{{height}}": str(int(service.config.get("height", "1024"))),
                "{{steps}}": str(int(service.config.get("steps", "30"))),
                "{{cfg}}": str(float(service.config.get("cfg", "4"))),
                "{{sampler}}": service.config.get("sampler", "er_sde"),
                "{{scheduler}}": service.config.get("scheduler", "simple"),
            }
            wf_text = json.dumps(wf)
            for old, new in replacements.items():
                wf_text = wf_text.replace(old, new)
            return json.loads(wf_text)
        except Exception as exc:
            logger.error("自定义工作流加载失败，回退内置工作流: %s", exc)
    return build_anima_workflow(service, positive, negative, seed)


def build_anima_workflow(service: Any, positive: str, negative: str, seed: int) -> dict[str, Any]:
    w = int(service.config.get("width", "1024"))
    h = int(service.config.get("height", "1024"))
    steps = int(service.config.get("steps", "30"))
    cfg = float(service.config.get("cfg", "4"))
    sampler = service.config.get("sampler", "er_sde")
    scheduler = service.config.get("scheduler", "simple")
    unet = service.config.get("unet_model", "anima-preview3-base.safetensors")
    clip = service.config.get("clip_model", "qwen_3_06b_base.safetensors")
    vae = service.config.get("vae_model", "qwen_image_vae.safetensors")
    wf = {
        "46": {"inputs": {"filename_prefix": "Anima", "images": ["63", 0]}, "class_type": "SaveImage"},
        "61": {"inputs": {"clip_name": clip, "type": "stable_diffusion", "device": "default"}, "class_type": "CLIPLoader"},
        "62": {"inputs": {"vae_name": vae}, "class_type": "VAELoader"},
        "63": {"inputs": {"samples": ["66", 0], "vae": ["62", 0]}, "class_type": "VAEDecode"},
        "64": {"inputs": {"width": w, "height": h, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "68": {"inputs": {"unet_name": unet, "weight_dtype": "default"}, "class_type": "UNETLoader"},
    }
    model_src, clip_src = ["68", 0], ["61", 0]
    if service.config.get("turbo_mode", False):
        strength = float(service.config.get("turbo_strength", "0.6"))
        wf["69"] = {"inputs": {"model": ["68", 0], "clip": ["61", 0], "lora_name": service.config.get("turbo_lora_model", "anima-turbo-lora-v0.2.safetensors"), "strength_model": strength, "strength_clip": strength}, "class_type": "LoraLoader"}
        model_src, clip_src = ["69", 0], ["69", 1]
    wf["65"] = {"inputs": {"text": negative, "clip": clip_src}, "class_type": "CLIPTextEncode"}
    wf["67"] = {"inputs": {"text": positive, "clip": clip_src}, "class_type": "CLIPTextEncode"}
    wf["66"] = {"inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler, "scheduler": scheduler, "denoise": 1, "model": model_src, "positive": ["67", 0], "negative": ["65", 0], "latent_image": ["64", 0]}, "class_type": "KSampler"}
    return wf


def ensure_comfy_session(service: Any):
    if service.comfy_session is None or service.comfy_session.closed:
        service.comfy_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600), trust_env=True)


async def do_generate(service: Any, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[bool, list[bytes], str]:
    async with service._gen_lock:
        service._generating = True
        try:
            return await do_generate_locked(service, scene_desc, is_ntr, session_id)
        finally:
            service._generating = False


async def do_generate_locked(service: Any, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[bool, list[bytes], str]:
    ensure_comfy_session(service)
    positive, negative = build_prompt(service, scene_desc, is_ntr, session_id)
    seed = random.randint(0, 2**63 - 1)
    if session_id and hasattr(service, "_ulog"):
        service._ulog(
            session_id,
            "PROMPT",
            f"seed={seed} scene={scene_desc} positive={positive} negative={negative}",
        )
    workflow = build_workflow(service, positive, negative, seed)
    try:
        async with service.comfy_session.post(f"{service.comfyui_url}/prompt", json={"prompt": workflow}) as resp:
            data = await resp.json()
        if "prompt_id" not in data:
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(data)
            return False, [], f"ComfyUI 提交失败: {msg}"
        prompt_id = data["prompt_id"]
        for _ in range(int(600 / 1.5)):
            await asyncio.sleep(1.5)
            async with service.comfy_session.get(f"{service.comfyui_url}/history/{prompt_id}") as resp:
                history = await resp.json()
            if prompt_id not in history:
                continue
            outputs = history[prompt_id].get("outputs", {})
            images = outputs.get("46", {}).get("images", [])
            if not images:
                continue
            result = []
            for img in images:
                params = {"filename": img["filename"]}
                if img.get("subfolder"):
                    params["subfolder"] = img["subfolder"]
                async with service.comfy_session.get(f"{service.comfyui_url}/view", params=params) as resp:
                    if resp.status == 200:
                        result.append(await resp.read())
            return True, result, ""
        return False, [], "超时"
    except Exception as exc:
        return False, [], f"异常: {exc}"
