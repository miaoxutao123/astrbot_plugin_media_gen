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
