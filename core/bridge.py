"""与账号运维插件（astrbot_plugin_videos_analysis）的联动：把生成的视频交给它的 publish_request。

发布永远走运维插件自己的审批：这里只“提交申请”，管理员在聊天里回复「确认 xxxx」后才真正发布。
定时任务没有真实的消息事件，用 BridgeEvent 冒充“创建该任务的管理员在推送会话里发起申请”，
预览因此会发到推送会话，由管理员本人确认。
"""

from __future__ import annotations

from .types import log


class BridgeEvent:
    """publish_request 只用到事件的这几个属性/方法。"""

    def __init__(self, umo: str, sender_id: str = "media_gen", sender_name: str = "AI 生成插件", admin: bool = True):
        self.unified_msg_origin = umo
        self._sid, self._name, self._admin = sender_id, sender_name, admin
        self.message_obj = None

    def get_sender_id(self) -> str:
        return self._sid

    def get_sender_name(self) -> str:
        return self._name

    def is_admin(self) -> bool:
        return self._admin


def ops_plugin(context, name: str):
    meta = context.get_registered_star(name)
    if not meta or not getattr(meta, "activated", True) or not getattr(meta, "star_cls", None):
        return None
    return meta.star_cls if hasattr(meta.star_cls, "publish_request") else None


PLATFORM_LABEL = {"douyin": "抖音", "bilibili": "B站"}
STATUS_TEXT = {"published": "已发布", "failed": "发布失败", "rejected": "管理员没有批准", "expired": "审批超时作废"}


async def insight(context, name: str, platform: str, days: int = 7, timeout: float = 40) -> dict | None:
    """读运维插件的运营洞察（v0.12+ 的 ops_insight），没有或失败返回 None，不影响生成。"""
    import asyncio

    meta = context.get_registered_star(name)
    fn = getattr(getattr(meta, "star_cls", None), "ops_insight", None) if meta else None
    if not fn:
        return None
    try:
        return await asyncio.wait_for(fn(platform, days), timeout=timeout)
    except Exception as e:
        log.warning(f"[media_gen] 读取运营洞察失败: {e}")
        return None


def _fmt_num(n) -> str:
    return f"{n / 10000:.1f}万" if isinstance(n, (int, float)) and n >= 10000 else str(n)


def insight_text(data: dict | None, platform: str, limit: int = 900) -> str:
    """把运营洞察压成给构思模型看的几行中文（只取选题有用的部分）。"""
    if not data:
        return ""
    p = next((x for x in data.get("platforms") or [] if x.get("platform") == platform), None)
    if not p or p.get("error"):
        return ""
    lines = []
    tot = p.get("totals") or {}
    label = {"fans": "粉丝", "plays": "播放", "likes": "获赞"}
    acc = "，".join(f"{label[k]} {_fmt_num(tot[k])}" for k in label if k in tot)
    period = p.get("period") or {}
    if period:
        acc += f"；近{data.get('days', 7)}天 " + "，".join(f"{k} {_fmt_num(v)}" for k, v in list(period.items())[:5])
    if "posts_in_period" in p:
        acc += f"；近{data.get('days', 7)}天发布 {p['posts_in_period']} 条"
    if acc:
        lines.append(f"账号（{PLATFORM_LABEL.get(platform, platform)}）：{acc}")
    vs = p.get("vs_similar") or {}
    if vs:
        def f(k, v):
            return f"{v * 100:.0f}%" if k in ("互动率", "完播率") and isinstance(v, (int, float)) else _fmt_num(v)

        lines.append("和同类作者比：" + "，".join(f"{k} {f(k, o)}（同类 {f(k, s)}）" for k, (o, s) in vs.items()))

    def works(rows):
        return "；".join(f"《{w.get('title', '')}》播放 {_fmt_num(w.get('views', 0))} 赞 {_fmt_num(w.get('likes', 0))}" for w in rows)

    if p.get("best_works"):
        lines.append("表现最好的作品：" + works(p["best_works"]))
    if p.get("our_publishes"):
        ours = [f"《{r.get('title', '')}》{STATUS_TEXT.get(r.get('status'), r.get('status'))}"
                + (f" 播放 {_fmt_num(r['views'])} 赞 {_fmt_num(r.get('likes', 0))}" if "views" in r else "")
                for r in p["our_publishes"][:4]]
        lines.append("自动生成并发布过的：" + "；".join(ours))
    if p.get("trending"):
        lines.append("平台当前热点：" + "、".join(p["trending"][:8]))
    text = "\n".join(lines)
    return text[:limit]


def result_message(result: dict, job_title: str = "") -> str:
    """运维插件回传的发布结果 → 推送给会话的一句话。"""
    status = result.get("status", "")
    plat = PLATFORM_LABEL.get(result.get("platform", ""), result.get("platform", ""))
    title = result.get("title") or job_title
    if status == "published":
        return f"📤 已发布到{plat}：《{title}》" + (f"\n{result['url']}" if result.get("url") else "") + \
            ("\n（平台审核通过后公开）" if "审核" in (result.get("message") or "") else "")
    if status == "failed":
        return f"❌ 发布到{plat}失败：《{title}》\n{result.get('message', '')[:200]}\n可以说“重新发布”再试"
    if status == "rejected":
        return f"🚫 发布到{plat}的申请 [{result.get('code', '')}] 没有被批准：《{title}》"
    if status == "expired":
        return f"⌛ 发布到{plat}的申请 [{result.get('code', '')}] 超时没人确认，已作废：《{title}》\n需要的话说“重新发布”"
    return f"发布状态更新：{status}"


async def publish(context, name: str, event, video_path: str, title: str, tags: list[str], platform: str,
                  account: str = "", **opts) -> dict:
    star = ops_plugin(context, name)
    if not star:
        return {"status": "unavailable", "error": f"没有找到已启用的账号运维插件 {name}（需要 v0.5.0+ 的 publish_request）"}
    try:
        res = await star.publish_request(event, video_path, title, tags, platform=platform, account=account or None,
                                         **{k: v for k, v in opts.items() if v})
    except Exception as e:  # 对方保证不抛，但防御一下
        log.exception("[media_gen] 调用 publish_request 失败")
        return {"status": "rejected", "error": str(e)[:200]}
    return res if isinstance(res, dict) else {"status": "unknown", "raw": str(res)[:200]}
