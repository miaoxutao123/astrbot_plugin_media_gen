"""适配器基类与模型配置。

每种接口协议一个适配器，统一成“提交 → 轮询 → 产物”三步：
- 同步接口（OpenAI images、即梦反代等）在 submit 里直接拿到产物，handle 带 assets
- 异步接口（Sora、百炼、方舟、可灵……）submit 返回任务 ID，poll 查询状态
handle 是可 JSON 序列化的 dict，会写进任务记录——插件重启后可以凭它继续轮询，不会丢任务也不会重复提交。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..core.http import Http
from ..core.types import IMAGE, VIDEO, Asset, GenError, GenRequest, PollResult

COMMON_KEYS = {
    "__template_key", "name", "enabled", "kind", "base_url", "api_key", "model", "description", "timeout_s",
    "submit_timeout_s", "poll_interval_s", "max_concurrency", "queue_group", "fallback", "proxy", "extra",
    "default_size", "default_duration", "provider", "cost_hint",
}


def _as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except ValueError as e:
            raise GenError(f"extra 不是合法 JSON: {e}", user_error=True) from e
    return {}


@dataclass
class ModelSpec:
    name: str
    provider: str
    kind: str = IMAGE
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    description: str = ""
    timeout_s: float = 0  # 0 = 按类型取默认（图 300s / 视频 1800s）
    submit_timeout_s: float = 0
    poll_interval_s: float = 0
    max_concurrency: int = 0
    queue_group: str = ""
    fallback: list[str] = field(default_factory=list)
    proxy: str = ""
    extra: dict = field(default_factory=dict)
    default_size: str = ""
    default_duration: float = 0
    cost_hint: str = ""
    options: dict = field(default_factory=dict)  # 适配器特有字段

    @classmethod
    def from_config(cls, item: dict) -> ModelSpec:
        provider = item.get("__template_key") or item.get("provider") or ""
        fb = item.get("fallback") or []
        if isinstance(fb, str):
            fb = [x.strip() for x in fb.replace("，", ",").split(",") if x.strip()]
        return cls(
            name=str(item.get("name") or "").strip(),
            provider=provider,
            kind=(item.get("kind") or IMAGE).strip().lower(),
            enabled=bool(item.get("enabled", True)),
            base_url=str(item.get("base_url") or "").strip(),
            api_key=str(item.get("api_key") or "").strip(),
            model=str(item.get("model") or "").strip(),
            description=str(item.get("description") or ""),
            timeout_s=float(item.get("timeout_s") or 0),
            submit_timeout_s=float(item.get("submit_timeout_s") or 0),
            poll_interval_s=float(item.get("poll_interval_s") or 0),
            max_concurrency=int(item.get("max_concurrency") or 0),
            queue_group=str(item.get("queue_group") or ""),
            fallback=list(fb),
            proxy=str(item.get("proxy") or ""),
            extra=_as_dict(item.get("extra")),
            default_size=str(item.get("default_size") or ""),
            default_duration=float(item.get("default_duration") or 0),
            cost_hint=str(item.get("cost_hint") or ""),
            options={k: v for k, v in item.items() if k not in COMMON_KEYS},
        )

    def opt(self, key: str, default=None):
        v = self.options.get(key)
        return default if v in (None, "") else v


class Adapter:
    provider = ""
    label = ""
    kinds: tuple[str, ...] = (IMAGE,)
    default_base_url = ""
    resumable = True  # 重启后能否凭 handle 继续轮询
    poll_interval = 5.0
    # 适配器特有的配置字段：key -> (说明, 默认值)；生成 _conf_schema 和自然语言配置都用它
    fields: dict[str, tuple[str, object]] = {}
    doc = ""

    def __init__(self, spec: ModelSpec, http: Http):
        self.spec = spec
        self.http = http

    # ── 子类实现 ─────────────────────────────
    async def submit(self, req: GenRequest) -> dict:
        raise NotImplementedError

    async def poll(self, handle: dict) -> PollResult:
        # 同步接口：submit 已经带回产物
        return PollResult("succeeded", 100, [Asset(**a) for a in handle.get("assets", [])])

    async def cancel(self, handle: dict) -> None:
        return None

    async def probe(self) -> str:
        """不花钱的连通性检查（列模型、查一个不存在的任务等）。返回一句话结论，失败抛异常。"""
        return "未提供免费的连通性检查，请用 test 实际生成一次"

    # ── 工具方法 ─────────────────────────────
    @property
    def base(self) -> str:
        return (self.spec.base_url or self.default_base_url).rstrip("/")

    def url(self, path: str) -> str:
        return self.base + path

    def bearer(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.spec.api_key:
            h["Authorization"] = f"Bearer {self.spec.api_key}"
        return h

    def body(self, base: dict, req: GenRequest) -> dict:
        """合并模型级 extra 与单次请求 extra（后者优先）；值为 null 表示删除该字段。"""
        out = dict(base)
        for src in (self.spec.extra, req.extra):
            for k, v in (src or {}).items():
                if v is None:
                    out.pop(k, None)
                elif isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = {**out[k], **v}
                else:
                    out[k] = v
        return out

    @staticmethod
    def sync_handle(assets: list[Asset]) -> dict:
        if not assets:
            raise GenError("接口返回成功但没有任何产物", retryable=True, code="no_output")
        return {"sync": True, "assets": [a.__dict__ for a in assets]}

    def require_key(self) -> None:
        if not self.spec.api_key:
            raise GenError(f"模型 {self.spec.name} 没有配置 api_key", user_error=True, code="no_key")

    def want(self, req: GenRequest) -> None:
        if req.kind not in self.kinds:
            raise GenError(f"{self.label} 不支持生成{'视频' if req.kind == VIDEO else '图片'}", user_error=True)


def collect_media(obj, kind: str) -> list[Asset]:
    """兜底：在任意 JSON 里找图片/视频 URL 或 base64（各家中转站的返回格式五花八门）。"""
    found: list[Asset] = []
    exts_img = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    exts_vid = (".mp4", ".mov", ".webm", ".m3u8")

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k.lower())
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str):
            low = o.lower().split("?")[0]
            if o.startswith("data:image") or o.startswith("data:video"):
                found.append(Asset(kind, data=o))
            elif o.startswith(("http://", "https://")) and (
                    low.endswith(exts_vid if kind == VIDEO else exts_img)
                    or key in ("url", "video_url", "image_url", "uri", "download_url", "file_url", "image", "video")):
                found.append(Asset(kind, url=o))
            elif key in ("b64_json", "base64", "image_base64", "bytesbase64encoded") and len(o) > 100:
                found.append(Asset(kind, data=o))

    walk(obj)
    seen, out = set(), []
    for a in found:
        k = a.url or a.data[:64]
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out
