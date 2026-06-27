# Anima Turbo 提示词规范

## 核心规则

1. **tags 字段使用英文自然语言**：不要用逗号分隔的 Danbooru 标签，直接用完整的英文句子描述场景。
2. **自然语言要充分**：tags 至少写 3-5 句话，描述清楚角色外观、动作、构图、氛围。
3. **安全标签必须明确**：`safe / sensitive / nsfw / explicit` 必须在 quality_meta_year_safe 中明确出现。
4. **画师标签必须以 @ 开头**（如 `@wlop`），仅在用户明确要求时添加，否则留空。
5. **默认不追求写实**：除非用户明确要求。

## 可调参数

- **cfg**：LoRA 强度，范围 0.7-1，默认 1。更小的值有更多多样性和随机性，更大的值更稳定。
- **steps**：采样步数，范围 8-12，默认 10。更多步数质量更好但更慢。

## appearance 字段

appearance 使用逗号分隔的 Danbooru 标签描述角色外观（发型发色、瞳色、服装、饰品等），与 base 模式一致。多角色时所有角色外观写在一起。

## tags 字段写法要点

tags 必须包含：**角色外观引用**、**动作/姿势**、**表情**、**构图/视角**，建议补充氛围和光影。

**✅ 好的写法：**
> A girl with long flowing silver hair and bright blue eyes is standing in a vast sunflower field, wearing a white summer dress. She is smiling gently at the viewer. The composition is a full body shot from a slightly low angle. Warm dreamy atmosphere with soft golden light.

## 多角色场景

- **appearance 字段**放所有角色的外观标签（逗号分隔）
- **tags 中用自然语言**引用外观特征区分角色，如 "The girl with crimson hair and wolf ears..."
- **使用共处式描述**，不要用位置分割词（❌ "On the left... On the right..."）
- **character 字段**多个角色名用逗号分隔

## tags 末尾追加

tags 字段末尾必须加上 `no text, no logo, no ui`，用于替代负面提示词的作用。当分级为 nsfw 或 explicit 时，额外追加 `, no mosaic, uncensored`。

## 宽高比

支持 5 种比例（默认 1:1）：`16:9` `3:2` `1:1` `2:3` `9:16`
