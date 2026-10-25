"""适配器注册表 + 常用服务商快捷预设（自然语言配置时“加一个硅基流动出图”就靠这些预设补全）。"""

from __future__ import annotations

from .base import Adapter, ModelSpec
from .cn_vendors import Kling, MiniMax, VolcArk
from .comfyui import ComfyUI
from .dashscope import DashScope
from .generic import PRESETS as GENERIC_PRESETS
from .generic import Generic
from .global_vendors import Fal, Gemini, Replicate
from .openai_compat import OpenAIImage, OpenAIVideo

ADAPTERS: dict[str, type[Adapter]] = {
    cls.provider: cls for cls in (
        OpenAIImage, OpenAIVideo, DashScope, VolcArk, Kling, MiniMax, Gemini, Replicate, Fal, ComfyUI, Generic,
    )
}

# 服务商快捷预设：provider + 默认地址/模型/字段。用户只需补 api_key。
VENDOR_PRESETS: dict[str, dict] = {
    "openai_gpt_image": {"provider": "openai_image", "kind": "image", "base_url": "https://api.openai.com/v1",
                         "model": "gpt-image-1", "ref_mode": "edits"},
    "openai_sora": {"provider": "openai_video", "kind": "video", "base_url": "https://api.openai.com/v1",
                    "model": "sora-2", "allowed_seconds": "4,8,12"},
    "siliconflow_image": {"provider": "openai_image", "kind": "image", "base_url": "https://api.siliconflow.cn/v1",
                          "model": "Kwai-Kolors/Kolors", "ref_mode": "json_image"},
    "siliconflow_video": {"provider": "generic", "kind": "video", "preset": "siliconflow_video",
                          "model": "Wan-AI/Wan2.2-T2V-A14B"},
    "dashscope_qwen_image": {"provider": "dashscope", "kind": "image", "model": "qwen-image-plus"},
    "dashscope_wan_video": {"provider": "dashscope", "kind": "video", "model": "wan2.5-t2v-preview"},
    "dashscope_wan_i2v": {"provider": "dashscope", "kind": "video", "model": "wan2.5-i2v-preview", "resolution": "720P"},
    "ark_seedream": {"provider": "volcengine_ark", "kind": "image", "model": "doubao-seedream-4-0-250828"},
    "ark_seedance": {"provider": "volcengine_ark", "kind": "video", "model": "doubao-seedance-1-0-pro-250528"},
    "kling_video": {"provider": "kling", "kind": "video", "model": "kling-v2-1-master"},
    "kling_image": {"provider": "kling", "kind": "image", "model": "kling-v2"},
    "minimax_hailuo": {"provider": "minimax", "kind": "video", "model": "MiniMax-Hailuo-02"},
    "minimax_image": {"provider": "minimax", "kind": "image", "model": "image-01"},
    "gemini_image": {"provider": "gemini", "kind": "image", "model": "gemini-2.5-flash-image"},
    "google_veo": {"provider": "gemini", "kind": "video", "model": "veo-3.1-generate-preview"},
    "replicate_flux": {"provider": "replicate", "kind": "image", "model": "black-forest-labs/flux-schnell"},
    "fal_flux": {"provider": "fal", "kind": "image", "model": "fal-ai/flux/schnell"},
    "newapi_video": {"provider": "generic", "kind": "video", "preset": "newapi_video"},
    "jimeng_image": {"provider": "openai_image", "kind": "image", "base_url": "http://host.docker.internal:5100/v1",
                     "model": "jimeng-4.0", "ref_mode": "none", "size_format": "none"},
    "jimeng_video": {"provider": "generic", "kind": "video", "preset": "jimeng_video",
                     "base_url": "http://host.docker.internal:5100/v1", "model": "jimeng-video-3.0"},
    "comfy_qwen_image": {"provider": "comfyui", "kind": "image", "base_url": "http://host.docker.internal:8188",
                         "workflow": "builtin:qwen_image", "default_size": "1024x1024", "timeout_s": 900},
    "comfy_h3_video": {"provider": "comfyui", "kind": "video", "base_url": "http://host.docker.internal:8188",
                       "workflow": "builtin:h3_video", "default_size": "896x512", "default_duration": 5,
                       "timeout_s": 3600},
}


def build_adapter(spec: ModelSpec, http) -> Adapter:
    cls = ADAPTERS.get(spec.provider)
    if not cls:
        raise ValueError(f"未知的接口类型 {spec.provider}，可选：{', '.join(ADAPTERS)}")
    return cls(spec, http)


__all__ = ["ADAPTERS", "VENDOR_PRESETS", "GENERIC_PRESETS", "ModelSpec", "build_adapter", "Adapter"]
