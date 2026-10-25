"""国内主流厂商原生协议：火山方舟（Seedream / Seedance）、可灵 Kling、MiniMax 海螺。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from ..core.types import IMAGE, VIDEO, Asset, GenError, GenRequest, PollResult, dig
from .base import Adapter, collect_media
from .openai_compat import parse_image_response, snap_choice


# ═══════════════════════ 火山方舟 ═══════════════════════

class VolcArk(Adapter):
    """方舟：图片 POST /images/generations（同步，OpenAI 风格）；视频 POST /contents/generations/tasks（异步）。"""

    provider = "volcengine_ark"
    label = "火山方舟（豆包 Seedream / Seedance）"
    kinds = (IMAGE, VIDEO)
    default_base_url = "https://ark.cn-beijing.volces.com/api/v3"
    poll_interval = 8
    doc = "视频任务 status ∈ queued/running/succeeded/failed/cancelled/expired，成功后 content.video_url（24 小时有效）"
    fields = {
        "param_style": ("视频参数写法：body（新版顶层字段 ratio/duration/resolution）/ text（旧版写在提示词后 --ratio 16:9）", "body"),
        "resolution": ("视频分辨率 480p / 720p / 1080p（留空不传）", ""),
        "watermark": ("是否加水印", False),
    }

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        self.require_key()
        if req.kind == IMAGE:
            body = {"model": self.spec.model, "prompt": req.prompt, "response_format": "url",
                    "watermark": bool(self.spec.opt("watermark", False))}
            if req.size or req.aspect_ratio:
                w, h = req.width_height((2048, 2048))
                body["size"] = f"{w}x{h}"
            if req.seed is not None:
                body["seed"] = req.seed
            if req.images:
                urls = [r.url_or_data() for r in req.images]
                body["image"] = urls[0] if len(urls) == 1 else urls
            resp = await self.http.post_json(self.url("/images/generations"), self.body(body, req),
                                             headers=self.bearer(), timeout=self.spec.submit_timeout_s or 180)
            return self.sync_handle(parse_image_response(resp))

        text = req.prompt
        params: dict = {"watermark": bool(self.spec.opt("watermark", False))}
        if req.duration:
            params["duration"] = int(round(req.duration))
        if req.size or req.aspect_ratio:
            params["ratio"] = req.ratio()
        if res := self.spec.opt("resolution"):
            params["resolution"] = res
        if req.seed is not None:
            params["seed"] = req.seed
        if req.audio is not None:
            params["generate_audio"] = req.audio
        content: list[dict] = []
        if self.spec.opt("param_style", "body") == "text":
            text += "".join(f" --{k} {str(v).lower()}" for k, v in params.items())
            params = {}
        content.append({"type": "text", "text": text})
        if req.images:
            role = "first_frame" if (req.last_frame or len(req.images) == 1) else "reference_image"
            for r in req.images:
                content.append({"type": "image_url", "image_url": {"url": r.url_or_data()}, "role": role})
        if req.last_frame:
            content.append({"type": "image_url", "image_url": {"url": req.last_frame.url_or_data()},
                            "role": "last_frame"})
        body = self.body({"model": self.spec.model, "content": content, **params}, req)
        resp = await self.http.post_json(self.url("/contents/generations/tasks"), body, headers=self.bearer(),
                                         timeout=self.spec.submit_timeout_s or None)
        if not resp.get("id"):
            raise GenError(f"提交成功但没有任务 ID: {str(resp)[:200]}", code="no_id")
        return {"id": resp["id"]}

    async def poll(self, handle: dict) -> PollResult:
        if handle.get("sync"):
            return await super().poll(handle)
        resp = await self.http.get_json(self.url(f"/contents/generations/tasks/{handle['id']}"),
                                        headers=self.bearer(), timeout=30)
        status = str(resp.get("status") or "").lower()
        if status == "succeeded":
            url = dig(resp, "content.video_url")
            assets = [Asset(VIDEO, url=url, ext=".mp4")] if url else collect_media(resp.get("content"), VIDEO)
            if cover := dig(resp, "content.last_frame_url"):
                handle["last_frame_url"] = cover
            return PollResult("succeeded", 100, assets)
        if status in ("failed", "cancelled", "expired"):
            err = resp.get("error") or {}
            code = str(err.get("code", "")) if isinstance(err, dict) else ""
            msg = (err.get("message") if isinstance(err, dict) else str(err)) or status
            return PollResult("failed", error=f"{code} {msg}".strip(),
                              user_error="sensitive" in code.lower() or "invalid" in code.lower())
        return PollResult("running" if status == "running" else "pending")

    async def cancel(self, handle: dict) -> None:
        if handle.get("id"):
            try:
                await self.http.request("DELETE", self.url(f"/contents/generations/tasks/{handle['id']}"),
                                        headers=self.bearer(), timeout=20, retries=0)
            except GenError:
                pass

    async def probe(self) -> str:
        self.require_key()
        try:
            await self.http.get_json(self.url("/contents/generations/tasks?page_size=1"), headers=self.bearer(),
                                     timeout=20, retries=0)
        except GenError as e:
            if e.status in (401, 403):
                raise
            return f"接口可达（返回 {e.status}）"
        return "接口可达，API Key 有效"


# ═══════════════════════ 可灵 ═══════════════════════

def kling_jwt(ak: str, sk: str, ttl: int = 1800) -> str:
    """可灵鉴权：HS256 JWT，iss=AccessKey，exp=+30min，nbf=-5s。手写避免多装 PyJWT。"""

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64(json.dumps({"iss": ak, "exp": now + ttl, "nbf": now - 5}, separators=(",", ":")).encode())
    sig = hmac.new(sk.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(sig)}"


class Kling(Adapter):
    provider = "kling"
    label = "可灵 Kling"
    kinds = (IMAGE, VIDEO)
    default_base_url = "https://api-beijing.klingai.com"
    poll_interval = 10
    doc = "POST /v1/videos/text2video | image2video，GET 同路径/{task_id}；task_status ∈ submitted/processing/succeed/failed"
    fields = {
        "access_key": ("AccessKey（也可在 api_key 里填 AK:SK）", ""),
        "secret_key": ("SecretKey", ""),
        "mode": ("std（标准）/ pro（高品质）", "std"),
        "allowed_seconds": ("允许的时长档位", "5,10"),
    }

    def _auth(self) -> dict:
        ak, sk = self.spec.opt("access_key", ""), self.spec.opt("secret_key", "")
        if not (ak and sk) and ":" in self.spec.api_key:
            ak, sk = self.spec.api_key.split(":", 1)
        if ak and sk:
            token = kling_jwt(ak, sk)
        elif self.spec.api_key:  # 部分中转直接给 Bearer token
            token = self.spec.api_key
        else:
            raise GenError("可灵需要 AccessKey 和 SecretKey", user_error=True, code="no_key")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    @staticmethod
    def _img(r) -> str:
        # 可灵要求纯 base64（不带 data: 前缀）或 URL
        return r.url if r.url.startswith("http") else r.b64()

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        if req.kind == IMAGE:
            path = "/v1/images/generations"
            body = {"model_name": self.spec.model or "kling-v2", "prompt": req.prompt, "n": max(1, req.n),
                    "aspect_ratio": req.ratio("1:1")}
            if req.images:
                body["image"] = self._img(req.images[0])
        else:
            body = {"model_name": self.spec.model or "kling-v2-master", "prompt": req.prompt,
                    "mode": self.spec.opt("mode", "std"),
                    "duration": snap_choice(req.duration or 5, self.spec.opt("allowed_seconds", "5,10"))}
            if req.images:
                path = "/v1/videos/image2video"
                body["image"] = self._img(req.images[0])
                if req.last_frame:
                    body["image_tail"] = self._img(req.last_frame)
            else:
                path = "/v1/videos/text2video"
                body["aspect_ratio"] = req.ratio()
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        resp = await self.http.post_json(self.url(path), self.body(body, req), headers=self._auth(),
                                         timeout=self.spec.submit_timeout_s or None)
        self._check(resp)
        tid = dig(resp, "data.task_id")
        if not tid:
            raise GenError(f"提交成功但没有 task_id: {str(resp)[:200]}", code="no_id")
        return {"id": tid, "path": path}

    @staticmethod
    def _check(resp) -> None:
        code = resp.get("code") if isinstance(resp, dict) else None
        if code not in (0, None, "0"):
            # 1102 余额不足 / 1301 内容安全 / 1303 并发超限 … 1303 属于可重试
            raise GenError(f"可灵错误 {code}: {resp.get('message')}", retryable=code in (1302, 1303, 5000, 5001),
                           user_error=code in (1102, 1301, 1201), code=str(code))

    async def poll(self, handle: dict) -> PollResult:
        resp = await self.http.get_json(self.url(f"{handle['path']}/{handle['id']}"), headers=self._auth(), timeout=30)
        self._check(resp)
        data = resp.get("data") or {}
        status = str(data.get("task_status") or "").lower()
        if status == "succeed":
            res = data.get("task_result") or {}
            assets = [Asset(VIDEO, url=v["url"], ext=".mp4") for v in res.get("videos") or [] if v.get("url")]
            assets += [Asset(IMAGE, url=i["url"]) for i in res.get("images") or [] if i.get("url")]
            return PollResult("succeeded", 100, assets)
        if status == "failed":
            msg = data.get("task_status_msg") or "failed"
            return PollResult("failed", error=msg, user_error="安全" in msg or "risk" in msg.lower())
        return PollResult("running" if status == "processing" else "pending")

    async def probe(self) -> str:
        resp = await self.http.get_json(self.url("/v1/videos/text2video?pageNum=1&pageSize=1"), headers=self._auth(),
                                        timeout=20, retries=0)
        self._check(resp)
        return "接口可达，AK/SK 有效"


# ═══════════════════════ MiniMax 海螺 ═══════════════════════

class MiniMax(Adapter):
    provider = "minimax"
    label = "MiniMax 海螺"
    kinds = (IMAGE, VIDEO)
    default_base_url = "https://api.minimaxi.com/v1"
    poll_interval = 10
    doc = "视频三步：POST /video_generation → GET /query/video_generation?task_id= → GET /files/retrieve?file_id=（download_url）；国际站 https://api.minimax.io/v1"
    fields = {
        "resolution": ("视频分辨率 768P / 1080P（留空按模型默认）", ""),
        "allowed_seconds": ("允许的时长档位", "6,10"),
        "prompt_optimizer": ("是否让模型自动优化提示词", True),
    }

    @staticmethod
    def _check(resp) -> None:
        br = resp.get("base_resp") if isinstance(resp, dict) else None
        code = (br or {}).get("status_code", 0)
        if code not in (0, None):
            # 1002 限流 / 1008 余额不足 / 1026 敏感内容 / 2013 参数错误
            raise GenError(f"MiniMax 错误 {code}: {br.get('status_msg')}", retryable=code in (1002, 1039),
                           user_error=code in (1008, 1026, 1027, 2013), code=str(code))

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        self.require_key()
        if req.kind == IMAGE:
            body = {"model": self.spec.model or "image-01", "prompt": req.prompt, "n": max(1, req.n),
                    "response_format": "url", "aspect_ratio": req.ratio("1:1")}
            if req.images:
                body["subject_reference"] = [{"type": "character", "image_file": req.images[0].url_or_data()}]
            resp = await self.http.post_json(self.url("/image_generation"), self.body(body, req), headers=self.bearer(),
                                             timeout=self.spec.submit_timeout_s or 180)
            self._check(resp)
            urls = dig(resp, "data.image_urls") or []
            b64s = dig(resp, "data.image_base64") or []
            return self.sync_handle([Asset(IMAGE, url=u) for u in urls] + [Asset(IMAGE, data=b) for b in b64s])

        body = {"model": self.spec.model or "MiniMax-Hailuo-02", "prompt": req.prompt,
                "prompt_optimizer": bool(self.spec.opt("prompt_optimizer", True))}
        if req.duration:
            body["duration"] = int(snap_choice(req.duration, self.spec.opt("allowed_seconds", "6,10")))
        if res := self.spec.opt("resolution"):
            body["resolution"] = res
        if req.images:
            body["first_frame_image"] = req.images[0].url_or_data()
        if req.last_frame:
            body["last_frame_image"] = req.last_frame.url_or_data()
        resp = await self.http.post_json(self.url("/video_generation"), self.body(body, req), headers=self.bearer(),
                                         timeout=self.spec.submit_timeout_s or None)
        self._check(resp)
        if not resp.get("task_id"):
            raise GenError(f"提交成功但没有 task_id: {str(resp)[:200]}", code="no_id")
        return {"id": resp["task_id"]}

    async def poll(self, handle: dict) -> PollResult:
        if handle.get("sync"):
            return await super().poll(handle)
        resp = await self.http.get_json(self.url("/query/video_generation"), params={"task_id": handle["id"]},
                                        headers=self.bearer(), timeout=30)
        self._check(resp)
        status = str(resp.get("status") or "").lower()
        if status == "success":
            fid = resp.get("file_id")
            info = await self.http.get_json(self.url("/files/retrieve"), params={"file_id": fid},
                                            headers=self.bearer(), timeout=30)
            self._check(info)
            url = dig(info, "file.download_url")
            if not url:
                raise GenError(f"任务成功但取不到下载地址: {str(info)[:200]}", retryable=True)
            return PollResult("succeeded", 100, [Asset(VIDEO, url=url, ext=".mp4")])
        if status == "fail":
            return PollResult("failed", error=str(resp.get("error_message") or "fail"))
        return PollResult("running" if status == "processing" else "pending")

    async def probe(self) -> str:
        self.require_key()
        resp = await self.http.get_json(self.url("/query/video_generation"), params={"task_id": "0"},
                                        headers=self.bearer(), timeout=20, retries=0)
        br = (resp or {}).get("base_resp") or {}
        if br.get("status_code") in (1004, 2049):
            raise GenError(f"鉴权失败：{br.get('status_msg')}", user_error=True)
        return f"接口可达（{br.get('status_msg') or 'ok'}）"
