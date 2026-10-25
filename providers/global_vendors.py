"""海外/聚合平台原生协议：Google Gemini（Nano Banana / Imagen / Veo）、Replicate、fal.ai。"""

from __future__ import annotations

from ..core.types import IMAGE, VIDEO, Asset, GenError, GenRequest, PollResult, dig
from .base import Adapter, collect_media


# ═══════════════════════ Google Gemini API ═══════════════════════

class Gemini(Adapter):
    """
    - Gemini 出图（gemini-*-image，俗称 Nano Banana）：POST /models/{m}:generateContent，responseModalities 含 IMAGE
    - Imagen：POST /models/{m}:predict → predictions[].bytesBase64Encoded
    - Veo：POST /models/{m}:predictLongRunning → operation name → GET /{name}，done 后
      response.generateVideoResponse.generatedSamples[0].video.uri（下载需带 key、跟随重定向）
    """

    provider = "gemini"
    label = "Google Gemini / Imagen / Veo"
    kinds = (IMAGE, VIDEO)
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    poll_interval = 10
    doc = "Gemini 原生 REST（也适用于支持 /v1beta 原生路径的中转，如 new-api）"
    fields = {
        "resolution": ("Veo 分辨率 720p / 1080p（留空按默认）", ""),
        "auth": ("鉴权方式：header（x-goog-api-key）/ query（?key=）/ bearer（中转常用）", "header"),
    }

    def _h(self) -> dict:
        self.require_key()
        mode = self.spec.opt("auth", "header")
        if mode == "bearer":
            return self.bearer()
        if mode == "query":
            return {"Content-Type": "application/json"}
        return {"Content-Type": "application/json", "x-goog-api-key": self.spec.api_key}

    def _q(self) -> dict | None:
        return {"key": self.spec.api_key} if self.spec.opt("auth", "header") == "query" else None

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        model = self.spec.model
        if req.kind == VIDEO:
            inst: dict = {"prompt": req.prompt}
            if req.images:
                inst["image"] = {"bytesBase64Encoded": req.images[0].b64(), "mimeType": req.images[0].mime}
            if req.last_frame:
                inst["lastFrame"] = {"bytesBase64Encoded": req.last_frame.b64(), "mimeType": req.last_frame.mime}
            params: dict = {"aspectRatio": req.ratio()}
            if req.duration:
                params["durationSeconds"] = int(round(req.duration))
            if req.negative_prompt:
                params["negativePrompt"] = req.negative_prompt
            if res := self.spec.opt("resolution"):
                params["resolution"] = res
            body = self.body({"instances": [inst], "parameters": params}, req)
            resp = await self.http.post_json(self.url(f"/models/{model}:predictLongRunning"), body, headers=self._h(),
                                             params=self._q(), timeout=self.spec.submit_timeout_s or None)
            if not resp.get("name"):
                raise GenError(f"提交成功但没有 operation name: {str(resp)[:200]}", code="no_id")
            return {"id": resp["name"]}

        if model.startswith("imagen"):
            params = {"sampleCount": max(1, req.n), "aspectRatio": req.ratio("1:1")}
            body = self.body({"instances": [{"prompt": req.prompt}], "parameters": params}, req)
            resp = await self.http.post_json(self.url(f"/models/{model}:predict"), body, headers=self._h(),
                                             params=self._q(), timeout=self.spec.submit_timeout_s or 180)
            assets = [Asset(IMAGE, data=p["bytesBase64Encoded"]) for p in resp.get("predictions") or []
                      if p.get("bytesBase64Encoded")]
            return self.sync_handle(assets)

        parts: list[dict] = [{"text": req.prompt}]
        for r in req.images:
            parts.append({"inline_data": {"mime_type": r.mime, "data": r.b64()}})
        gen: dict = {"responseModalities": ["TEXT", "IMAGE"]}
        if req.aspect_ratio or req.size:
            gen["imageConfig"] = {"aspectRatio": req.ratio("1:1")}
        body = self.body({"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}, req)
        resp = await self.http.post_json(self.url(f"/models/{model}:generateContent"), body, headers=self._h(),
                                         params=self._q(), timeout=self.spec.submit_timeout_s or 180)
        assets, text = [], ""
        for p in dig(resp, "candidates.0.content.parts", []) or []:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                assets.append(Asset(IMAGE, data=inline["data"]))
            text += p.get("text", "")
        if not assets:
            reason = dig(resp, "candidates.0.finishReason") or dig(resp, "promptFeedback.blockReason") or ""
            raise GenError(f"Gemini 没有返回图片（{reason}）：{text[:150]}", user_error=True, code="no_image")
        return self.sync_handle(assets)

    async def poll(self, handle: dict) -> PollResult:
        if handle.get("sync"):
            return await super().poll(handle)
        resp = await self.http.get_json(self.url(f"/{handle['id']}"), headers=self._h(), params=self._q(), timeout=30)
        if not resp.get("done"):
            return PollResult("running")
        if err := resp.get("error"):
            return PollResult("failed", error=str(err.get("message") or err)[:300])
        samples = dig(resp, "response.generateVideoResponse.generatedSamples") or \
            dig(resp, "response.generatedVideos") or []
        assets = []
        for s in samples:
            uri = dig(s, "video.uri")
            if uri:
                headers = {"x-goog-api-key": self.spec.api_key} if self.spec.opt("auth", "header") == "header" else {}
                if self.spec.opt("auth") == "query":
                    uri += ("&" if "?" in uri else "?") + f"key={self.spec.api_key}"
                assets.append(Asset(VIDEO, url=uri, headers=headers, ext=".mp4"))
            elif b := dig(s, "video.bytesBase64Encoded") or dig(s, "video.videoBytes"):
                assets.append(Asset(VIDEO, data=b, ext=".mp4"))
        if not assets:
            filtered = dig(resp, "response.generateVideoResponse.raiMediaFilteredReasons")
            return PollResult("failed", error=f"没有产物{('：' + str(filtered)) if filtered else ''}",
                              user_error=bool(filtered))
        return PollResult("succeeded", 100, assets)

    async def probe(self) -> str:
        resp = await self.http.get_json(self.url("/models"), headers=self._h(), params=self._q(), timeout=20, retries=1)
        names = [m.get("name", "").split("/")[-1] for m in resp.get("models") or []]
        if self.spec.model and names and self.spec.model not in names:
            return f"接口可达，但模型列表里没有 {self.spec.model}"
        return f"接口可达（{len(names)} 个模型）"


# ═══════════════════════ Replicate ═══════════════════════

class Replicate(Adapter):
    provider = "replicate"
    label = "Replicate"
    kinds = (IMAGE, VIDEO)
    default_base_url = "https://api.replicate.com/v1"
    poll_interval = 5
    doc = "model 填 owner/name（官方模型）或 owner/name:version；status ∈ starting/processing/succeeded/failed/canceled"
    fields = {
        "image_key": ("参考图/首帧传给哪个 input 字段（各模型不同，如 image / start_image / first_frame_image）", "image"),
        "last_frame_key": ("尾帧字段名", "last_frame_image"),
        "wait_seconds": ("提交时 Prefer: wait=N 同步等待秒数（0=不等，直接轮询）", 0),
    }

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        self.require_key()
        inp: dict = {"prompt": req.prompt}
        if req.negative_prompt:
            inp["negative_prompt"] = req.negative_prompt
        if req.aspect_ratio or req.size:
            inp["aspect_ratio"] = req.ratio()
        if req.duration and req.kind == VIDEO:
            inp["duration"] = int(round(req.duration))
        if req.seed is not None:
            inp["seed"] = req.seed
        if req.n > 1:
            inp["num_outputs"] = req.n
        if req.images:
            inp[self.spec.opt("image_key", "image")] = req.images[0].url_or_data()
        if req.last_frame:
            inp[self.spec.opt("last_frame_key", "last_frame_image")] = req.last_frame.url_or_data()
        inp = self.body(inp, req)
        headers = self.bearer()
        if (w := int(self.spec.opt("wait_seconds", 0) or 0)) > 0:
            headers["Prefer"] = f"wait={min(w, 60)}"
        model = self.spec.model
        if ":" in model:
            url, body = self.url("/predictions"), {"version": model.split(":", 1)[1], "input": inp}
        else:
            url, body = self.url(f"/models/{model}/predictions"), {"input": inp}
        resp = await self.http.post_json(url, body, headers=headers, timeout=self.spec.submit_timeout_s or None)
        if not resp.get("id"):
            raise GenError(f"提交成功但没有 prediction id: {str(resp)[:200]}", code="no_id")
        return {"id": resp["id"], "kind": req.kind, "first": resp if resp.get("status") == "succeeded" else None}

    async def poll(self, handle: dict) -> PollResult:
        resp = handle.pop("first", None) or await self.http.get_json(self.url(f"/predictions/{handle['id']}"),
                                                                     headers=self.bearer(), timeout=30)
        status = resp.get("status")
        if status == "succeeded":
            out = resp.get("output")
            kind = handle.get("kind", IMAGE)
            urls = [out] if isinstance(out, str) else (out if isinstance(out, list) else [])
            assets = [Asset(kind, url=u) for u in urls if isinstance(u, str) and u.startswith("http")]
            return PollResult("succeeded", 100, assets or collect_media(out, kind))
        if status in ("failed", "canceled"):
            err = str(resp.get("error") or status)
            return PollResult("failed", error=err[:300], user_error="nsfw" in err.lower() or "sensitive" in err.lower())
        return PollResult("running" if status == "processing" else "pending")

    async def cancel(self, handle: dict) -> None:
        try:
            await self.http.request("POST", self.url(f"/predictions/{handle['id']}/cancel"), headers=self.bearer(),
                                    timeout=20, retries=0)
        except GenError:
            pass

    async def probe(self) -> str:
        self.require_key()
        await self.http.get_json(self.url("/account"), headers=self.bearer(), timeout=20, retries=1)
        return "接口可达，Token 有效"


# ═══════════════════════ fal.ai ═══════════════════════

class Fal(Adapter):
    provider = "fal"
    label = "fal.ai 队列接口"
    kinds = (IMAGE, VIDEO)
    default_base_url = "https://queue.fal.run"
    poll_interval = 5
    doc = "POST {base}/{model} → request_id/status_url/response_url；status ∈ IN_QUEUE/IN_PROGRESS/COMPLETED"
    fields = {
        "image_key": ("参考图/首帧字段名（多数模型是 image_url）", "image_url"),
        "last_frame_key": ("尾帧字段名", "end_image_url"),
    }

    def _h(self) -> dict:
        self.require_key()
        return {"Content-Type": "application/json", "Authorization": f"Key {self.spec.api_key}"}

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        body: dict = {"prompt": req.prompt}
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        if req.aspect_ratio or req.size:
            body["aspect_ratio"] = req.ratio()
        if req.duration and req.kind == VIDEO:
            body["duration"] = str(int(round(req.duration)))
        if req.seed is not None:
            body["seed"] = req.seed
        if req.n > 1:
            body["num_images"] = req.n
        if req.images:
            body[self.spec.opt("image_key", "image_url")] = req.images[0].url_or_data()
        if req.last_frame:
            body[self.spec.opt("last_frame_key", "end_image_url")] = req.last_frame.url_or_data()
        resp = await self.http.post_json(self.url(f"/{self.spec.model}"), self.body(body, req), headers=self._h(),
                                         timeout=self.spec.submit_timeout_s or None)
        rid = resp.get("request_id")
        if not rid:
            raise GenError(f"提交成功但没有 request_id: {str(resp)[:200]}", code="no_id")
        app = "/".join(self.spec.model.split("/")[:2])  # 状态接口只用 owner/app 前两段
        return {"id": rid, "kind": req.kind,
                "status_url": resp.get("status_url") or self.url(f"/{app}/requests/{rid}/status"),
                "response_url": resp.get("response_url") or self.url(f"/{app}/requests/{rid}"),
                "cancel_url": resp.get("cancel_url") or self.url(f"/{app}/requests/{rid}/cancel")}

    async def poll(self, handle: dict) -> PollResult:
        st = await self.http.get_json(handle["status_url"], headers=self._h(), timeout=30)
        status = str(st.get("status") or "").upper()
        if status != "COMPLETED":
            return PollResult("running" if status == "IN_PROGRESS" else "pending")
        try:
            res = await self.http.get_json(handle["response_url"], headers=self._h(), timeout=60)
        except GenError as e:
            if e.status and 400 <= e.status < 500:  # fal 把模型报错放在结果接口的 4xx 里
                return PollResult("failed", error=str(e)[:300], user_error=e.status == 422)
            raise
        kind = handle.get("kind", IMAGE)
        assets = [Asset(IMAGE, url=i["url"]) for i in res.get("images") or [] if isinstance(i, dict) and i.get("url")]
        if url := dig(res, "video.url"):
            assets.append(Asset(VIDEO, url=url, ext=".mp4"))
        return PollResult("succeeded", 100, assets or collect_media(res, kind))

    async def cancel(self, handle: dict) -> None:
        try:
            await self.http.request("PUT", handle["cancel_url"], headers=self._h(), timeout=20, retries=0)
        except GenError:
            pass

    async def probe(self) -> str:
        try:
            await self.http.get_json(self.url(f"/{self.spec.model}/requests/00000000/status"), headers=self._h(),
                                     timeout=20, retries=0)
        except GenError as e:
            if e.status in (401, 403):
                raise
            return f"接口可达（返回 {e.status}）"
        return "接口可达"
