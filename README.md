# astrbot_plugin_media_gen — AI 图片/视频生成

给 AstrBot 用的生成插件：把配置好的图片/视频模型通过 LLM 工具交给 AI 调用，支持自然语言配置、定时自动化，
并能把成品交给账号运维插件（`astrbot_plugin_videos_analysis`）走发布审批。

## 能做什么

- **多接口兼容**：OpenAI 图片（gpt-image / DALL·E / 各类中转）、聊天出图、OpenAI Sora、阿里百炼（万相视频、千问 qwen-image、万相文生图）、
  火山方舟（Seedream / Seedance）、可灵、MiniMax 海螺、Google Gemini / Imagen / Veo、Replicate、fal.ai、
  ComfyUI 本地工作流（内置 Qwen-Image 2.1 出图、MiniMax H3 文/图生视频），以及“通用 HTTP 模板”
  （内置硅基流动视频、new-api 统一视频、即梦反代预设）。协议调研见 [docs/PROTOCOLS.md](docs/PROTOCOLS.md)。
- **4 个 LLM 工具**（描述是静态的，结果很短，不会把对话历史撑大）：

  | 工具 | 谁能用 | 作用 |
  |---|---|---|
  | `media_generate` | 所有人（视频默认只给管理员） | 生成图片/视频，完成后自动发到聊天；支持用户发的图、上一张生成的图作参考/首尾帧 |
  | `media_task` | 所有人 | 查看可用模型、任务进度、取消、重试、重新发送、提交发布 |
  | `media_settings` | 管理员 | 自然语言配置：增删改模型、默认模型、全局设置、免费连通性检查、真实测试、撤销 |
  | `media_automation` | 管理员 | 定时自动生成：AI 构思标题/话题/画面 → 生成 → 推送 → 提交发布审批 |

- **命令**：`/画 <描述>`、`/视频 <描述>`（可附图）、`/mg`、`/mg_jobs`、`/mg_cancel <任务号>`、`/mg_models`、
  `/mg_probe [模型]`、`/mg_auto [list|run 名称]`。

## 对话示例

> 帮我加一个硅基流动的出图模型，key 是 sk-xxxx，超时 3 分钟，失败了就用本地出图
> → `media_settings(add, 硅基出图, preset=siliconflow_image, values={"api_key":…, "超时":"3分钟", "备用":"本地出图"})`

> 画一张赛博朋克风格的重庆夜景，16:9
> → `media_generate(image, …, size="16:9")`，图片直接发到聊天

> 把刚才那张图做成 5 秒视频，镜头慢慢跟着她往前走
> → `media_generate(video, …, images="last", duration=5)`，立即返回任务号，几分钟后自动推送

> 每天早上 9 点做一组治愈系橘猫的竖屏图文，合成视频发抖音
> → `media_automation(add, 每日猫咪, {"schedule":"09:00","kind":"slides","topic":…,"count":3,"platform":"douyin","auto_publish":true})`

## 配置

在 WebUI 的插件配置里，“生成模型”是一个模板列表：点“添加”选接口类型，填名称、地址、Key、模型 ID 即可。
也可以全部在聊天里让机器人配置（管理员），字段支持中文别名（地址、密钥、超时、并发、备用、尺寸、时长……），
值支持“10分钟”“允许”这类写法。每次修改前自动备份，`undo` 可撤销。

本机（3080e5）建议的模型：

| 名称 | 类型 | 关键配置 |
|---|---|---|
| 本地出图 | comfyui | `base_url=http://host.docker.internal:<端口>`，`workflow=builtin:qwen_image`，`default_size=1024x576` |
| 本地视频 | comfyui | 同一个 ComfyUI，`workflow=builtin:h3_video`，`default_size=896x512`，`timeout_s=2400`，`free_after_job=true` |

内置工作流需要的是 `~/TEST/comfyui-h3/ComfyUI` 那套环境（有 `TextEncodeQwenImage21`、`MiniMaxH3ImageToVideo`、VHS 节点）。
`:8188` 上的原 ComfyUI 没有这些节点，`/mg_probe` 会直接报缺哪个节点。自定义工作流：在 ComfyUI 里“导出（API）”，
把要替换的值写成 `{{prompt}}`、`{{seed}}`、`{{width}}`、`{{height}}`、`{{frames}}`、`{{image}}` 等占位符，
放进 `data/plugin_data/astrbot_plugin_media_gen/workflows/`，`workflow` 填文件名。

插件跑在 Docker 里时，访问宿主机服务要用 `host.docker.internal`（`check` 会提示写成 127.0.0.1 的配置）。
本机/内网/Tailscale 地址会自动绕过 `HTTP(S)_PROXY` 直连。

## 稳定性设计

