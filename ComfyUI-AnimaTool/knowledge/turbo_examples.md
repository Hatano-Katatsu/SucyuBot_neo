# Turbo Prompt JSON 示例

> Turbo 模式：tags 使用英文自然语言，appearance 使用逗号分隔的 Danbooru 标签。
> tags 末尾必须加上 `no text, no logo, no ui`；nsfw/explicit 分级额外追加 `, no mosaic, uncensored`。

## 1) 单角色竖构图

```json
{
  "aspect_ratio": "2:3",
  "quality_meta_year_safe": "masterpiece, best quality, highres, newest, year 2025, sensitive",
  "count": "1girl",
  "character": "shiroko (blue archive)",
  "series": "blue archive",
  "appearance": "medium-length silver hair, blue eyes, wolf ears, white sailor uniform, blue scarf",
  "tags": "Shiroko is sitting on a classroom desk, looking at the viewer with a gentle smile. The composition is an upper body shot from the front. Soft warm sunlight streams through the window, creating a cozy atmosphere. no text, no logo, no ui"
}
```

## 2) 双角色，已知角色

```json
{
  "aspect_ratio": "1:1",
  "quality_meta_year_safe": "masterpiece, best quality, highres, newest, year 2025, sensitive",
  "count": "2girls",
  "character": "shiroko (blue archive), serika (blue archive)",
  "series": "blue archive",
  "appearance": "medium-length silver hair, blue eyes, wolf ears, white sailor uniform, blue scarf, long black hair, low ponytail, red eyes, cat ears, black sailor uniform, blue ribbon",
  "tags": "Two girls are sitting together on a classroom bench, looking at the viewer with friendly smiles. The girl with silver hair and wolf ears is Shiroko, and the girl with black hair in a ponytail and cat ears is Serika. Soft warm sunlight creates a cozy atmosphere. no text, no logo, no ui"
}
```

## 3) 双角色原创，横构图

```json
{
  "aspect_ratio": "3:2",
  "quality_meta_year_safe": "masterpiece, best quality, highres, newest, year 2025, safe",
  "count": "2girls",
  "appearance": "short crimson red hair, amber eyes, wolf ears, dark leather armor, fur trim, heavy boots, long silver-white hair, emerald green eyes, pointed ears, blue and white robe, glowing runes, pointed shoes",
  "artist": "@makihitsuji",
  "tags": "Two girls stand side by side in an ancient forest clearing. The beastgirl warrior with crimson hair and wolf ears wields a battle axe with a fierce grin. The elven mage with silver-white hair and pointed ears holds a crystal staff with a serene smile. Dappled sunlight filters through the canopy with floating golden particles. no text, no logo, no ui"
}
```
