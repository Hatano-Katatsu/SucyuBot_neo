# Prompt JSON 示例

> 这些示例可直接用于工具调用（HTTP/CLI/Function Calling）。

## 1) 竖构图 9:16，人物半身

```json
{
  "aspect_ratio": "9:16",
  "quality_meta_year_safe": "masterpiece, best quality, score_7, highres, newest, year 2025, sensitive",
  "count": "1girl",
  "character": "shiroko (blue archive)",
  "series": "blue archive",
  "appearance": "long silver hair, wolf ears, blue eyes, school uniform, necktie",
  "artist": "",
  "style": "anime illustration, vibrant colors",
  "tags": "upper body, looking at viewer, smile",
  "environment": "school, cherry blossoms, sunlight",
  "neg": "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, bad anatomy, bad hands, bad feet, missing fingers, extra fingers, text, watermark, logo, nsfw, explicit"
}
```

## 2) 横构图 16:10，全身动态

```json
{
  "aspect_ratio": "16:10",
  "quality_meta_year_safe": "masterpiece, best quality, score_7, highres, newest, year 2025, sensitive",
  "count": "1girl",
  "character": "",
  "series": "",
  "appearance": "short hair, brown hair, red eyes, school uniform",
  "artist": "",
  "style": "anime illustration, vibrant colors",
  "tags": "full body, dynamic pose, running, wind, motion blur",
  "environment": "sunset, backlight, dust particles",
  "neg": "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, bad anatomy, bad hands, bad feet, extra fingers, missing fingers, text, watermark, logo, nsfw, explicit"
}
```

## 3) 超宽 21:9，风景+小人

```json
{
  "aspect_ratio": "21:9",
  "quality_meta_year_safe": "masterpiece, best quality, score_7, highres, newest, year 2025, sensitive",
  "count": "1girl",
  "character": "",
  "series": "",
  "appearance": "long hair, white dress",
  "artist": "@guweiz",
  "style": "painterly, atmospheric perspective",
  "tags": "wide shot, small figure, landscape, mountains, river, clouds",
  "environment": "golden hour, volumetric light, haze",
  "neg": "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, bad anatomy, bad hands, bad feet, extra fingers, missing fingers, text, watermark, logo, nsfw, explicit"
}
```

## 4) 双角色 1:1，Blue Archive 白子与芹香

```json
{
  "aspect_ratio": "1:1",
  "quality_meta_year_safe": "masterpiece, best quality, score_7, highres, newest, year 2025, sensitive",
  "count": "2girls",
  "character": "shiroko (blue archive), serika (blue archive)",
  "series": "blue archive",
  "appearance": "medium silver hair, wolf ears, blue eyes, white sailor uniform, blue scarf, long black hair, cat ears, red eyes, black sailor uniform, blue ribbon",
  "artist": "",
  "style": "anime illustration, soft lighting, detailed",
  "tags": "looking at viewer, smile, friends, leaning on each other",
  "nltags": "Shiroko has silver hair and wolf ears, wearing a white sailor uniform. Serika has long black hair and cat ears, wearing a black sailor uniform.",
  "environment": "classroom window, sunset, warm light",
  "neg": "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, bad anatomy, bad hands, bad feet, extra fingers, missing fingers, text, watermark, logo, nsfw, explicit"
}
```

## 5) 三角色横构图 16:9，Vocaloid 初音未来与镜音双子

```json
{
  "aspect_ratio": "16:9",
  "quality_meta_year_safe": "masterpiece, best quality, score_7, highres, newest, year 2025, sensitive",
  "count": "3girls",
  "character": "hatsune miku (vocaloid), kagamine rin (vocaloid), kagamine len (vocaloid)",
  "series": "vocaloid",
  "appearance": "long twintails, aqua hair, aqua eyes, teal sleeveless dress, black thighhighs, headset microphone, short blonde hair, blue eyes, orange sleeveless top, white shorts, white hair bow, headset microphone, short blonde hair, blue eyes, white button-up shirt, black shorts, yellow necktie, headset microphone",
  "artist": "",
  "style": "anime illustration, soft shading",
  "tags": "group shot, looking at viewer, smile, singing, concert",
  "nltags": "Hatsune Miku has long twintails and wears a teal dress. Kagamine Rin has short blonde hair and an orange top. Kagamine Len has short blonde hair and a white shirt.",
  "environment": "stage lights, colorful background",
  "neg": "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, bad anatomy, bad hands, bad feet, extra fingers, missing fingers, text, watermark, logo, nsfw, explicit"
}
```

## 6) 原创角色纯自然语言 1:1，兽耳娘战士与精灵魔法师

```json
{
  "aspect_ratio": "1:1",
  "quality_meta_year_safe": "masterpiece, best quality, score_7, highres, newest, year 2025, safe",
  "count": "2girls",
  "character": "",
  "series": "",
  "appearance": "",
  "artist": "@makihitsuji",
  "style": "fantasy illustration, detailed, soft lighting",
  "tags": "full body, group shot, standing side by side, magic effects, forest background",
  "nltags": "Two girls stand side by side in an ancient forest. The beastgirl warrior has short crimson red hair, amber golden eyes, and large wolf ears on top of her head. She wears dark leather armor with fur trim and wields a large battle axe. Beside her, the elven mage has long silver-white hair flowing down her back, bright emerald green eyes, and pointed ears. She wears elegant blue and white robes with glowing magical runes and holds a crystal staff.",
  "environment": "ancient forest, dappled sunlight, magical particles, mist",
  "neg": "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, bad anatomy, bad hands, bad feet, extra fingers, missing fingers, text, watermark, logo, nsfw, explicit"
}
```

> 说明：若想只指定比例，可用 `aspect_ratio`（如 `16:10`），并省略 width/height，由工具侧推算。
