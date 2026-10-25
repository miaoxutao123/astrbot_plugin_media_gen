import os
import sys

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(ROOT))

from astrbot_plugin_media_gen.mock.server import FakeBackend, Mock, build_app  # noqa: E402

KEY = "sk-mock"


@pytest_asyncio.fixture
async def mock_server():
    mock = Mock(FakeBackend(image_delay=0.05, video_delay=0.3), {}, "http://placeholder")
    server = TestServer(build_app(mock), host="127.0.0.1")
    await server.start_server()
    mock.public = str(server.make_url("")).rstrip("/")
    yield mock
    await server.close()


@pytest.fixture
def ref_png(tmp_path):
    import subprocess
    p = tmp_path / "ref.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1",
                    str(p)], check=True)
    return str(p)


def model_configs(base: str) -> list[dict]:
    """每种接口一份配置，全部指向模拟服务器。"""
    return [
        {"__template_key": "openai_image", "name": "oa-img", "kind": "image", "base_url": f"{base}/openai/v1",
         "api_key": KEY, "model": "gpt-image-1"},
        {"__template_key": "openai_image", "name": "oa-img-url", "kind": "image", "base_url": f"{base}/openai/v1",
         "api_key": KEY, "model": "dall-e-3", "response_format": "url", "ref_mode": "json_image"},
        {"__template_key": "openai_image", "name": "oa-chat-img", "kind": "image", "base_url": f"{base}/openai/v1",
         "api_key": KEY, "model": "gemini-image", "ref_mode": "chat"},
        {"__template_key": "openai_video", "name": "sora", "kind": "video", "base_url": f"{base}/openai/v1",
         "api_key": KEY, "model": "sora-2", "poll_interval_s": 0.2},
        {"__template_key": "dashscope", "name": "ds-qwen", "kind": "image", "base_url": f"{base}/dashscope/api/v1",
         "api_key": KEY, "model": "qwen-image-plus"},
        {"__template_key": "dashscope", "name": "ds-wanx", "kind": "image", "base_url": f"{base}/dashscope/api/v1",
         "api_key": KEY, "model": "wan2.2-t2i-flash", "poll_interval_s": 0.2},
        {"__template_key": "dashscope", "name": "ds-wan", "kind": "video", "base_url": f"{base}/dashscope/api/v1",
         "api_key": KEY, "model": "wan2.5-t2v-preview", "poll_interval_s": 0.2},
        {"__template_key": "volcengine_ark", "name": "seedream", "kind": "image", "base_url": f"{base}/ark/api/v3",
         "api_key": KEY, "model": "doubao-seedream-4-0"},
        {"__template_key": "volcengine_ark", "name": "seedance", "kind": "video", "base_url": f"{base}/ark/api/v3",
         "api_key": KEY, "model": "doubao-seedance-1-0-pro", "poll_interval_s": 0.2},
        {"__template_key": "kling", "name": "kling-v", "kind": "video", "base_url": f"{base}/kling",
         "access_key": "mock-ak", "secret_key": "mock-sk", "poll_interval_s": 0.2},
        {"__template_key": "kling", "name": "kling-i", "kind": "image", "base_url": f"{base}/kling",
         "api_key": "mock-ak:mock-sk", "poll_interval_s": 0.2},
        {"__template_key": "minimax", "name": "hailuo", "kind": "video", "base_url": f"{base}/minimax/v1",
         "api_key": KEY, "poll_interval_s": 0.2},
        {"__template_key": "minimax", "name": "mm-img", "kind": "image", "base_url": f"{base}/minimax/v1", "api_key": KEY},
        {"__template_key": "gemini", "name": "nano-banana", "kind": "image", "base_url": f"{base}/gemini/v1beta",
         "api_key": KEY, "model": "gemini-2.5-flash-image"},
        {"__template_key": "gemini", "name": "imagen", "kind": "image", "base_url": f"{base}/gemini/v1beta",
         "api_key": KEY, "model": "imagen-4.0-generate-001"},
        {"__template_key": "gemini", "name": "veo", "kind": "video", "base_url": f"{base}/gemini/v1beta",
         "api_key": KEY, "model": "veo-3.1-generate-preview", "poll_interval_s": 0.2},
        {"__template_key": "replicate", "name": "rep-flux", "kind": "image", "base_url": f"{base}/replicate/v1",
         "api_key": KEY, "model": "black-forest-labs/flux-schnell", "poll_interval_s": 0.2},
        {"__template_key": "replicate", "name": "rep-video", "kind": "video", "base_url": f"{base}/replicate/v1",
         "api_key": KEY, "model": "minimax/video-01", "poll_interval_s": 0.2, "wait_seconds": 5},
        {"__template_key": "fal", "name": "fal-flux", "kind": "image", "base_url": f"{base}/fal",
         "api_key": KEY, "model": "fal-ai/flux/schnell", "poll_interval_s": 0.2},
        {"__template_key": "fal", "name": "fal-video", "kind": "video", "base_url": f"{base}/fal",
         "api_key": KEY, "model": "fal-ai/kling-video/v2/master/text-to-video", "poll_interval_s": 0.2},
        {"__template_key": "openai_image", "name": "sf-img", "kind": "image", "base_url": f"{base}/siliconflow/v1",
         "api_key": KEY, "model": "Kwai-Kolors/Kolors", "ref_mode": "json_image"},
        {"__template_key": "generic", "name": "sf-video", "kind": "video", "base_url": f"{base}/siliconflow/v1",
         "api_key": KEY, "model": "Wan-AI/Wan2.2-T2V-A14B", "preset": "siliconflow_video", "poll_interval_s": 0.2},
        {"__template_key": "generic", "name": "newapi-video", "kind": "video", "base_url": f"{base}/newapi/v1",
         "api_key": KEY, "model": "kling-v1", "preset": "newapi_video", "poll_interval_s": 0.2},
        {"__template_key": "generic", "name": "jimeng-video", "kind": "video", "base_url": f"{base}/jimeng/v1",
         "api_key": KEY, "model": "jimeng-video-3.0", "preset": "jimeng_video"},
        {"__template_key": "openai_image", "name": "jimeng-img", "kind": "image", "base_url": f"{base}/jimeng/v1",
         "api_key": KEY, "model": "jimeng-4.0", "ref_mode": "none", "size_format": "none"},
    ]
