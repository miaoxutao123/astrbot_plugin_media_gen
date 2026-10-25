"""根据适配器的 fields 生成 _conf_schema.json（改了适配器字段后运行：python -m tools.gen_schema，在插件上级目录执行
`python -m astrbot_plugin_media_gen.tools.gen_schema`）。"""

from __future__ import annotations

import json
import os

from ..providers import ADAPTERS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COMMON = {
    "name": {"description": "模型名称（唯一，聊天里用这个名字点名）", "type": "string", "default": ""},
    "enabled": {"description": "启用", "type": "bool", "default": True},
    "kind": {"description": "生成类型", "type": "string", "options": ["image", "video"], "default": "image"},
    "base_url": {"description": "接口地址", "type": "string", "default": "",
                 "hint": "留空用官方默认地址；中转/反代/本地服务填完整前缀（含 /v1 等版本段）。插件跑在 Docker 里访问宿主机用 host.docker.internal"},
    "api_key": {"description": "API Key", "type": "string", "default": ""},
    "model": {"description": "模型 ID", "type": "string", "default": ""},
    "description": {"description": "给 AI 看的简介（擅长什么、速度、花费）", "type": "string", "default": "",
                    "hint": "例如：写实人像出图好，约 15 秒，免费本地"},
    "default_size": {"description": "默认尺寸（宽x高）", "type": "string", "default": ""},
    "default_duration": {"description": "默认视频时长（秒）", "type": "float", "default": 0},
    "timeout_s": {"description": "单个任务总超时（秒，0=图片 300 / 视频 1800）", "type": "int", "default": 0},
    "submit_timeout_s": {"description": "提交请求超时（秒，0=全局默认）", "type": "int", "default": 0},
    "poll_interval_s": {"description": "轮询间隔（秒，0=按接口默认，会逐步拉长）", "type": "float", "default": 0},
    "max_concurrency": {"description": "最大并发（0=图片 2 / 视频 1；ComfyUI 默认 1）", "type": "int", "default": 0},
    "queue_group": {"description": "队列分组（同组共享并发上限，如同一块显卡）", "type": "string", "default": ""},
    "fallback": {"description": "失败时依次改用的模型（名称，逗号分隔）", "type": "string", "default": ""},
    "proxy": {"description": "代理（如 http://127.0.0.1:7890）", "type": "string", "default": ""},
    "cost_hint": {"description": "费用说明（给 AI 和用户看）", "type": "string", "default": ""},
    "extra": {"description": "额外请求参数（JSON，原样合并进请求体，null 表示删除字段）", "type": "text", "default": ""},
}

TEMPLATE_NAMES = {
    "openai_image": "OpenAI 兼容图片（gpt-image / DALL·E / 硅基流动 / Seedream 中转 / 即梦反代 / 聊天出图）",
    "openai_video": "OpenAI Sora 视频（/v1/videos）",
    "dashscope": "阿里云百炼（万相视频 / 千问 qwen-image / 万相文生图）",
    "volcengine_ark": "火山方舟（豆包 Seedream 图片 / Seedance 视频）",
    "kling": "可灵 Kling（视频 / 图片）",
    "minimax": "MiniMax 海螺（视频 / 图片）",
    "gemini": "Google Gemini / Imagen / Veo",
    "replicate": "Replicate",
    "fal": "fal.ai",
    "comfyui": "ComfyUI 本地工作流（Qwen-Image 2.1 / H3 / 自定义）",
    "generic": "通用 HTTP 模板（硅基流动视频 / new-api 统一视频 / 即梦视频 / 任意接口）",
}


def _field(desc: str, default) -> dict:
    t = "bool" if isinstance(default, bool) else "int" if isinstance(default, int) else \
        "float" if isinstance(default, float) else "string"
    d = {"description": desc.split("（")[0] if len(desc) > 30 else desc, "type": t, "default": default}
    if len(desc) > 30:
        d["hint"] = desc
    if t == "string" and ("模板" in desc or "JSON" in desc):
        d["type"] = "text"
    return d


def models_schema() -> dict:
    templates = {}
    for key, cls in ADAPTERS.items():
        items = {k: dict(v) for k, v in COMMON.items()}
        items["kind"]["options"] = list(cls.kinds)
        items["kind"]["default"] = cls.kinds[0]
        if cls.default_base_url:
            items["base_url"]["hint"] = f"默认 {cls.default_base_url}。" + items["base_url"]["hint"]
        for fk, (desc, default) in cls.fields.items():
            items[fk] = _field(desc, default)
        templates[key] = {"name": TEMPLATE_NAMES.get(key, cls.label), "hint": cls.doc, "items": items}
    return {"description": "生成模型", "type": "template_list",
            "hint": "每个条目是一个可被 AI 调用的模型。也可以直接在聊天里让机器人帮你配置（管理员）。",
            "templates": templates}


