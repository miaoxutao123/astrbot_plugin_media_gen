"""超时、重试、熔断、回退、续跑、取消、并发队列。"""

import asyncio

from astrbot_plugin_media_gen.core.jobs import JobManager
from astrbot_plugin_media_gen.core.registry import Registry, make_http_factory
from astrbot_plugin_media_gen.core.types import GenRequest

from .conftest import model_configs


def mgr_for(mock, tmp_path, overrides: dict | None = None, **cfg) -> JobManager:
    models = model_configs(mock.public)
    for m in models:
        m.update((overrides or {}).get(m["name"], {}))
    config = {"models": models, "http_retries": 3, "http_backoff_s": 0.05, **cfg}
    reg = Registry(config, make_http_factory(config), str(tmp_path))
    return JobManager(reg, config, str(tmp_path))


def video(prompt="稳定性测试"):
    return GenRequest(kind="video", prompt=prompt, duration=4)


def image(prompt="稳定性测试"):
    return GenRequest(kind="image", prompt=prompt)


async def test_429_retry_after_then_success(mock_server, tmp_path):
    mock_server.faults = {"429": 1.0, "max": 2}
    mgr = mgr_for(mock_server, tmp_path)
    job = mgr.submit(image(), model="seedream")
    assert await mgr.wait(job, 20)
    assert job.status == "succeeded", job.errors
    assert mock_server.injected == 2
    await mgr.registry.close()


async def test_submit_500_not_retried_but_falls_back(mock_server, tmp_path):
    """提交时 500 可能已被受理 → 不在原接口重试（防重复扣费），直接换回退模型。"""
    mock_server.faults = {"500": 1.0, "max": 1}
    mgr = mgr_for(mock_server, tmp_path, {"sora": {"fallback": "seedance"}})
    job = mgr.submit(video(), model="sora")
    assert await mgr.wait(job, 20)
    assert job.status == "succeeded", job.errors
    assert job.model == "seedance"
    assert job.tried == ["sora", "seedance"]
    assert "500" in job.errors[0]
    assert sum(1 for t in mock_server.tasks.values()) == 1  # sora 没有被重复提交
    await mgr.registry.close()


async def test_poll_errors_are_tolerated(mock_server, tmp_path):
    mgr = mgr_for(mock_server, tmp_path)
    job = mgr.submit(video(), model="ds-wan")
    while not job.handle:
        await asyncio.sleep(0.02)
    mock_server.faults = {"500": 1.0, "max": 4}  # 轮询期间连续 4 次 500
    assert await mgr.wait(job, 30)
    assert job.status == "succeeded", job.errors
    await mgr.registry.close()


async def test_stuck_task_times_out_and_cancels(mock_server, tmp_path):
    mock_server.faults = {"stuck": 1.0, "max": 1}
    mgr = mgr_for(mock_server, tmp_path, {"seedance": {"timeout_s": 2}})
    job = mgr.submit(video(), model="seedance")
    assert await mgr.wait(job, 20)
    assert job.status == "failed"
    assert "超过 2 秒" in job.error
    assert all(t.cancelled for t in mock_server.tasks.values())  # 已调用远端取消
    await mgr.registry.close()


async def test_hung_submit_hits_submit_timeout(mock_server, tmp_path):
    mock_server.faults = {"hang": 1.0, "max": 1}
    mgr = mgr_for(mock_server, tmp_path, {"oa-img": {"submit_timeout_s": 1}}, read_timeout_s=1)
    job = mgr.submit(image(), model="oa-img")
    assert await mgr.wait(job, 30)
    assert job.status == "failed"
    assert "超时" in job.error or "timeout" in job.error.lower()
    await mgr.registry.close()


async def test_content_failure_does_not_fall_back(mock_server, tmp_path):
    """内容审核类失败换模型也没用，不回退、不计入熔断。"""
    mock_server.faults = {"fail": 1.0, "max": 1}
    mgr = mgr_for(mock_server, tmp_path, {"ds-wan": {"fallback": "seedance"}})
    job = mgr.submit(video(), model="ds-wan")
    assert await mgr.wait(job, 20)
    assert job.status == "failed"
    assert job.tried == ["ds-wan"]
    assert not mgr.registry.breaker.fails.get("ds-wan")
    await mgr.registry.close()


