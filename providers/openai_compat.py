"""OpenAI 系：图片 /images/generations + /images/edits、聊天出图 /chat/completions、Sora /videos。

兼容面最广的一类：OpenAI 官方、Azure 风格中转、new-api / one-api、硅基流动、ModelScope、
火山方舟 Seedream（/api/v3/images/generations）、即梦反代（jimeng-api）、OpenRouter 聊天出图等。
"""

from __future__ import annotations

import re

import aiohttp

from ..core.types import IMAGE, VIDEO, Asset, GenError, GenRequest, PollResult, dig, parse_size
from .base import Adapter, collect_media

_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_DATA_URL = re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+")
_HTTP_IMG = re.compile(r"https?://[^\s)\"'<>]+\.(?:png|jpe?g|webp|gif)(?:\?[^\s)\"'<>]*)?", re.I)


def snap_choice(value: float, choices: str) -> str:
    """把时长贴到接口允许的档位（如 Sora 4/8/12 秒）。"""
    opts = [float(x) for x in str(choices).replace("，", ",").split(",") if x.strip()]
    if not opts:
        return str(int(value)) if value else ""
    best = min(opts, key=lambda x: abs(x - (value or opts[0])))
    return str(int(best)) if best == int(best) else str(best)


def parse_image_response(resp, kind: str = IMAGE) -> list[Asset]:
    assets: list[Asset] = []
    for item in (resp.get("data") or resp.get("images") or []) if isinstance(resp, dict) else []:
        if isinstance(item, str):
            assets.append(Asset(kind, url=item) if item.startswith("http") else Asset(kind, data=item))
        elif isinstance(item, dict):
            if item.get("b64_json"):
                assets.append(Asset(kind, data=item["b64_json"]))
            elif item.get("url"):
                assets.append(Asset(kind, url=item["url"]))
    return assets or collect_media(resp, kind)


class OpenAIImage(Adapter):
    provider = "openai_image"
    label = "OpenAI 兼容图片接口"
    kinds = (IMAGE, VIDEO)  # VIDEO：同步视频接口（如即梦反代 /videos/generations）
    default_base_url = "https://api.openai.com/v1"
    resumable = False
    doc = "POST {base}/images/generations（同步）。参考图：edits=multipart /images/edits；json_image=在 body 里带 image 字段（方舟 Seedream、多数中转）；chat=走 /chat/completions 聊天出图"
    fields = {
        "path": ("生成接口路径（视频同步接口改成 /videos/generations）", "/images/generations"),
        "ref_mode": ("参考图传法：edits / json_image / chat / none", "edits"),
        "response_format": ("url 或 b64_json；留空则不传（gpt-image 系列不接受此参数）", ""),
        "size_format": ("尺寸写法：wxh / w*h / ratio / none", "wxh"),
        "image_field": ("json_image 模式下参考图字段名", "image"),
    }

    def _size(self, req: GenRequest):
        fmt = self.spec.opt("size_format", "wxh")
        if fmt == "none" or not (req.size or req.aspect_ratio):
            return None
        if fmt == "ratio":
            return req.ratio("1:1")
        w, h = req.width_height()
        return f"{w}*{h}" if fmt == "w*h" else f"{w}x{h}"

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        mode = self.spec.opt("ref_mode", "edits")
        if mode == "chat":
            return self.sync_handle(await self._chat(req))
        body = {"model": self.spec.model, "prompt": req.prompt}
        if req.n > 1:
            body["n"] = req.n
        if (size := self._size(req)) is not None:
            body["size"] = size
        if fmt := self.spec.opt("response_format"):
            body["response_format"] = fmt
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            body["seed"] = req.seed
        if req.kind == VIDEO and req.duration:
            body["duration"] = int(req.duration)
        refs = list(req.images) + ([req.last_frame] if req.last_frame else [])
        if refs and mode == "edits" and req.kind == IMAGE:
            resp = await self._edits(req, body, refs)
        else:
            if refs and mode in ("json_image", "edits"):
                field = self.spec.opt("image_field", "image")
                urls = [r.url_or_data() for r in refs]
                body[field] = urls[0] if len(urls) == 1 else urls
            body = self.body(body, req)
            resp = await self.http.post_json(self.url(self.spec.opt("path", "/images/generations")), body,
                                             headers=self.bearer(), timeout=self.spec.submit_timeout_s or None)
        if isinstance(resp, dict) and resp.get("error"):
            raise GenError(str(resp["error"])[:300], user_error=True)
        return self.sync_handle(parse_image_response(resp, req.kind))

    async def _edits(self, req: GenRequest, body: dict, refs) -> dict:
        body = self.body(body, req)

        def form():
            fd = aiohttp.FormData()
            for k, v in body.items():
                fd.add_field(k, str(v))
            key = "image[]" if len(refs) > 1 else "image"
            for r in refs:
                fd.add_field(key, r.read(), filename=r.name, content_type=r.mime)
            return fd

        headers = {k: v for k, v in self.bearer().items() if k != "Content-Type"}
        return await self.http.request("POST", self.url("/images/edits"), data=form, headers=headers,
                                       idempotent=False, timeout=self.spec.submit_timeout_s or None)

    async def _chat(self, req: GenRequest) -> list[Asset]:
        content = [{"type": "text", "text": req.prompt}]
        for r in req.images:
            content.append({"type": "image_url", "image_url": {"url": r.url_or_data()}})
        body = self.body({"model": self.spec.model, "messages": [{"role": "user", "content": content}],
                          "stream": False}, req)
        resp = await self.http.post_json(self.url("/chat/completions"), body, headers=self.bearer(),
                                         timeout=self.spec.submit_timeout_s or None)
        msg = dig(resp, "choices.0.message", {}) or {}
        assets = [Asset(IMAGE, url=u) if u.startswith("http") else Asset(IMAGE, data=u)
                  for u in (dig(i, "image_url.url") or "" for i in msg.get("images") or []) if u]
        raw, text = msg.get("content"), ""
        if isinstance(raw, list):  # content parts
            for part in raw:
                if not isinstance(part, dict):
                    continue
                if u := dig(part, "image_url.url"):
                    assets.append(Asset(IMAGE, url=u) if u.startswith("http") else Asset(IMAGE, data=u))
                elif part.get("type") == "text":
                    text += part.get("text", "")
        elif isinstance(raw, str):
            text = raw
        for m in _MD_IMG.findall(text) + _DATA_URL.findall(text) + _HTTP_IMG.findall(text):
            assets.append(Asset(IMAGE, data=m) if m.startswith("data:") else Asset(IMAGE, url=m))
        seen, out = set(), []
        for a in assets:
            k = a.url or a.data[:80]
            if k not in seen:
                seen.add(k)
                out.append(a)
        if not out:
            raise GenError(f"聊天接口没有返回图片：{text[:150]}", retryable=False, user_error=True, code="no_image")
        return out

    async def probe(self) -> str:
        try:
            resp = await self.http.get_json(self.url("/models"), headers=self.bearer(), timeout=20, retries=1)
        except GenError as e:
            if e.status in (404, 405):  # 很多中转/反代没有 /models
                return f"服务可达（没有 /models 列表接口，返回 {e.status}），请用 test 实际生成验证"
            raise
        ids = [m.get("id") for m in (resp.get("data") or [])] if isinstance(resp, dict) else []
        if ids and self.spec.model and self.spec.model not in ids:
            return f"接口可达，但模型列表里没有 {self.spec.model}（共 {len(ids)} 个模型，部分中转站列表不全，可直接 test）"
        return f"接口可达（{len(ids)} 个模型）"