def automations_schema() -> dict:
    items = {
        "name": {"description": "名称", "type": "string", "default": ""},
        "enabled": {"description": "启用", "type": "bool", "default": True},
        "schedule": {"description": "时间", "type": "string", "default": "10:00",
                     "hint": "每天固定时间：10:00 或 09:30,20:00；按间隔：every 6h / every 90m；只手动：manual"},
        "kind": {"description": "产出类型", "type": "string", "options": ["video", "image", "slides"], "default": "video",
                 "hint": "video=视频；image=单张/多张图（图文）；slides=多张图拼成带字幕的图文视频（可发布）"},
        "topic": {"description": "主题 / 创作方向", "type": "text", "default": "",
                  "hint": "例如：每天一个治愈系小动物短片，竖屏，适合抖音"},
        "style": {"description": "风格与要求", "type": "text", "default": ""},
        "model": {"description": "使用的模型（留空=默认）", "type": "string", "default": ""},
        "count": {"description": "图片张数（image/slides）", "type": "int", "default": 1},
        "size": {"description": "尺寸（如 720x1280，留空=模型默认）", "type": "string", "default": ""},
        "duration": {"description": "视频时长（秒，0=模型默认）", "type": "float", "default": 0},
        "platform": {"description": "发布平台", "type": "string", "options": ["none", "douyin", "bilibili"], "default": "none"},
        "account": {"description": "发布账号（账号运维插件里的账号名，留空=当前账号）", "type": "string", "default": ""},
        "auto_publish": {"description": "生成后自动提交发布申请（仍需管理员在聊天里确认）", "type": "bool", "default": False},
        "notify_umo": {"description": "结果/审批推送到的会话（在聊天里创建时自动填）", "type": "string", "default": ""},
        "max_per_day": {"description": "每天最多运行次数", "type": "int", "default": 3},
    }
    return {"description": "自动化生成任务", "type": "template_list",
            "hint": "按时间自动：AI 构思脚本 → 生成 → 推送 → （可选）交给账号运维插件发布审批",
            "templates": {"recipe": {"name": "定时生成", "items": items}}}


def build() -> dict:
    return {
        "models": models_schema(),
        "default_image_model": {"description": "默认图片模型（名称）", "type": "string", "default": ""},
        "default_video_model": {"description": "默认视频模型（名称）", "type": "string", "default": ""},
        "automations": automations_schema(),
        "planner_provider_id": {"description": "自动化构思用的聊天模型", "type": "string", "default": "",
                                "_special": "select_provider", "hint": "留空=用默认聊天模型"},
        "allow_non_admin_image": {"description": "允许普通用户生成图片", "type": "bool", "default": True},
        "allow_non_admin_video": {"description": "允许普通用户生成视频", "type": "bool", "default": False,
                                  "hint": "视频慢且贵（本地会长时间占用显卡），默认只给管理员"},
        "user_daily_image_limit": {"description": "普通用户每天图片上限", "type": "int", "default": 20},
        "user_daily_video_limit": {"description": "普通用户每天视频上限", "type": "int", "default": 3},
        "tool_wait_s": {"description": "AI 调用出图时最多同步等待（秒）", "type": "int", "default": 60,
                        "hint": "超过就先回复“生成中”，完成后自动发到聊天；视频总是异步"},
        "image_timeout_s": {"description": "图片任务默认超时（秒）", "type": "int", "default": 300},
        "video_timeout_s": {"description": "视频任务默认超时（秒）", "type": "int", "default": 1800},
        "submit_timeout_s": {"description": "提交请求默认超时（秒）", "type": "int", "default": 180},
        "queue_timeout_s": {"description": "排队最长等待（秒）", "type": "int", "default": 7200},
        "connect_timeout_s": {"description": "连接超时（秒）", "type": "int", "default": 15},
        "read_timeout_s": {"description": "单次读取超时（秒）", "type": "int", "default": 120},
        "http_retries": {"description": "网络错误/限流重试次数", "type": "int", "default": 3},
        "max_poll_errors": {"description": "连续查询失败多少次判定失败", "type": "int", "default": 8},
        "max_poll_interval_s": {"description": "最长轮询间隔（秒）", "type": "int", "default": 30},
        "breaker_threshold": {"description": "连续失败几次熔断", "type": "int", "default": 3},
        "breaker_cooldown_s": {"description": "熔断时长（秒）", "type": "int", "default": 300},
        "auto_fallback": {"description": "失败时自动换同类型的其他模型", "type": "bool", "default": False,
                          "hint": "关闭时只用模型自己配置的 fallback；打开可能用到收费模型"},
        "fallback_on_timeout": {"description": "超时后也换回退模型", "type": "bool", "default": True},
        "max_active_jobs": {"description": "同时排队/运行的任务上限", "type": "int", "default": 20},
        "default_image_concurrency": {"description": "图片模型默认并发", "type": "int", "default": 2},
        "default_video_concurrency": {"description": "视频模型默认并发", "type": "int", "default": 1},
        "send_as_file_mb": {"description": "视频超过多少 MB 改为发文件", "type": "int", "default": 100},
        "keep_files_days": {"description": "产物保留天数", "type": "int", "default": 7},
        "max_file_mb": {"description": "单个产物大小上限（MB）", "type": "int", "default": 512},
        "proxy": {"description": "全局代理（访问海外接口时用）", "type": "string", "default": ""},
        "publish_confirm_ttl_s": {"description": "自动化发布申请的审批有效期（秒）", "type": "int", "default": 43200,
                                  "hint": "定时任务常在无人时完成，默认 12 小时内回复「确认 xxxx」都有效（最长 24 小时，需运维插件支持 confirm_ttl）"},
        "ops_plugin": {"description": "账号运维插件名", "type": "string", "default": "astrbot_plugin_videos_analysis",
                       "hint": "发布联动调用它的 publish_request"},
        "planner_use_insight": {"description": "自动化选题参考账号运营数据", "type": "bool", "default": True,
                                "hint": "发布到平台的自动化任务构思时，读取运维插件的 ops_insight（账号数据、表现好的作品、热点、"
                                        "已发布内容的表现）一起交给构思模型；需要运维插件 v0.12+，没有时自动跳过"},
    }


def main() -> None:
    path = os.path.join(ROOT, "_conf_schema.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build(), f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
