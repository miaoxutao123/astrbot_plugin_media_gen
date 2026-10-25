# 主流图片/视频生成接口调研（2026-10）

插件把各家接口统一成“提交 → 轮询 → 产物”三步。下表是每种协议的要点和插件里对应的适配器。
调研时间 2026-10-25；各家字段以官方文档为准，模型 ID 会频繁更新，配置时以控制台显示为准。

## 协议形态分类

| 形态 | 代表 | 特点 | 插件处理 |
|---|---|---|---|
| 同步返回 | OpenAI `/images/generations`、千问 qwen-image、Gemini 出图、Imagen、即梦反代 | 一个请求等到出结果；图片多为 url 或 base64 | 提交即得产物；不可续跑 |
| 提交 + 按 ID 轮询 | Sora `/videos`、百炼 `/tasks`、方舟 `/contents/generations/tasks`、可灵、Replicate、new-api | 返回任务 ID，状态字段各不相同 | 任务 ID 落盘，可跨重启续跑 |
| 三段式 | MiniMax 海螺 | 查询成功后只给 file_id，还要再取下载地址 | 适配器内部多一步 |
| 长时操作（LRO） | Google Veo `predictLongRunning` | 返回 operation name，`done` 为真后取 uri，下载要带 key | 同上，下载时带鉴权头 |
| 队列 URL | fal.ai | 提交返回 status_url / response_url | 直接用返回的 URL 轮询 |
| 本地工作流 | ComfyUI `/prompt` + `/history` + `/view` | 节点图；需要上传参考图；同卡串行 | 内置工作流 + 自定义占位符 |

## 各家要点

| 接口 | 提交 | 查询 / 结果 | 状态值 | 鉴权 | 适配器 |
|---|---|---|---|---|---|
| OpenAI 图片 | `POST /v1/images/generations`；参考图 `POST /v1/images/edits`（multipart） | 同步，`data[].b64_json`/`url` | — | Bearer | `openai_image` |
| 聊天出图（OpenRouter、new-api 转 Gemini 等） | `POST /v1/chat/completions` | `message.images[]`、content 里的 markdown/data URL | — | Bearer | `openai_image`（ref_mode=chat） |
| OpenAI Sora 2 | `POST /v1/videos`（model、prompt、seconds=4/8/12、size；参考图 input_reference multipart） | `GET /v1/videos/{id}` → `GET /v1/videos/{id}/content` | queued / in_progress / completed / failed | Bearer | `openai_video` |
| 阿里百炼 万相视频 | `POST /api/v1/services/aigc/video-generation/video-synthesis`，必须带 `X-DashScope-Async: enable` | `GET /api/v1/tasks/{id}` → `output.video_url`（24h 有效） | PENDING / RUNNING / SUCCEEDED / FAILED / CANCELED / UNKNOWN | Bearer | `dashscope` |
| 阿里百炼 千问 qwen-image | `POST /api/v1/services/aigc/multimodal-generation/generation`（messages 格式，size 用 `1024*1024`） | 同步，`output.choices[0].message.content[].image` | — | Bearer | `dashscope` |
| 阿里百炼 万相文生图 | `POST /api/v1/services/aigc/text2image/image-synthesis`（异步） | `/tasks/{id}` → `output.results[].url` | 同上 | Bearer | `dashscope` |
| 火山方舟 Seedream | `POST /api/v3/images/generations`（OpenAI 风格，image 字段传参考图） | 同步 `data[].url` | — | Bearer | `volcengine_ark` |
| 火山方舟 Seedance | `POST /api/v3/contents/generations/tasks`（content 数组：text + image_url，role=first_frame/last_frame） | `GET .../tasks/{id}` → `content.video_url`（24h） | queued / running / succeeded / failed / cancelled / expired | Bearer | `volcengine_ark` |
| 可灵 Kling | `POST /v1/videos/text2video`、`/image2video`、`/v1/images/generations` | `GET` 同路径 `/{task_id}` → `data.task_result.videos[].url` | submitted / processing / succeed / failed | AK/SK 签 HS256 JWT（iss=AK，30 分钟） | `kling` |
| MiniMax 海螺 | `POST /v1/video_generation` | `GET /v1/query/video_generation?task_id=` → file_id → `GET /v1/files/retrieve?file_id=` → `file.download_url` | Preparing / Queueing / Processing / Success / Fail | Bearer；国内 api.minimaxi.com，国际 api.minimax.io | `minimax` |
| Google Gemini 出图 | `POST /v1beta/models/{m}:generateContent`，responseModalities 含 IMAGE | 同步 `candidates[].content.parts[].inlineData` | — | x-goog-api-key | `gemini` |
| Google Veo | `POST /v1beta/models/{m}:predictLongRunning` | `GET /v1beta/{operation}` → `response.generateVideoResponse.generatedSamples[].video.uri`（下载带 key、跟随重定向） | done: true/false | x-goog-api-key | `gemini` |
| Replicate | `POST /v1/models/{owner}/{name}/predictions` 或 `/v1/predictions`（version） | `GET /v1/predictions/{id}` → `output` | starting / processing / succeeded / failed / canceled | Bearer | `replicate` |
| fal.ai | `POST https://queue.fal.run/{model}` | 返回的 status_url / response_url | IN_QUEUE / IN_PROGRESS / COMPLETED | `Key xxx` | `fal` |
| 硅基流动 图片 | `POST /v1/images/generations` | 同步，返回 `images[].url`（不是 data） | — | Bearer | `openai_image`（兼容 images 字段） |
| 硅基流动 视频 | `POST /v1/video/submit` → requestId | `POST /v1/video/status` → `results.videos[].url`（1h 有效） | Succeed / InQueue / InProgress / Failed | Bearer | `generic` 预设 `siliconflow_video` |
| new-api 统一视频 | `POST /v1/video/generations` → task_id | `GET /v1/video/generations/{id}` | queued / in_progress / succeeded / failed | Bearer | `generic` 预设 `newapi_video` |
| new-api Sora 格式 | `/v1/videos` 一套 | 同 Sora | 同 Sora | Bearer | `openai_video` |
| 即梦反代（iptag/jimeng-api，本机 :5100） | `POST /v1/images/generations`、`/v1/videos/generations`（同步阻塞） | `data[].url` | — | Bearer（即梦 sessionid） | `openai_image` / `generic` 预设 `jimeng_video` |
| ComfyUI | `POST /prompt`（API 格式节点图）；参考图 `POST /upload/image` | `GET /history/{id}`；不在 history 时查 `/queue`；`GET /view` 下载 | 无显式状态，看 history/queue | 无（可加反代 Bearer） | `comfyui` |

