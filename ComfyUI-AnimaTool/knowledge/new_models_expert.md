# 提示词字段写法

## 字段说明

| 字段 | 写法 |
|------|------|
| `quality_meta_year_safe` | `masterpiece, best quality, <safe/sensitive/nsfw/explicit>`。安全等级必须明确。 |
| `count` | 人数标签，如 `1girl`、`2girls`、`1boy`、`1other`。 |
| `character` | 角色名，可带作品信息，如 `hatsune miku` 或 `yunli (honkai star rail)`。多个角色用逗号分隔。 |
| `series` | 作品/系列名，如 `vocaloid`、`honkai star rail`。 |
| `artist` | 画师名，需要时以 `@` 开头，如 `@wlop`。 |
| `appearance` | 逗号分隔的 Danbooru 标签，描述角色外观：发型发色、瞳色、服装、饰品等。多角色时写在一起。 |
| `tags` | 英文自然语言，完整句子描述场景。至少 3-5 句话，包含角色外观引用、动作/姿势、表情、构图/视角，可补充氛围和光影。 |
| `neg` | 负面提示词。safe/sensitive 时追加 `nsfw, explicit`；nsfw/explicit 时追加 `safe, sensitive, censored, mosaic, no mosaic, uncensored`。同时包含 `bad anatomy, bad hands, bad feet, extra fingers, missing fingers, text, watermark, logo`。 |
| `aspect_ratio` | 可选 `16:9`、`3:2`、`1:1`、`2:3`、`9:16`，默认 `1:1`。 |
| `steps` / `cfg` | 由系统根据所选模型固定，无需填写。 |

## `tags` 示例

```
A girl with long flowing silver hair and bright blue eyes is standing in a vast sunflower field, wearing a white summer dress. She is smiling gently at the viewer. The composition is a full body shot from a slightly low angle. Warm dreamy atmosphere with soft golden light.
```

## 多角色场景

- `appearance` 中写所有角色外观标签。
- `tags` 中用自然语言引用外观特征区分角色，如 "The girl with crimson hair and wolf ears..."
- 使用共处式描述，避免位置分割词（"On the left... On the right..."）。

## 模型能力限制

模型不具备生成文字、LOGO、UI 界面、对话框、气泡的能力。`tags` 中不要出现 `text`、`sign`、`letter`、`caption`、`ui`、`interface`、`hud`、`menu`、`button`、`dialog`、`speech bubble`、`thought bubble`、`logo` 等描述。

当分级为 `nsfw` 或 `explicit` 时，`tags` 末尾追加 `no mosaic, uncensored`。
