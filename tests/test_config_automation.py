"""自然语言配置落地层与自动化定时逻辑。"""

import time
from datetime import datetime

import pytest

from astrbot_plugin_media_gen.core import automation
from astrbot_plugin_media_gen.core.automation import Automations, parse_plan, parse_schedule
from astrbot_plugin_media_gen.core.registry import Registry, make_http_factory
from astrbot_plugin_media_gen.core.settings import Settings
from astrbot_plugin_media_gen.core.types import GenError


class Cfg(dict):
    saves = 0

    def save_config(self):
        Cfg.saves += 1


def test_add_with_preset_and_chinese_fields(tmp_path):
    cfg = Cfg(models=[])
    s = Settings(cfg, str(tmp_path))
    res = s.add("硅基出图", preset="siliconflow_image", values={"密钥": "sk-abcdefghijk", "超时": "2分钟", "并发": "3"})
    assert res["status"] == "added" and res["default"] is True
    m = cfg["models"][0]
    assert m["__template_key"] == "openai_image" and m["api_key"] == "sk-abcdefghijk"
    assert m["timeout_s"] == 120 and m["max_concurrency"] == 3 and m["kind"] == "image"
    assert cfg["default_image_model"] == "硅基出图"
    assert "sk-abcdefghijk" not in str(s.show())  # 不回显完整密钥
    # 新增的模型能被注册表正确解析
    reg = Registry(cfg, make_http_factory(cfg), str(tmp_path))
    assert reg.get("硅基出图").provider == "openai_image"


def test_video_only_provider_gets_video_kind(tmp_path):
    cfg = Cfg(models=[])
    s = Settings(cfg, str(tmp_path))
    s.add("sora", provider="openai_video", values={"api_key": "sk-1"})
    assert cfg["models"][0]["kind"] == "video"
    with pytest.raises(GenError):
        s.add("x", provider="openai_video", values={"类型": "image"})


def test_set_rename_updates_refs_and_undo(tmp_path):
    cfg = Cfg(models=[])
    s = Settings(cfg, str(tmp_path))
    s.add("a", preset="comfy_qwen_image")
    s.add("b", preset="siliconflow_image", values={"api_key": "k", "备用": "a"})
    s.set("a", {"名称": "本地出图", "步数": 30})
    assert cfg["default_image_model"] == "本地出图"
    assert cfg["models"][1]["fallback"] == "本地出图"
    assert cfg["models"][0]["steps"] == 30
    s.undo()
    assert cfg["models"][0]["name"] == "a"


def test_bad_values_rejected(tmp_path):
    s = Settings(Cfg(models=[]), str(tmp_path))
    with pytest.raises(GenError):
        s.add("x", preset="nope")
    s.add("x", preset="ark_seedream", values={"api_key": "k"})
    with pytest.raises(GenError):
        s.set("x", {"不存在的字段": 1})
    with pytest.raises(GenError):
        s.set("x", {"尺寸": "大概一千"})
    res = s.set("", {"视频超时": "45分钟", "普通用户视频": "允许"})
    assert res["changed"] == {"video_timeout_s": 2700, "allow_non_admin_video": True}


def test_lint_flags_missing_key(tmp_path):
    s = Settings(Cfg(models=[]), str(tmp_path))
    res = s.add("k", preset="kling_video")
    assert any("AccessKey" in w for w in res["warnings"])


def test_schedule_parse():
    assert parse_schedule("10:00") == ("daily", [(10, 0)])
    assert parse_schedule("09:30,20:00") == ("daily", [(9, 30), (20, 0)])
    assert parse_schedule("每天9点半") == ("daily", [(9, 30)])
    assert parse_schedule("every 6h") == ("interval", 21600)
    assert parse_schedule("每30分钟") == ("interval", 1800)
    assert parse_schedule("manual")[0] == "manual"
    with pytest.raises(GenError):
        parse_schedule("every 1m")


async def test_daily_due_and_caps(tmp_path, monkeypatch):
    # 固定日期 + 假时钟：trigger 用 time.time() 记 last_run，不能依赖真实的当前时刻
    # （之前在本地 08:00 前跑，真实的 last_run 早于 08:00 档，会被判成还没跑过）
    day = datetime(2026, 1, 15).timestamp()
    clock = [day]
    monkeypatch.setattr(automation.time, "time", lambda: clock[0])
    runs = []

    async def runner(r, reason):
        runs.append(reason)
        return {"ok": True}

    cfg = Cfg(automations=[])
    a = Automations(cfg, str(tmp_path), runner)
    a.add({"name": "猫", "topic": "治愈小猫", "schedule": "08:00", "平台": "抖音", "类型": "图文"})
    r = cfg["automations"][0]
    assert r["platform"] == "douyin" and r["kind"] == "slides"
    # 创建于 07:00，08:05 时应触发；同一天再查不应重复
    a.state["猫"]["armed_at"] = day + 7 * 3600
    clock[0] = day + 8 * 3600 + 300
    assert [x["name"] for x in a.due(clock[0])] == ["猫"]
    await a.trigger(r, "schedule")
    assert runs == ["schedule"]
    assert a.due(day + 9 * 3600) == []
    # 创建于 09:00 的任务不应补跑当天 08:00
    a.state["猫"] = {"armed_at": day + 9 * 3600}
    assert a.due(day + 9 * 3600 + 60) == []
    # 超过 2 小时的错过不补跑
    a.state["猫"] = {"armed_at": day + 1 * 3600}
    assert a.due(day + 11 * 3600) == []


async def test_interval_and_max_per_day(tmp_path):
    async def runner(r, reason):
        return {}

    cfg = Cfg(automations=[])
    a = Automations(cfg, str(tmp_path), runner)
    a.add({"name": "x", "topic": "t", "schedule": "every 1h", "max_per_day": 1})
    now = time.time()
    a.state["x"]["armed_at"] = now - 3700
    assert a.due(now)
    await a.trigger(cfg["automations"][0], "schedule")
    a.state["x"]["last_run"] = now - 3700
    assert a.due(now) == []  # 今日额度已用完
    assert "上限" in a.state["x"]["last_result"]


def test_parse_plan_tolerant():
    plan = parse_plan('好的：\n```json\n{"title":"橘猫的清晨","tags":"#猫 #治愈","prompt":"an orange cat"}\n```')
    assert plan["tags"] == ["猫", "治愈"] and plan["title"] == "橘猫的清晨"
    with pytest.raises(ValueError):
        parse_plan("没有 JSON")