async def test_breaker_opens_and_skips(mock_server, tmp_path):
    """主模型服务挂了（连不上）：连续失败达到阈值后熔断，之后的任务直接走回退模型。"""
    mgr = mgr_for(mock_server, tmp_path, {"oa-img": {"base_url": "http://127.0.0.1:1/v1", "fallback": "seedream"}},
                  breaker_threshold=2, breaker_cooldown_s=60)
    for _ in range(2):
        job = mgr.submit(image(), model="oa-img")
        assert await mgr.wait(job, 20)
        assert job.status == "succeeded" and job.model == "seedream"
        assert "连接失败" in job.errors[0]
    assert mgr.registry.breaker.is_open("oa-img")
    job = mgr.submit(image(), model="oa-img")
    assert await mgr.wait(job, 20)
    assert job.status == "succeeded" and job.model == "seedream"
    assert "熔断" in job.errors[0]
    assert job.tried == ["seedream"]  # 熔断期间根本没去连 oa-img
    await mgr.registry.close()


async def test_resume_after_restart_without_resubmit(mock_server, tmp_path):
    mock_server.backend.video_delay = 1.5
    mgr = mgr_for(mock_server, tmp_path)
    job = mgr.submit(video(), model="kling-v")
    while not job.handle:
        await asyncio.sleep(0.02)
    await mgr.shutdown()  # 模拟插件重载
    assert len(mock_server.tasks) == 1
    await mgr.registry.close()

    mgr2 = mgr_for(mock_server, tmp_path)
    assert mgr2.jobs[job.id].status in ("running", "submitting", "queued")
    assert mgr2.resume_all() == 1
    j2 = mgr2.jobs[job.id]
    assert await mgr2.wait(j2, 30)
    assert j2.status == "succeeded", j2.errors
    assert len(mock_server.tasks) == 1  # 没有重复提交
    await mgr2.registry.close()


async def test_interrupted_paid_sync_submit_is_not_auto_retried(mock_server, tmp_path):
    import json
    import os
    mgr = mgr_for(mock_server, tmp_path)
    job = mgr.submit(image(), model="oa-img")
    await mgr.wait(job, 10)
    # 伪造“提交到一半插件崩了”的记录
    job.status, job.handle = "submitting", None
    mgr.save()
    await mgr.registry.close()
    mgr2 = mgr_for(mock_server, tmp_path)
    assert mgr2.resume_all() == 0
    assert mgr2.jobs[job.id].status == "failed"
    assert "重复扣费" in mgr2.jobs[job.id].error
    assert os.path.exists(os.path.join(tmp_path, "jobs.json"))
    json.load(open(os.path.join(tmp_path, "jobs.json")))
    await mgr2.registry.close()


async def test_cancel_running_job(mock_server, tmp_path):
    mock_server.backend.video_delay = 5
    mgr = mgr_for(mock_server, tmp_path)
    job = mgr.submit(video(), model="sora")
    while not job.handle:
        await asyncio.sleep(0.02)
    msg = await mgr.cancel(job)
    assert "已取消" in msg and job.status == "cancelled"
    assert all(t.cancelled for t in mock_server.tasks.values())
    await mgr.registry.close()


async def test_queue_group_limits_concurrency(mock_server, tmp_path):
    mock_server.backend.video_delay = 0.6
    mgr = mgr_for(mock_server, tmp_path, {"seedance": {"max_concurrency": 1}})
    jobs = [mgr.submit(video(f"p{i}"), model="seedance") for i in range(3)]
    peak = 0
    while any(j.status not in ("succeeded", "failed") for j in jobs):
        peak = max(peak, sum(1 for j in jobs if j.status in ("submitting", "running", "downloading")))
        await asyncio.sleep(0.02)
    assert all(j.status == "succeeded" for j in jobs)
    assert peak == 1
    await mgr.registry.close()


async def test_unknown_model_rejected_up_front(mock_server, tmp_path):
    from astrbot_plugin_media_gen.core.types import GenError
    mgr = mgr_for(mock_server, tmp_path)
    try:
        mgr.submit(image(), model="不存在的模型")
        raise AssertionError("应当抛错")
    except GenError as e:
        assert e.user_error and "可用" in str(e)
    await mgr.registry.close()
