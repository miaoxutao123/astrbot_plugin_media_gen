"""阿里云百炼 DashScope 原生协议：通义万相（wan 系列）文/图生视频、文生图，千问 qwen-image。

- 视频：POST /services/aigc/video-generation/video-synthesis（Header X-DashScope-Async: enable）
        → output.task_id → GET /tasks/{id}，output.task_status ∈ PENDING/RUNNING/SUCCEEDED/FAILED/CANCELED/UNKNOWN
        → output.video_url（24 小时有效）
- 千问文生图：POST /services/aigc/multimodal-generation/generation（同步，messages 格式）
        → output.choices[0].message.content[].image
- 万相文生图：POST /services/aigc/text2image/image-synthesis（异步）→ output.results[].url
  有参考图时改走 /services/aigc/image2image/image-synthesis（input.images）
"""

from __future__ import annotations

from ..core.types import IMAGE, VIDEO, Asset, GenError, GenRequest, PollResult, dig
from .base import Adapter, collect_media


class DashScope(Adapter):
    provider = "dashscope"
    label = "阿里云百炼 DashScope"
    kinds = (IMAGE, VIDEO)
    default_base_url = "https://dashscope.aliyuncs.com/api/v1"
    poll_interval = 8
    doc = "百炼原生协议（万相视频/文生图、千问 qwen-image）。base_url 可换成 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1 或新加坡 dashscope-intl"
    fields = {
        "image_api": ("文生图接口：auto（qwen-image 走同步 multimodal，其余走异步 text2image）/ multimodal / text2image", "auto"),
        "resolution": ("图生视频分辨率档位，如 480P / 720P / 1080P（留空不传）", ""),
        "prompt_extend": ("是否让百炼自动扩写提示词", False),
        "watermark": ("是否加水印", False),
    }

    def _headers(self, async_: bool) -> dict:
        self.require_key()
        h = self.bearer()
        if async_:
            h["X-DashScope-Async"] = "enable"
        return h

    def _params(self, req: GenRequest) -> dict:
        p: dict = {"watermark": bool(self.spec.opt("watermark", False)),
                   "prompt_extend": bool(self.spec.opt("prompt_extend", False))}
        if req.seed is not None:
            p["seed"] = req.seed
        return p

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        if req.kind == VIDEO:
            return await self._video(req)
        api = self.spec.opt("image_api", "auto")
        if api == "multimodal" or (api == "auto" and self.spec.model.startswith("qwen-image")):
            return await self._qwen_image(req)
        return await self._wan_image(req)

    async def _video(self, req: GenRequest) -> dict:
        inp: dict = {"prompt": req.prompt}
        if req.negative_prompt:
            inp["negative_prompt"] = req.negative_prompt
        params = self._params(req)
        if req.duration:
            params["duration"] = int(round(req.duration))
        if req.audio is not None:
            params["audio"] = req.audio
        if req.images and req.last_frame:  # 首尾帧（kf2v）
            inp["first_frame_url"] = req.images[0].url_or_data()
            inp["last_frame_url"] = req.last_frame.url_or_data()
        elif req.images:  # 图生视频（i2v）
            inp["img_url"] = req.images[0].url_or_data()
        if req.images and (res := self.spec.opt("resolution")):
            params["resolution"] = res
        elif not req.images and req.size:
            w, h = req.width_height()
            params["size"] = f"{w}*{h}"
        body = self.body({"model": self.spec.model, "input": inp, "parameters": params}, req)
        resp = await self.http.post_json(self.url("/services/aigc/video-generation/video-synthesis"), body,
                                         headers=self._headers(True), timeout=self.spec.submit_timeout_s or None)
        return self._task(resp)

    async def _qwen_image(self, req: GenRequest) -> dict:
        content = [{"image": r.url_or_data()} for r in req.images] + [{"text": req.prompt}]
        params = self._params(req)
        params["n"] = max(1, req.n)
        if req.size or req.aspect_ratio:
            w, h = req.width_height()
            params["size"] = f"{w}*{h}"
        if req.negative_prompt:
            params["negative_prompt"] = req.negative_prompt
        body = self.body({"model": self.spec.model, "input": {"messages": [{"role": "user", "content": content}]},
                          "parameters": params}, req)
        resp = await self.http.post_json(self.url("/services/aigc/multimodal-generation/generation"), body,
                                         headers=self._headers(False), timeout=self.spec.submit_timeout_s or 180)
        self._raise(resp)
        return self.sync_handle(self._assets(resp, IMAGE))

    async def _wan_image(self, req: GenRequest) -> dict:
        inp: dict = {"prompt": req.prompt}
        if req.negative_prompt:
            inp["negative_prompt"] = req.negative_prompt
        params = self._params(req)
        params["n"] = max(1, req.n)
        if req.size or req.aspect_ratio:
            w, h = req.width_height()
            params["size"] = f"{w}*{h}"
        path = "/services/aigc/text2image/image-synthesis"
        if req.images:
            inp["images"] = [r.url_or_data() for r in req.images]
            path = "/services/aigc/image2image/image-synthesis"
        body = self.body({"model": self.spec.model, "input": inp, "parameters": params}, req)
        resp = await self.http.post_json(self.url(path), body, headers=self._headers(True),
                                         timeout=self.spec.submit_timeout_s or None)
        return self._task(resp)

    def _raise(self, resp) -> None:
        if isinstance(resp, dict) and resp.get("code") and not resp.get("output"):
            raise GenError(f"{resp.get('code')}: {resp.get('message')}", user_error=True, code=str(resp.get("code")))

    def _task(self, resp) -> dict:
        self._raise(resp)
        tid = dig(resp, "output.task_id")
        if not tid:
            raise GenError(f"提交成功但没有 task_id: {str(resp)[:200]}", code="no_id")
        return {"id": tid}

    @staticmethod
    def _assets(resp, kind: str) -> list[Asset]:
        out = dig(resp, "output", {}) or {}
        if url := out.get("video_url"):
            return [Asset(VIDEO, url=url, ext=".mp4")]
        assets = [Asset(kind, url=r["url"]) for r in out.get("results") or [] if isinstance(r, dict) and r.get("url")]
        for ch in out.get("choices") or []:
            for c in dig(ch, "message.content", []) or []:
                if isinstance(c, dict) and c.get("image"):
                    assets.append(Asset(IMAGE, url=c["image"]))
        return assets or collect_media(out, kind)

    async def poll(self, handle: dict) -> PollResult:
        if handle.get("sync"):
            return await super().poll(handle)
        resp = await self.http.get_json(self.url(f"/tasks/{handle['id']}"), headers=self._headers(False), timeout=30)
        status = str(dig(resp, "output.task_status", "")).upper()
        if status == "SUCCEEDED":
            kind = VIDEO if dig(resp, "output.video_url") else IMAGE
            return PollResult("succeeded", 100, self._assets(resp, kind))
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            code = dig(resp, "output.code", "") or ""
            msg = dig(resp, "output.message", "") or status
            user = any(k in f"{code}{msg}".lower() for k in ("datainspection", "inappropriate", "invalidparameter"))
            return PollResult("failed", error=f"{code} {msg}".strip(), user_error=user)
        return PollResult("running" if status == "RUNNING" else "pending")

    async def cancel(self, handle: dict) -> None:
        if handle.get("id"):
            try:
                await self.http.request("POST", self.url(f"/tasks/{handle['id']}/cancel"),
                                        headers=self._headers(False), timeout=20, retries=0)
            except GenError:
                pass

    async def probe(self) -> str:
        try:
            await self.http.get_json(self.url("/tasks/00000000-0000-0000-0000-000000000000"),
                                     headers=self._headers(False), timeout=20, retries=0)
        except GenError as e:
            if e.status in (401, 403):
                raise GenError(f"鉴权失败：{e}", user_error=True) from e
            return f"接口可达（返回 {e.status}）"
        return "接口可达，API Key 有效"