本机 new-api（v1.0.0-rc.46）二进制里能看到 `/v1/videos`、`/v1/video/generations`、`/v1/videos/text2video`（可灵原生路径）等路由，所以中转站场景三种格式都可能遇到，插件都能接。

## 设计上的取舍

- **防重复扣费**：提交接口不是幂等的。只有“请求肯定没被受理”（连不上、429、503）才在原接口重试；500/502/504/读超时可能已经排上队，直接换回退模型，不在原接口重试。
- **URL 时效**：百炼、方舟 24 小时，硅基流动 1 小时，可灵/海螺也会过期。产物一律在任务完成时下载到本地，聊天推送、发布都用本地文件。
- **参考图**：大多数接口同时接受公网 URL 和 data URL。QQ 聊天图片的链接有时效和防盗链，所以聊天里的图统一转成文件内容（data URL / base64 / multipart / 上传）。可灵要求纯 base64（不带 `data:` 前缀），单独处理。
- **时长档位**：Sora 只收 4/8/12 秒，可灵 5/10 秒，海螺 6/10 秒，H3 是 17k+5 帧网格。适配器会把请求时长贴到最近的档位。
- **免费连通性检查（probe）**：查一个不存在的任务、列模型、查 system_stats，只看鉴权和可达性，不产生费用。

## 参考来源

- OpenAI Video generation guide: https://developers.openai.com/api/docs/guides/video-generation
- 阿里云百炼 万相文生视频 API 参考: https://help.aliyun.com/zh/model-studio/legacy-wan-text-to-video-api-reference
- 阿里云百炼 千问图像生成与编辑 API 参考: https://www.alibabacloud.com/help/zh/model-studio/qwen-image-generation-and-editing-api-reference
- Gemini API Veo: https://ai.google.dev/gemini-api/docs/veo
- MiniMax 视频下载接口: https://platform.minimax.io/docs/api-reference/video-generation-download.md
- 硅基流动视频状态接口: https://docs.siliconflow.cn/cn/api-reference/videos/get_videos_status
- 方舟 Seedance 第三方整理（官方文档需登录）: https://docs.apiyi.com/en/api-capabilities/seedance2/video-generation
- 可灵任务状态（第三方整理，官方文档页需 JS 渲染）: https://tessl.io/registry/skills/github/jeremylongshore/tons-of-skills-marketplace/klingai-job-monitoring
- LiteLLM 的 OpenAI Videos 代理说明: https://docs.litellm.ai/docs/providers/openai/videos