- **任务持久化**：每个任务写入 `jobs.json`（原子写），远端任务 ID 一拿到就落盘。插件重载/重启后自动续跑，不会重复提交。
  提交到一半就中断的付费任务结果未知，标记失败并提示可重试，不自动重提（防重复扣费）。本地 ComfyUI 的会直接重跑。
- **三层超时**：单次 HTTP（连接 15s / 读取 120s）、提交（默认 180s）、整体（图片 300s / 视频 1800s，可按模型设置）。
  排队等待单独计时（默认最长 2 小时）。超时会尝试取消远端任务。
- **重试分级**：查询类请求遇到网络抖动、429、5xx 都重试，并遵守 `Retry-After`。提交类请求只在“肯定没被受理”
  （连不上、429、503）时重试。轮询连续失败 8 次才判定失败，轮询间隔逐步拉长到 30 秒。
- **熔断 + 回退**：模型连续失败 3 次熔断 5 分钟，熔断期间直接走 `fallback`。内容审核、参数错误、余额不足这类
  “换模型也没用”的失败不回退、不计入熔断。可选 `auto_fallback` 自动换同类型的其他模型（可能用到收费模型，默认关）。
- **队列分组**：同一个 ComfyUI 的模型自动共用一个队列（默认并发 1），避免两个任务抢显存；`free_after_job` 跑完卸载模型，
  方便和短剧工厂、本地 LLM 共用显卡。
- **产物校验**：按文件头识别真实格式，视频用 ffprobe 校验可解码并记录时长/分辨率/是否有声音。
- **权限与额度**：普通用户默认只能出图，每天图片 20 张、视频 3 个（可配）。参考图不能用内网地址，也不能读插件数据目录以外的本地文件。

## 与账号运维插件联动

- 手动：生成后说“把这个视频发到 B 站，标题……”，AI 会调用 `media_task(publish)`，交给运维插件的 `publish_request`。
- 自动：自动化任务开了 `auto_publish` 后，生成完自动提交发布申请，审批预览推送到创建任务的会话。
- 发布**永远需要管理员在聊天里回复「确认 xxxx」**，生成插件只提交申请。
- 图片直接发成平台**图文**（抖音图文、B站图文动态；运维插件 v0.14+），配方 `kind=image` 开了 `auto_publish` 会把这一组图作为一条图文提交；
  `publish_images_as=视频` 或运维插件较旧时，先合成带推镜、淡入淡出和字幕的图文视频再发。`kind=slides` 始终是合成视频。
- 发布时默认声明“内容由 AI 生成”（`declare_ai_generated`）：抖音勾选自主声明，B站图文打开 AI 创作标记。
- 定时任务常在无人值守时完成，所以提交时会带上 `confirm_ttl`（默认 12 小时，可配置）；运维插件 v0.12+ 本身也把发布申请默认设为
  12 小时有效，并把待审批申请存到磁盘，重载、重启都不会丢。
- **结果回传**（运维插件 v0.12+）：提交时带上 `caller` / `caller_ref`（本插件名和任务号），发布成功、失败、被拒、超时作废后，
  运维插件回调本插件的 `on_publish_result`：任务状态里记下结果、作品链接，并推送到任务所在会话，例如
  “📤 已发布到B站：《标题》+ 链接”“⌛ 申请超时没人确认，已作废”。自动化任务的“上次结果”也会更新。
- **选题参考运营数据**（`planner_use_insight`，默认开）：发布到平台的自动化任务构思前，读取运维插件的 `ops_insight`
  （账号数据、同类作者对比、表现最好的作品、平台热点、之前自动发布的内容表现），交给构思模型：延续受欢迎的方向、
  避开没人看的、热点只在相关时借用。运维插件太旧或读取失败时自动跳过，不影响生成。

## 测试

```bash
# 单元/协议/稳定性/联动测试（68 个），需要 aiohttp pytest pytest-asyncio
python -m pytest tests -q
```

`mock/server.py` 是多提供商模拟服务器：一个进程模拟上面所有接口（含可灵 JWT 校验、百炼异步头校验、MiniMax 三段式、
Veo 的 LRO 与带 key 下载……），可注入 429/500/503/挂起/任务失败/任务卡死等故障，还带一个脚本化的 OpenAI 兼容 LLM
（`/llm`，消息里写 `CALL 工具名 {参数}` 就发出对应工具调用），用来端到端测 AstrBot 的工具链路。

```bash
# 假后端：ffmpeg 生成测试图/视频，秒出
~/TEST/comfyui-h3/.venv/bin/python -m mock.server --port 8199 --backend fake --faults "429:0.1,500:0.05"
# 真后端：把本机 Qwen-Image / H3 包装成各家云接口
~/TEST/comfyui-h3/.venv/bin/python -m mock.server --port 8199 --backend comfy --comfy-url http://127.0.0.1:8192
```
