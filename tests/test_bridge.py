"""与账号运维插件的联动：发布带回传参数、运营洞察转选题参考、发布结果转消息。"""

import pytest

from astrbot_plugin_media_gen.core import bridge

INSIGHT = {
    "days": 7,
    "platforms": [
        {"platform": "bilibili", "error": "未登录"},
        {
            "platform": "douyin", "totals": {"fans": 120, "likes": 360, "following": 40},
            "period": {"播放": 15, "点赞": 4, "评论": 2, "分享": 1, "主页访问": 12, "新增粉丝": 5},
            "posts_in_period": 0,
            "vs_similar": {"播放": [15, 20], "完播率": [0.15, 0.25]},
            "best_works": [{"title": "下班路上的晚霞", "views": 12345, "likes": 40}],
            "our_publishes": [{"title": "橘猫打哈欠", "status": "published", "views": 120, "likes": 3},
                              {"title": "落叶", "status": "failed"}],
            "trending": ["秋天的第一杯奶茶", "猫咪迷惑行为"],
        },
    ],
}


class FakeStar:
    def __init__(self):
        self.calls = []

    async def publish_request(self, event, path, title, tags, platform="douyin", account=None, **opts):
        self.calls.append(opts)
        return {"status": "pending_confirmation", "code": "1234"}

    async def ops_insight(self, platform="", days=7):
        return INSIGHT


class Meta:
    def __init__(self, star):
        self.star_cls, self.activated = star, True


class Ctx:
    def __init__(self, star):
        self.meta = Meta(star) if star else None

    def get_registered_star(self, name):
        return self.meta


def test_insight_text_keeps_what_planning_needs():
    text = bridge.insight_text(INSIGHT, "douyin")
    assert "粉丝 120" in text and "近7天发布 0 条" in text
    assert "完播率 15%（同类 25%）" in text
    assert "《下班路上的晚霞》播放 1.2万" in text
    assert "《橘猫打哈欠》已发布 播放 120" in text and "《落叶》发布失败" in text
    assert "秋天的第一杯奶茶" in text
    assert len(text) <= 900


def test_insight_text_empty_when_unavailable():
    assert bridge.insight_text(None, "douyin") == ""
    assert bridge.insight_text(INSIGHT, "bilibili") == ""  # 该平台报错
    assert bridge.insight_text({"platforms": []}, "douyin") == ""


@pytest.mark.asyncio
async def test_insight_missing_plugin_or_old_version():
    assert await bridge.insight(Ctx(None), "x", "douyin") is None

    class Old:  # 运维插件 v0.11 及以前没有 ops_insight
        async def publish_request(self, *a, **k):
            return {}

    assert await bridge.insight(Ctx(Old()), "x", "douyin") is None
    assert (await bridge.insight(Ctx(FakeStar()), "x", "douyin"))["days"] == 7


@pytest.mark.asyncio
async def test_publish_passes_caller_and_drops_empty_opts():
    star = FakeStar()
    res = await bridge.publish(Ctx(star), "x", object(), "/v.mp4", "标题", ["猫"], "bilibili",
                               desc="", confirm_ttl=0, caller="astrbot_plugin_media_gen", caller_ref="job1")
    assert res["code"] == "1234"
    assert star.calls == [{"caller": "astrbot_plugin_media_gen", "caller_ref": "job1"}]


def test_result_message():
    ok = bridge.result_message({"status": "published", "platform": "bilibili", "title": "橘猫",
                                "url": "https://www.bilibili.com/video/BV1x", "message": "B站审核通过后公开"})
    assert "已发布到B站" in ok and "BV1x" in ok and "审核" in ok
    assert "失败" in bridge.result_message({"status": "failed", "platform": "douyin", "message": "上传超时"}, "猫")
    assert "没有被批准" in bridge.result_message({"status": "rejected", "platform": "douyin", "code": "0001"})
    assert "超时没人确认" in bridge.result_message({"status": "expired", "platform": "douyin", "code": "0002"})
