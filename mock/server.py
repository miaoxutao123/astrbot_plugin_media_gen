"""多提供商模拟服务器：一个进程同时模拟主流图片/视频生成接口，用来测试插件对不同 server 类型的兼容性。

后端两种：
  fake  —— ffmpeg 生成测试图/测试视频（秒出，不占显卡），用于协议与故障测试
  comfy —— 转发到真实的本机 ComfyUI（Qwen-Image 2.1 出图 / H3 视频），把本地模型“包装”成各家云接口

路径前缀 → 模拟的接口
  /openai     OpenAI images（generations/edits）、chat 出图、Sora /videos
  /dashscope  阿里百炼（万相视频异步、qwen-image 同步、万相文生图异步、/tasks）
  /ark        火山方舟（Seedream 图片、Seedance 视频任务）
  /kling      可灵（JWT 校验、text2video/image2video/images）
  /minimax    MiniMax（video_generation → query → files/retrieve 三段式）
  /gemini     Gemini（generateContent 出图、Imagen predict、Veo predictLongRunning + operations）
  /replicate  Replicate predictions
  /fal        fal.ai 队列
  /siliconflow 硅基流动（images 同步 + video submit/status）
  /newapi     new-api 统一视频格式
  /jimeng     即梦反代（同步阻塞返回）

故障注入：启动参数 --faults "429:0.2,500:0.1,hang:0.05,fail:0.1,stuck:0.05"，
或运行时 POST /_mock/faults {"429":0.3,...}；GET /_mock/stats 看各接口命中次数。
鉴权：所有接口要求 key = sk-mock（可灵 AK/SK = mock-ak / mock-sk）。

运行：
  ~/TEST/comfyui-h3/.venv/bin/python -m mock.server --port 8199 --backend fake
  ~/TEST/comfyui-h3/.venv/bin/python -m mock.server --port 8199 --backend comfy --comfy-url http://127.0.0.1:8188
（在插件目录下执行；需要 aiohttp，ComfyUI 的 venv 里就有）
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import sys
import tempfile
import time
import uuid

from aiohttp import web

KEY = "sk-mock"
KLING_AK, KLING_SK = "mock-ak", "mock-sk"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


# ═══════════════════════ 生成后端 ═══════════════════════

class FakeBackend:
    def __init__(self, image_delay: float = 0.5, video_delay: float = 3.0):
        self.image_delay = image_delay
        self.video_delay = video_delay

    async def _ffmpeg(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-loglevel", "error", *args,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(err.decode()[:300])

    async def generate(self, kind: str, prompt: str, image: bytes | None, size: str, duration: float) -> tuple[bytes, str]:
        w, h = 512, 512
        if size and "x" in size.replace("*", "x"):
            w, h = (int(x) for x in size.replace("*", "x").split("x")[:2])
            w, h = max(64, min(w, 1280)) // 2 * 2, max(64, min(h, 1280)) // 2 * 2
        color = "#" + hashlib.md5(prompt.encode()).hexdigest()[:6]
        with tempfile.TemporaryDirectory() as td:
            if kind == "image":
                await asyncio.sleep(self.image_delay)
                out = os.path.join(td, "o.png")
                await self._ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}", "-frames:v", "1", out)
                ext = ".png"
            else:
                await asyncio.sleep(self.video_delay)
                d = max(1.0, min(duration or 2, 6))
                out = os.path.join(td, "o.mp4")
                await self._ffmpeg("-f", "lavfi", "-i", f"testsrc2=s={min(w, 640)//2*2}x{min(h, 640)//2*2}:d={d}:r=24",
                                   "-f", "lavfi", "-i", f"sine=f=440:d={d}", "-shortest", "-c:v", "libx264",
                                   "-pix_fmt", "yuv420p", "-c:a", "aac", out)
                ext = ".mp4"
            with open(out, "rb") as f:
                return f.read(), ext


class ComfyBackend:
    """用插件自己的 ComfyUI 适配器跑内置工作流（顺便验证适配器）。"""

    def __init__(self, url: str, image_size: str, video_size: str, image_steps: int, video_steps: int):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from astrbot_plugin_media_gen.core.http import Http
        from astrbot_plugin_media_gen.core.types import GenRequest, RefImage
        from astrbot_plugin_media_gen.providers import ModelSpec, build_adapter
        self.GenRequest, self.RefImage = GenRequest, RefImage
        self.http = Http(read_timeout=120)
        self.img = build_adapter(ModelSpec(name="mock-qwen", provider="comfyui", kind="image", base_url=url,
                                           options={"workflow": "builtin:qwen_image", "steps": image_steps}), self.http)
        self.vid = build_adapter(ModelSpec(name="mock-h3", provider="comfyui", kind="video", base_url=url,
                                           options={"workflow": "builtin:h3_video", "steps": video_steps}), self.http)
        self.image_size, self.video_size = image_size, video_size
        self.lock = asyncio.Lock()  # 单卡：一次只跑一个

    async def generate(self, kind: str, prompt: str, image: bytes | None, size: str, duration: float) -> tuple[bytes, str]:
        refs = []
        if image:
            p = os.path.join(tempfile.gettempdir(), f"mock_ref_{uuid.uuid4().hex[:8]}.png")
            with open(p, "wb") as f:
                f.write(image)
            refs = [self.RefImage(path=p)]
        ad = self.img if kind == "image" else self.vid
        # 测试用：尺寸固定为配置值，避免误传 4K 把显卡跑满
        req = self.GenRequest(kind=kind, prompt=prompt, images=refs,
                              size=self.image_size if kind == "image" else self.video_size,
                              duration=min(duration or 5, 5))
        async with self.lock:
            handle = await ad.submit(req)
            while True:
                res = await ad.poll(handle)
                if res.state == "succeeded":
                    break
                if res.state == "failed":
                    raise RuntimeError(res.error)
                await asyncio.sleep(3)
        url = res.assets[0].url
        data = await self.http.request("GET", url, expect="bytes", timeout=300)
        return data, res.assets[0].ext or (".png" if kind == "image" else ".mp4")


# ═══════════════════════ 任务与故障 ═══════════════════════

class Task:
    def __init__(self, kind: str, prompt: str):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.prompt = prompt
        self.state = "pending"
        self.file = ""
        self.error = ""
        self.created = time.time()
        self.cancelled = False


class Mock:
    def __init__(self, backend, faults: dict[str, float], public_url: str):
        self.backend = backend
        self.faults = faults
        self.public = public_url.rstrip("/")
        self.tasks: dict[str, Task] = {}
        self.stats: dict[str, int] = {}
        self.injected = 0
        os.makedirs(OUT, exist_ok=True)

    def roll(self, kind: str) -> bool:
        """faults 里可带 "max": N —— 总共只注入 N 次故障（测试用，结果确定）。"""
        if "max" in self.faults and self.injected >= self.faults["max"]:
            return False
        hit = random.random() < float(self.faults.get(kind, 0) or 0)
        if hit:
            self.injected += 1
        return hit

    def file_url(self, name: str) -> str:
        return f"{self.public}/files/{name}"

    def start(self, kind: str, prompt: str, image: bytes | None = None, size: str = "", duration: float = 0) -> Task:
        t = Task(kind, prompt)
        self.tasks[t.id] = t
        fail, stuck = self.roll("fail"), self.roll("stuck")

        async def run():
            await asyncio.sleep(0.2)
            if stuck:
                return  # 永远 pending，用来测超时
            t.state = "running"
            try:
                if fail:
                    await asyncio.sleep(0.5)
                    raise RuntimeError("mock: 模拟生成失败（内容审核/内部错误）")
                data, ext = await self.backend.generate(kind, prompt, image, size, duration)
                if t.cancelled:
                    return
                name = f"{t.id}{ext}"
                with open(os.path.join(OUT, name), "wb") as f:
                    f.write(data)
                t.file, t.state = name, "succeeded"
            except Exception as e:
                t.error, t.state = str(e)[:300], "failed"

        asyncio.ensure_future(run())
        return t

    async def run_sync(self, kind: str, prompt: str, image: bytes | None = None, size: str = "", duration: float = 0) -> str:
        if self.roll("fail"):
            raise web.HTTPBadRequest(text=json.dumps({"error": {"message": "mock: 内容审核未通过", "code": "content_policy"}}),
                                     content_type="application/json")
        data, ext = await self.backend.generate(kind, prompt, image, size, duration)
        name = f"{uuid.uuid4().hex[:16]}{ext}"
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(data)
        return name


def _b64img(s: str | None) -> bytes | None:
    if not s or not isinstance(s, str) or s.startswith("http"):
        return None
    try:
        return base64.b64decode(s.split(",", 1)[1] if s.startswith("data:") else s)
    except Exception:
        return None


def _jerr(status: int, msg: str, **extra) -> web.Response:
    return web.json_response({"error": {"message": msg, **extra}}, status=status)


def build_app(mock: Mock) -> web.Application:
    routes = web.RouteTableDef()

    @web.middleware
    async def middleware(request: web.Request, handler):
        path = request.path
        if path.startswith(("/_mock", "/files", "/llm")):  # 脚本 LLM 不参与故障注入
            if path.startswith("/llm") and request.headers.get("Authorization") != f"Bearer {KEY}":
                return _jerr(401, "mock: invalid api key")
            return await handler(request)
        prefix = path.split("/")[1]
        mock.stats[prefix] = mock.stats.get(prefix, 0) + 1
        if mock.roll("hang"):
            await asyncio.sleep(600)
        if mock.roll("429"):
            return web.json_response({"error": {"message": "mock: rate limited", "code": "rate_limit"}}, status=429,
                                     headers={"Retry-After": "1"})
        if mock.roll("500"):
            return web.json_response({"error": {"message": "mock: internal error"}}, status=500)
        if mock.roll("503"):
            return web.json_response({"error": {"message": "mock: overloaded"}}, status=503)
        # 鉴权
        if prefix == "kling":
            if not _kling_ok(request.headers.get("Authorization", "")):
                return web.json_response({"code": 1004, "message": "mock: JWT 校验失败"}, status=401)
        elif prefix == "gemini":
            if request.headers.get("x-goog-api-key") != KEY and request.query.get("key") != KEY \
                    and request.headers.get("Authorization") != f"Bearer {KEY}":
                return _jerr(403, "mock: API key not valid")
        elif prefix == "fal":
            if request.headers.get("Authorization") != f"Key {KEY}":
                return _jerr(401, "mock: bad fal key")
        elif request.headers.get("Authorization") != f"Bearer {KEY}":
            return _jerr(401, "mock: invalid api key")
        return await handler(request)

    # ── 通用 ──
    @routes.get("/files/{name}")
    async def files(request):
        p = os.path.join(OUT, os.path.basename(request.match_info["name"]))
        if not os.path.isfile(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    @routes.post("/_mock/faults")
    async def set_faults(request):
        mock.faults = {k: float(v) for k, v in (await request.json()).items()}
        mock.injected = 0
        return web.json_response(mock.faults)

    @routes.get("/_mock/stats")
    async def stats(request):
        states = {}
        for t in mock.tasks.values():
            states[t.state] = states.get(t.state, 0) + 1
        return web.json_response({"hits": mock.stats, "tasks": states, "faults": mock.faults})

    # ── OpenAI ──
    @routes.get("/openai/v1/models")
    async def oa_models(request):
        return web.json_response({"data": [{"id": m} for m in ("gpt-image-1", "dall-e-3", "sora-2", "gemini-image")]})

    @routes.post("/openai/v1/images/generations")
    async def oa_gen(request):
        b = await request.json()
        if not b.get("prompt"):
            return _jerr(400, "prompt is required")
        img = _b64img(b["image"][0] if isinstance(b.get("image"), list) else b.get("image"))
        n = int(b.get("n") or 1)
        names = [await mock.run_sync("image", b["prompt"], img, str(b.get("size") or "")) for _ in range(n)]
        if b.get("response_format") == "url":
            return web.json_response({"created": int(time.time()), "data": [{"url": mock.file_url(x)} for x in names]})
        data = []
        for x in names:
            with open(os.path.join(OUT, x), "rb") as f:
                data.append({"b64_json": base64.b64encode(f.read()).decode()})
        return web.json_response({"created": int(time.time()), "data": data})

    @routes.post("/openai/v1/images/edits")
    async def oa_edit(request):
        form = await request.post()
        imgs = [v for k, v in form.items() if k in ("image", "image[]")]
        if not imgs:
            return _jerr(400, "image is required")
        name = await mock.run_sync("image", form.get("prompt", ""), imgs[0].file.read(), str(form.get("size") or ""))
        return web.json_response({"data": [{"url": mock.file_url(name)}]})

    @routes.post("/openai/v1/chat/completions")
    async def oa_chat(request):
        b = await request.json()
        text = next((p.get("text") for p in b["messages"][-1]["content"] if p.get("type") == "text"), "") \
            if isinstance(b["messages"][-1]["content"], list) else b["messages"][-1]["content"]
        name = await mock.run_sync("image", text or "x")
        with open(os.path.join(OUT, name), "rb") as f:
            data_url = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        return web.json_response({"choices": [{"message": {"role": "assistant",
                                                           "content": f"好的，图片如下：\n![image]({data_url})"}}]})

    @routes.post("/openai/v1/videos")
    async def oa_video(request):
        image = None
        if request.content_type.startswith("multipart"):
            form = await request.post()
            b = {k: v for k, v in form.items() if not hasattr(v, "file")}
            if "input_reference" in form:
                image = form["input_reference"].file.read()
        else:
            b = await request.json()
        if str(b.get("seconds", "4")) not in ("4", "8", "12"):
            return _jerr(400, "seconds must be 4, 8 or 12")
        t = mock.start("video", b.get("prompt", ""), image, str(b.get("size") or ""), float(b.get("seconds") or 4))
        return web.json_response({"id": f"video_{t.id}", "object": "video", "status": "queued", "progress": 0,
                                  "model": b.get("model")})

    def _oa_task(request) -> Task | None:
        return mock.tasks.get(request.match_info["vid"].removeprefix("video_"))

    @routes.get("/openai/v1/videos/{vid}")
    async def oa_video_get(request):
        t = _oa_task(request)
        if not t:
            return _jerr(404, "video not found")
        st = {"pending": "queued", "running": "in_progress", "succeeded": "completed", "failed": "failed"}[t.state]
        body = {"id": f"video_{t.id}", "status": st, "progress": 100 if t.state == "succeeded" else 40}
        if t.state == "failed":
            body["error"] = {"code": "moderation_blocked", "message": t.error}
        return web.json_response(body)

    @routes.get("/openai/v1/videos/{vid}/content")
    async def oa_video_content(request):
        t = _oa_task(request)
        if not t or t.state != "succeeded":
            return _jerr(404, "not ready")
        return web.FileResponse(os.path.join(OUT, t.file))

    @routes.delete("/openai/v1/videos/{vid}")
    async def oa_video_del(request):
        t = _oa_task(request)
        if t:
            t.cancelled = True
        return web.json_response({"id": request.match_info["vid"], "deleted": True})

    # ── DashScope ──
    @routes.post("/dashscope/api/v1/services/aigc/video-generation/video-synthesis")
    async def ds_video(request):
        if request.headers.get("X-DashScope-Async") != "enable":
            return web.json_response({"code": "AccessDenied", "message": "current user api does not support synchronous calls"},
                                     status=403)
        b = await request.json()
        inp, par = b.get("input", {}), b.get("parameters", {})
        img = _b64img(inp.get("img_url") or inp.get("first_frame_url"))
        t = mock.start("video", inp.get("prompt", ""), img, str(par.get("size", "")).replace("*", "x"),
                       float(par.get("duration") or 5))
        return web.json_response({"output": {"task_status": "PENDING", "task_id": t.id}, "request_id": uuid.uuid4().hex})

    @routes.post("/dashscope/api/v1/services/aigc/text2image/image-synthesis")
    @routes.post("/dashscope/api/v1/services/aigc/image2image/image-synthesis")
    async def ds_t2i(request):
        if request.headers.get("X-DashScope-Async") != "enable":
            return web.json_response({"code": "AccessDenied", "message": "async required"}, status=403)
        b = await request.json()
        t = mock.start("image", b["input"]["prompt"], None, str(b.get("parameters", {}).get("size", "")).replace("*", "x"))
        return web.json_response({"output": {"task_status": "PENDING", "task_id": t.id}})

    @routes.post("/dashscope/api/v1/services/aigc/multimodal-generation/generation")
    async def ds_mm(request):
        b = await request.json()
        content = b["input"]["messages"][0]["content"]
        text = next((c["text"] for c in content if "text" in c), "")
        img = next((_b64img(c["image"]) for c in content if "image" in c), None)
        name = await mock.run_sync("image", text, img, str(b.get("parameters", {}).get("size", "")).replace("*", "x"))
        return web.json_response({"output": {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": [{"image": mock.file_url(name)}]}}]}, "usage": {}, "request_id": "x"})

    @routes.get("/dashscope/api/v1/tasks/{tid}")
    async def ds_task(request):
        t = mock.tasks.get(request.match_info["tid"])
        if not t:
            return web.json_response({"output": {"task_status": "UNKNOWN", "task_id": request.match_info["tid"]}})
        out = {"task_id": t.id, "task_status": {"pending": "PENDING", "running": "RUNNING", "succeeded": "SUCCEEDED",
                                                "failed": "FAILED"}[t.state]}
        if t.state == "succeeded":
            if t.kind == "video":
                out["video_url"] = mock.file_url(t.file)
            else:
                out["results"] = [{"url": mock.file_url(t.file)}]
        if t.state == "failed":
            out.update(code="DataInspectionFailed", message=t.error)
        return web.json_response({"output": out, "request_id": "x"})

    @routes.post("/dashscope/api/v1/tasks/{tid}/cancel")
    async def ds_cancel(request):
        if t := mock.tasks.get(request.match_info["tid"]):
            t.cancelled = True
        return web.json_response({"request_id": "x"})

    # ── 火山方舟 ──
    @routes.post("/ark/api/v3/images/generations")
    async def ark_img(request):
        b = await request.json()
        img = _b64img(b["image"][0] if isinstance(b.get("image"), list) else b.get("image"))
        name = await mock.run_sync("image", b.get("prompt", ""), img, str(b.get("size", "")))
        return web.json_response({"model": b.get("model"), "data": [{"url": mock.file_url(name), "size": b.get("size")}]})

    @routes.post("/ark/api/v3/contents/generations/tasks")
    async def ark_video(request):
        b = await request.json()
        text = next((c["text"] for c in b.get("content", []) if c.get("type") == "text"), "")
        img = next((_b64img(c["image_url"]["url"]) for c in b.get("content", []) if c.get("type") == "image_url"), None)
        t = mock.start("video", text, img, "", float(b.get("duration") or 5))
        return web.json_response({"id": f"cgt-{t.id}"})

    @routes.get("/ark/api/v3/contents/generations/tasks")
    async def ark_list(request):
        return web.json_response({"items": [], "total": 0})

    @routes.get("/ark/api/v3/contents/generations/tasks/{tid}")
    async def ark_task(request):
        t = mock.tasks.get(request.match_info["tid"].removeprefix("cgt-"))
        if not t:
            return _jerr(404, "task not found", code="ResourceNotFound")
        body = {"id": request.match_info["tid"], "status": {"pending": "queued", "running": "running",
                                                            "succeeded": "succeeded", "failed": "failed"}[t.state]}
        if t.state == "succeeded":
            body["content"] = {"video_url": mock.file_url(t.file)}
        if t.state == "failed":
            body["error"] = {"code": "OutputVideoSensitiveContentDetected", "message": t.error}
        return web.json_response(body)

    @routes.delete("/ark/api/v3/contents/generations/tasks/{tid}")
    async def ark_del(request):
        if t := mock.tasks.get(request.match_info["tid"].removeprefix("cgt-")):
            t.cancelled = True
        return web.json_response({})

    # ── 可灵 ──
    def _kling_resp(t: Task) -> dict:
        st = {"pending": "submitted", "running": "processing", "succeeded": "succeed", "failed": "failed"}[t.state]
        data = {"task_id": t.id, "task_status": st, "created_at": int(t.created * 1000)}
        if t.state == "succeeded":
            key = "videos" if t.kind == "video" else "images"
            data["task_result"] = {key: [{"id": "1", "url": mock.file_url(t.file), "duration": "5"}]}
        if t.state == "failed":
            data["task_status_msg"] = t.error
        return {"code": 0, "message": "SUCCEED", "request_id": "x", "data": data}

    @routes.post("/kling/v1/videos/{mode}")
    async def kl_video(request):
        b = await request.json()
        if str(b.get("duration", "5")) not in ("5", "10"):
            return web.json_response({"code": 1201, "message": "duration 只能是 5 或 10"}, status=400)
        t = mock.start("video", b.get("prompt", ""), _b64img(b.get("image")), "", float(b.get("duration") or 5))
        return web.json_response({"code": 0, "message": "SUCCEED", "data": {"task_id": t.id, "task_status": "submitted"}})

    @routes.post("/kling/v1/images/generations")
    async def kl_img(request):
        b = await request.json()
        t = mock.start("image", b.get("prompt", ""), _b64img(b.get("image")))
        return web.json_response({"code": 0, "message": "SUCCEED", "data": {"task_id": t.id, "task_status": "submitted"}})

    @routes.get("/kling/v1/{a}/{b}")
    async def kl_list(request):
        return web.json_response({"code": 0, "data": []})

    @routes.get("/kling/v1/{a}/{b}/{tid}")
    async def kl_task(request):
        t = mock.tasks.get(request.match_info["tid"])
        if not t:
            return web.json_response({"code": 1203, "message": "task not found"}, status=404)
        return web.json_response(_kling_resp(t))

    # ── MiniMax ──
    @routes.post("/minimax/v1/video_generation")
    async def mm_video(request):
        b = await request.json()
        t = mock.start("video", b.get("prompt", ""), _b64img(b.get("first_frame_image")), "", float(b.get("duration") or 6))
        return web.json_response({"task_id": t.id, "base_resp": {"status_code": 0, "status_msg": "success"}})

    @routes.get("/minimax/v1/query/video_generation")
    async def mm_query(request):
        t = mock.tasks.get(request.query.get("task_id", ""))
        if not t:
            return web.json_response({"status": "Fail", "base_resp": {"status_code": 2013, "status_msg": "invalid task_id"}})
        st = {"pending": "Queueing", "running": "Processing", "succeeded": "Success", "failed": "Fail"}[t.state]
        body = {"task_id": t.id, "status": st, "base_resp": {"status_code": 0, "status_msg": "success"}}
        if t.state == "succeeded":
            body["file_id"] = t.file
        return web.json_response(body)

    @routes.get("/minimax/v1/files/retrieve")
    async def mm_file(request):
        fid = request.query.get("file_id", "")
        return web.json_response({"file": {"file_id": fid, "download_url": mock.file_url(fid)},
                                  "base_resp": {"status_code": 0, "status_msg": "success"}})

    @routes.post("/minimax/v1/image_generation")
    async def mm_img(request):
        b = await request.json()
        name = await mock.run_sync("image", b.get("prompt", ""))
        return web.json_response({"data": {"image_urls": [mock.file_url(name)]},
                                  "base_resp": {"status_code": 0, "status_msg": "success"}})

    # ── Gemini ──
    @routes.get("/gemini/v1beta/models")
    async def gm_models(request):
        return web.json_response({"models": [{"name": f"models/{m}"} for m in
                                             ("gemini-2.5-flash-image", "imagen-4.0-generate-001", "veo-3.1-generate-preview")]})

    @routes.post("/gemini/v1beta/models/{spec}")
    async def gm_call(request):
        model, _, method = request.match_info["spec"].partition(":")
        b = await request.json()
        if method == "generateContent":
            parts = b["contents"][0]["parts"]
            text = next((p["text"] for p in parts if "text" in p), "")
            name = await mock.run_sync("image", text)
            with open(os.path.join(OUT, name), "rb") as f:
                data = base64.b64encode(f.read()).decode()
            return web.json_response({"candidates": [{"content": {"parts": [
                {"text": "Here you go"}, {"inlineData": {"mimeType": "image/png", "data": data}}]},
                "finishReason": "STOP"}]})
        if method == "predict":
            name = await mock.run_sync("image", b["instances"][0]["prompt"])
            with open(os.path.join(OUT, name), "rb") as f:
                return web.json_response({"predictions": [{"bytesBase64Encoded": base64.b64encode(f.read()).decode(),
                                                           "mimeType": "image/png"}]})
        if method == "predictLongRunning":
            inst = b["instances"][0]
            img = base64.b64decode(inst["image"]["bytesBase64Encoded"]) if inst.get("image") else None
            t = mock.start("video", inst["prompt"], img, "", float(b.get("parameters", {}).get("durationSeconds") or 4))
            return web.json_response({"name": f"models/{model}/operations/{t.id}"})
        return _jerr(404, f"unknown method {method}")

    @routes.get("/gemini/v1beta/models/{model}/operations/{tid}")
    async def gm_op(request):
        t = mock.tasks.get(request.match_info["tid"])
        if not t:
            return _jerr(404, "operation not found")
        name = f"models/{request.match_info['model']}/operations/{t.id}"
        if t.state in ("pending", "running"):
            return web.json_response({"name": name})
        if t.state == "failed":
            return web.json_response({"name": name, "done": True, "error": {"code": 3, "message": t.error}})
        return web.json_response({"name": name, "done": True, "response": {"generateVideoResponse": {
            "generatedSamples": [{"video": {"uri": f"{mock.public}/gemini/v1beta/files/{t.file}:download?alt=media"}}]}}})

    @routes.get("/gemini/v1beta/files/{f}")
    async def gm_file(request):
        name = request.match_info["f"].split(":")[0]
        raise web.HTTPFound(f"/files/{name}")

    # ── Replicate ──
    def _rep(t: Task) -> dict:
        st = {"pending": "starting", "running": "processing", "succeeded": "succeeded", "failed": "failed"}[t.state]
        body = {"id": t.id, "status": st, "urls": {"get": f"{mock.public}/replicate/v1/predictions/{t.id}"}}
        if t.state == "succeeded":
            body["output"] = [mock.file_url(t.file)] if t.kind == "image" else mock.file_url(t.file)
        if t.state == "failed":
            body["error"] = t.error
        return body

    @routes.post("/replicate/v1/models/{owner}/{name}/predictions")
    @routes.post("/replicate/v1/predictions")
    async def rep_create(request):
        b = await request.json()
        inp = b.get("input", {})
        kind = "video" if "video" in request.path or "duration" in inp else "image"
        t = mock.start(kind, inp.get("prompt", ""), _b64img(inp.get("image")), "", float(inp.get("duration") or 2))
        if request.headers.get("Prefer", "").startswith("wait"):
            for _ in range(40):
                if t.state in ("succeeded", "failed"):
                    break
                await asyncio.sleep(0.25)
        return web.json_response(_rep(t), status=201)

    @routes.get("/replicate/v1/predictions/{tid}")
    async def rep_get(request):
        t = mock.tasks.get(request.match_info["tid"])
        return web.json_response(_rep(t)) if t else _jerr(404, "not found")

    @routes.post("/replicate/v1/predictions/{tid}/cancel")
    async def rep_cancel(request):
        if t := mock.tasks.get(request.match_info["tid"]):
            t.cancelled = True
        return web.json_response({})

    @routes.get("/replicate/v1/account")
    async def rep_acct(request):
        return web.json_response({"type": "user", "username": "mock"})

    # ── fal ──
    @routes.post("/fal/{owner}/{app:.+}")
    async def fal_submit(request):
        b = await request.json()
        app = request.match_info["app"]
        kind = "video" if "video" in app else "image"
        t = mock.start(kind, b.get("prompt", ""), _b64img(b.get("image_url")), "", float(b.get("duration") or 2))
        base = f"{mock.public}/fal/{request.match_info['owner']}/{app.split('/')[0]}/requests/{t.id}"
        return web.json_response({"request_id": t.id, "status_url": base + "/status", "response_url": base,
                                  "cancel_url": base + "/cancel"})

    @routes.get("/fal/{owner}/{app}/requests/{tid}/status")
    async def fal_status(request):
        t = mock.tasks.get(request.match_info["tid"])
        if not t:
            return _jerr(404, "not found")
        st = {"pending": "IN_QUEUE", "running": "IN_PROGRESS"}.get(t.state, "COMPLETED")
        return web.json_response({"status": st, "queue_position": 0})

    @routes.get("/fal/{owner}/{app}/requests/{tid}")
    async def fal_result(request):
        t = mock.tasks.get(request.match_info["tid"])
        if not t:
            return _jerr(404, "not found")
        if t.state == "failed":
            return web.json_response({"detail": [{"msg": t.error, "type": "content_policy_violation"}]}, status=422)
        if t.kind == "video":
            return web.json_response({"video": {"url": mock.file_url(t.file)}})
        return web.json_response({"images": [{"url": mock.file_url(t.file), "content_type": "image/png"}]})

    @routes.put("/fal/{owner}/{app}/requests/{tid}/cancel")
    async def fal_cancel(request):
        if t := mock.tasks.get(request.match_info["tid"]):
            t.cancelled = True
        return web.json_response({"status": "CANCELLATION_REQUESTED"})

    # ── 硅基流动 ──
    @routes.post("/siliconflow/v1/images/generations")
    async def sf_img(request):
        b = await request.json()
        name = await mock.run_sync("image", b.get("prompt", ""), _b64img(b.get("image")), str(b.get("image_size", "")))
        return web.json_response({"images": [{"url": mock.file_url(name)}], "timings": {"inference": 1.2}, "seed": 1})

    @routes.post("/siliconflow/v1/video/submit")
    async def sf_vsub(request):
        b = await request.json()
        t = mock.start("video", b.get("prompt", ""), _b64img(b.get("image")), str(b.get("image_size", "")))
        return web.json_response({"requestId": t.id})

    @routes.post("/siliconflow/v1/video/status")
    async def sf_vst(request):
        b = await request.json()
        t = mock.tasks.get(b.get("requestId", ""))
        if not t:
            return _jerr(400, "invalid requestId")
        st = {"pending": "InQueue", "running": "InProgress", "succeeded": "Succeed", "failed": "Failed"}[t.state]
        body = {"status": st, "reason": t.error}
        if t.state == "succeeded":
            body["results"] = {"videos": [{"url": mock.file_url(t.file)}], "timings": {"inference": 3}, "seed": 1}
        return web.json_response(body)

    # ── new-api 统一视频 ──
    @routes.post("/newapi/v1/video/generations")
    async def na_sub(request):
        b = await request.json()
        t = mock.start("video", b.get("prompt", ""), _b64img(b.get("image")), "", float(b.get("duration") or 4))
        return web.json_response({"task_id": t.id, "status": "queued"})

    @routes.get("/newapi/v1/video/generations/{tid}")
    async def na_get(request):
        t = mock.tasks.get(request.match_info["tid"])
        if not t:
            return _jerr(404, "task not found")
        st = {"pending": "queued", "running": "in_progress", "succeeded": "succeeded", "failed": "failed"}[t.state]
        body = {"task_id": t.id, "status": st}
        if t.state == "succeeded":
            body["url"] = mock.file_url(t.file)
        if t.state == "failed":
            body["error"] = {"message": t.error}
        return web.json_response(body)

    # ── 即梦反代（同步阻塞） ──
    @routes.post("/jimeng/v1/videos/generations")
    async def jm_video(request):
        b = await request.json()
        imgs = b.get("file_paths") or []
        name = await mock.run_sync("video", b.get("prompt", ""), _b64img(imgs[0] if imgs else None), "",
                                   float(b.get("duration") or 5))
        return web.json_response({"created": int(time.time()), "data": [{"url": mock.file_url(name), "revised_prompt": b.get("prompt")}]})

    @routes.post("/jimeng/v1/images/generations")
    async def jm_img(request):
        b = await request.json()
        name = await mock.run_sync("image", b.get("prompt", ""))
        return web.json_response({"created": int(time.time()), "data": [{"url": mock.file_url(name)}]})

    # ── 脚本化 LLM（OpenAI 兼容）：消息里写 CALL 工具名 {参数JSON} 就发出对应工具调用，用来端到端测试工具链路 ──
    @routes.get("/llm/v1/models")
    async def llm_models(request):
        return web.json_response({"object": "list", "data": [{"id": "scripted", "object": "model"}]})

    @routes.post("/llm/v1/chat/completions")
    async def llm_chat(request):
        b = await request.json()
        msgs = b.get("messages") or []
        last = msgs[-1] if msgs else {}

        def text_of(m):
            c = m.get("content")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c or ""

        msg: dict = {"role": "assistant", "content": ""}
        finish = "stop"
        if last.get("role") == "tool":
            msg["content"] = "TOOL_RESULT " + text_of(last)[:1500]
        else:
            import re as _re
            m = _re.search(r"CALL\s+(\w+)\s+(\{.*\})", text_of(last), _re.S)
            sys_text = " ".join(text_of(x) for x in msgs if x.get("role") == "system")
            if "只输出一个 JSON 对象" in sys_text:  # 插件的自动化构思请求：回一个固定的策划 JSON
                n = 3
                msg["content"] = "```json\n" + json.dumps({
                    "title": "橘猫的慢时光", "tags": ["橘猫", "治愈", "日系"], "desc": "一只橘猫的安静午后",
                    "prompt": "an orange cat napping on a sunny windowsill, warm film tone",
                    "prompts": [f"orange cat scene {i + 1}, warm Japanese film photo" for i in range(n)],
                    "captions": ["午后的阳光", "伸个懒腰", "又睡着了"]}, ensure_ascii=False) + "\n```"
            elif m and b.get("tools"):
                msg["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                                      "function": {"name": m.group(1), "arguments": m.group(2)}}]
                msg["content"] = None
                finish = "tool_calls"
            else:
                msg["content"] = "（脚本 LLM）" + text_of(last)[:200]
        if b.get("stream"):
            sr = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await sr.prepare(request)
            cid, created = "chatcmpl-" + uuid.uuid4().hex[:8], int(time.time())

            async def chunk(delta, finish=None, usage=None):
                d = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "scripted",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage:
                    d["usage"] = usage
                await sr.write(f"data: {json.dumps(d, ensure_ascii=False)}\n\n".encode())

            if msg.get("tool_calls"):
                tc = msg["tool_calls"][0]
                await chunk({"role": "assistant", "content": None, "tool_calls": [
                    {"index": 0, "id": tc["id"], "type": "function",
                     "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]})
            else:
                await chunk({"role": "assistant", "content": msg["content"]})
            await chunk({}, finish, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            await sr.write(b"data: [DONE]\n\n")
            await sr.write_eof()
            return sr
        resp = {"id": "chatcmpl-" + uuid.uuid4().hex[:8], "object": "chat.completion", "created": int(time.time()),
                "model": b.get("model", "scripted"),
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        return web.json_response(resp)

    app = web.Application(middlewares=[middleware], client_max_size=64 * 1024 * 1024)
    app.add_routes(routes)
    return app


def _kling_ok(auth: str) -> bool:
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]
    try:
        h, p, s = token.split(".")
        sig = base64.urlsafe_b64encode(hmac.new(KLING_SK.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        return hmac.compare_digest(sig, s) and payload.get("iss") == KLING_AK and payload.get("exp", 0) > time.time()
    except Exception:
        return False


def parse_faults(s: str) -> dict[str, float]:
    out = {}
    for part in (s or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = float(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--public-url", default="", help="产物链接的对外地址（容器里访问时填 http://host.docker.internal:8199）")
    ap.add_argument("--backend", choices=["fake", "comfy"], default="fake")
    ap.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    ap.add_argument("--image-size", default="768x512")
    ap.add_argument("--video-size", default="640x384")
    ap.add_argument("--image-steps", type=int, default=20)
    ap.add_argument("--video-steps", type=int, default=12)
    ap.add_argument("--faults", default="")
    ap.add_argument("--fake-video-delay", type=float, default=3.0)
    args = ap.parse_args()
    backend = FakeBackend(video_delay=args.fake_video_delay) if args.backend == "fake" else ComfyBackend(
        args.comfy_url, args.image_size, args.video_size, args.image_steps, args.video_steps)
    public = args.public_url or f"http://{args.host}:{args.port}"
    app = build_app(Mock(backend, parse_faults(args.faults), public))
    print(f"mock providers on {public} (backend={args.backend}, faults={args.faults or 'none'})", flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