class OpenAIVideo(Adapter):
    """OpenAI Videos API（Sora 2）：POST /videos → GET /videos/{id} → GET /videos/{id}/content。"""

    provider = "openai_video"
    label = "OpenAI Sora 视频接口"
    kinds = (VIDEO,)
    default_base_url = "https://api.openai.com/v1"
    poll_interval = 10
    doc = "POST {base}/videos，轮询 GET /videos/{id}，下载 /videos/{id}/content。new-api 等中转的 Sora 格式同样适用"
    fields = {
        "allowed_seconds": ("接口允许的时长档位（逗号分隔）", "4,8,12"),
        "content_path": ("下载路径模板", "/videos/{id}/content"),
    }

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        body = {"model": self.spec.model or "sora-2", "prompt": req.prompt}
        if secs := snap_choice(req.duration, self.spec.opt("allowed_seconds", "4,8,12")):
            body["seconds"] = secs
        wh = parse_size(req.size)
        if wh:
            body["size"] = f"{wh[0]}x{wh[1]}"
        body = self.body(body, req)
        if req.images:
            ref = req.images[0]

            def form():
                fd = aiohttp.FormData()
                for k, v in body.items():
                    fd.add_field(k, str(v))
                fd.add_field("input_reference", ref.read(), filename=ref.name, content_type=ref.mime)
                return fd

            headers = {k: v for k, v in self.bearer().items() if k != "Content-Type"}
            resp = await self.http.request("POST", self.url("/videos"), data=form, headers=headers, idempotent=False,
                                           timeout=self.spec.submit_timeout_s or None)
        else:
            resp = await self.http.post_json(self.url("/videos"), body, headers=self.bearer(),
                                             timeout=self.spec.submit_timeout_s or None)
        vid = resp.get("id") or resp.get("task_id") if isinstance(resp, dict) else None
        if not vid:
            raise GenError(f"提交成功但没有返回任务 ID: {str(resp)[:200]}", code="no_id")
        return {"id": vid}

    async def poll(self, handle: dict) -> PollResult:
        resp = await self.http.get_json(self.url(f"/videos/{handle['id']}"), headers=self.bearer(), timeout=30)
        status = str(resp.get("status") or "").lower()
        progress = resp.get("progress")
        if status in ("completed", "succeeded", "success"):
            path = self.spec.opt("content_path", "/videos/{id}/content").format(id=handle["id"])
            direct = collect_media(resp, VIDEO)
            asset = direct[0] if direct else Asset(VIDEO, url=self.url(path), ext=".mp4",
                                                   headers={k: v for k, v in self.bearer().items() if k != "Content-Type"})
            return PollResult("succeeded", 100, [asset])
        if status in ("failed", "cancelled", "canceled", "expired"):
            err = resp.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return PollResult("failed", error=msg or status, user_error="moderation" in str(err).lower())
        return PollResult("running" if status == "in_progress" else "pending", progress)

    async def cancel(self, handle: dict) -> None:
        try:
            await self.http.request("DELETE", self.url(f"/videos/{handle['id']}"), headers=self.bearer(),
                                    timeout=20, retries=0)
        except GenError:
            pass

    async def probe(self) -> str:
        try:
            await self.http.get_json(self.url("/videos/probe-nonexistent"), headers=self.bearer(), timeout=20, retries=0)
        except GenError as e:
            if e.status in (401, 403):
                raise
            return f"接口可达（查询不存在的任务返回 {e.status}）"
        return "接口可达"
