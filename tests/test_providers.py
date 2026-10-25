"""每种接口协议对模拟服务器跑一遍完整链路：提交 → 轮询 → 下载 → 校验。"""

import os

import pytest

from astrbot_plugin_media_gen.core.jobs import JobManager
from astrbot_plugin_media_gen.core.registry import Registry, make_http_factory
from astrbot_plugin_media_gen.core.types import GenRequest, RefImage

from .conftest import model_configs

ALL = [c["name"] for c in model_configs("x")]
WITH_REF = ["oa-img", "oa-img-url", "sora", "ds-qwen", "ds-wan", "seedream", "seedance", "kling-v", "hailuo",
            "veo", "fal-video", "sf-video", "newapi-video", "jimeng-video"]


def make_mgr(base: str, tmp_path, **cfg) -> JobManager:
    config = {"models": model_configs(base), "http_retries": 2, **cfg}
    reg = Registry(config, make_http_factory(config), str(tmp_path))
    return JobManager(reg, config, str(tmp_path))


async def run_one(mgr: JobManager, name: str, ref: str | None = None, timeout: float = 30):
    spec = mgr.registry.get(name)
    req = GenRequest(kind=spec.kind, prompt=f"测试 {name}", size="640x360", duration=4,
                     images=[RefImage(path=ref)] if ref else [])
    job = mgr.submit(req, model=name)
    assert await mgr.wait(job, timeout), f"{name} 超时，状态 {job.status}"
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ALL)
async def test_text_to_media(mock_server, tmp_path, name):
    mgr = make_mgr(mock_server.public, tmp_path)
    job = await run_one(mgr, name)
    assert job.status == "succeeded", job.errors
    assert job.files and os.path.getsize(job.files[0]) > 64
    want = ".mp4" if job.kind == "video" else ".png"
    assert job.files[0].endswith(want), job.files
    if job.kind == "video":
        assert job.meta["video"]["duration"] > 0
    await mgr.registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", WITH_REF)
async def test_with_reference(mock_server, tmp_path, ref_png, name):
    mgr = make_mgr(mock_server.public, tmp_path)
    job = await run_one(mgr, name, ref=ref_png)
    assert job.status == "succeeded", job.errors
    await mgr.registry.close()


@pytest.mark.asyncio
async def test_probe_all(mock_server, tmp_path):
    mgr = make_mgr(mock_server.public, tmp_path)
    for spec in mgr.registry.specs():
        msg = await mgr.registry.adapter(spec).probe()
        assert msg, spec.name
    await mgr.registry.close()


@pytest.mark.asyncio
async def test_bad_key_is_not_retried(mock_server, tmp_path):
    mgr = make_mgr(mock_server.public, tmp_path)
    mgr.registry.config["models"][0]["api_key"] = "wrong"
    job = await run_one(mgr, "oa-img")
    assert job.status == "failed"
    assert "401" in job.error
    await mgr.registry.close()
