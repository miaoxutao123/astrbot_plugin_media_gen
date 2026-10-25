"""与 AstrBot 无关的基础类型：生成请求、轮询状态、产物、错误分级。"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from dataclasses import asdict, dataclass, field

try:  # 在 AstrBot 里必须用它的 logger：其 handler 的格式需要 plugin_tag 等字段，裸 getLogger 会在打日志时抛异常
    from astrbot.api import logger as log
except Exception:  # 单元测试 / mock 服务器等脱离 AstrBot 的场景
    log = logging.getLogger("media_gen")

IMAGE = "image"
VIDEO = "video"


class GenError(Exception):
    """生成失败。

    retryable：同一请求换个时间/换个模型可能成功（限流、5xx、超时、服务下线）
    user_error：请求本身有问题（提示词违规、参数非法、余额不足），换模型重试也没用
    """

    def __init__(self, message: str, *, retryable: bool = False, user_error: bool = False,
                 status: int | None = None, code: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.user_error = user_error
        self.status = status
        self.code = code

    def __str__(self) -> str:
        head = f"[{self.code or self.status}] " if (self.code or self.status) else ""
        return head + super().__str__()


class GenTimeout(GenError):
    def __init__(self, message: str):
        super().__init__(message, retryable=True, code="timeout")


@dataclass
class RefImage:
    """参考图/首尾帧。始终先落到本地，再按各家接口需要转成 data URL / base64 / multipart / 上传。"""

    path: str
    url: str = ""  # 原始公网地址（有的话，部分接口只收 URL 时优先用它）

    @property
    def mime(self) -> str:
        return mimetypes.guess_type(self.path)[0] or "image/png"

    def read(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def b64(self) -> str:
        return base64.b64encode(self.read()).decode()

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.b64()}"

    def url_or_data(self) -> str:
        return self.url if self.url.startswith(("http://", "https://")) else self.data_url()

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class GenRequest:
    kind: str  # image / video
    prompt: str
    negative_prompt: str = ""
    images: list[RefImage] = field(default_factory=list)  # 参考图；视频时第 1 张作首帧
    last_frame: RefImage | None = None
    size: str = ""  # "1280x720"（统一用 x，适配器各自转换成 1280*720 / 16:9 等）
    aspect_ratio: str = ""  # "16:9"
    duration: float = 0.0  # 秒，视频用
    n: int = 1
    seed: int | None = None
    audio: bool | None = None  # 视频是否要声音（支持的模型才生效）
    extra: dict = field(default_factory=dict)  # 透传给接口的额外参数

    def width_height(self, default: tuple[int, int] = (1024, 1024)) -> tuple[int, int]:
        return parse_size(self.size) or (size_from_ratio(self.aspect_ratio, max(default)) if self.aspect_ratio else default)

    def ratio(self, default: str = "16:9") -> str:
        if self.aspect_ratio:
            return self.aspect_ratio
        wh = parse_size(self.size)
        return ratio_of(*wh) if wh else default

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> GenRequest:
        d = dict(d)
        d["images"] = [RefImage(**i) for i in d.get("images") or []]
        if d.get("last_frame"):
            d["last_frame"] = RefImage(**d["last_frame"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Asset:
    """一个生成产物：远端 url、data URL/base64（data）或已在本地（path）。"""

    kind: str
    url: str = ""
    data: str = ""  # base64 或 data URL
    path: str = ""
    headers: dict = field(default_factory=dict)  # 下载 url 时要带的头（如 OpenAI/Gemini 鉴权）
    ext: str = ""


@dataclass
class PollResult:
    state: str  # pending / running / succeeded / failed
    progress: float | None = None  # 0-100
    assets: list[Asset] = field(default_factory=list)
    error: str = ""
    user_error: bool = False

    @property
    def done(self) -> bool:
        return self.state in ("succeeded", "failed")


# ── 尺寸工具 ─────────────────────────────────────────────

_COMMON_RATIOS = {"1:1": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3), "3:4": (3, 4),
                  "3:2": (3, 2), "2:3": (2, 3), "21:9": (21, 9)}


def parse_size(size: str) -> tuple[int, int] | None:
    if not size:
        return None
    s = size.lower().replace("*", "x").replace("×", "x").replace(" ", "")
    if "x" not in s:
        return None
    try:
        w, h = s.split("x", 1)
        return int(w), int(h)
    except ValueError:
        return None


def size_from_ratio(ratio: str, long_edge: int = 1024, multiple: int = 16) -> tuple[int, int]:
    rw, rh = _COMMON_RATIOS.get(ratio, None) or tuple(int(x) for x in ratio.split(":"))
    if rw >= rh:
        w, h = long_edge, long_edge * rh / rw
    else:
        w, h = long_edge * rw / rh, long_edge
    return int(round(w / multiple) * multiple), int(round(h / multiple) * multiple)


def ratio_of(w: int, h: int) -> str:
    """取最接近的常用比例（各家视频接口一般只接受固定几档）。"""
    target = w / h
    return min(_COMMON_RATIOS, key=lambda k: abs(_COMMON_RATIOS[k][0] / _COMMON_RATIOS[k][1] - target))


def snap(value: int, multiple: int) -> int:
    return max(multiple, int(round(value / multiple)) * multiple)


def dig(obj, path: str, default=None):
    """按 a.b.0.c 取嵌套字段；路径里的数字当列表下标。"""
    cur = obj
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        else:
            return default
    return cur
