"""ComfyUI（本机/局域网）：内置 Qwen-Image 2.1 出图与 MiniMax H3 文/图生视频，或任意 API 格式工作流。

流程：参考图 POST /upload/image → POST /prompt → 轮询 GET /history/{id}（不在 history 时查 /queue 看是否还在排队）
      → 产物经 GET /view 下载。取消：正在跑的 POST /interrupt，排队中的 POST /queue {"delete": [...]}。

自定义工作流：在 ComfyUI 里“导出（API）”得到 JSON，把要替换的值写成占位符：
  {{prompt}} {{negative_prompt}} {{seed}} {{width}} {{height}} {{frames}} {{steps}} {{duration}}
  {{image}} {{image_2}} …（已上传到 ComfyUI 的参考图文件名）{{last_frame}}
"""

from __future__ import annotations

import json
import math
import os
import random
import uuid
from urllib.parse import urlencode

import aiohttp

from ..core.types import IMAGE, VIDEO, Asset, GenError, GenRequest, PollResult, snap
from .base import Adapter
from .generic import _render

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
VID_EXT = (".mp4", ".webm", ".mov", ".gif")

QWEN_UNET = "qwen_image_2.1_int8_convrot.safetensors"
QWEN_TE = "qwen3vl_8b_int8_convrot.safetensors"
QWEN_VAE = "qwen_image_2.1_vae_bf16.safetensors"
H3_UNET = "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"
H3_TE = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
H3_VAE = "minimax_h3_video_vae_int8_convrot.safetensors"
H3_AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"


def _n(kind: str, **inputs) -> dict:
    return {"class_type": kind, "inputs": inputs}


def h3_frames(seconds: float, fps: int = 24) -> int:
    """H3 帧数落在 17k+5 网格上（124 帧 ≈ 5.17 秒，243 ≈ 10.1 秒）。"""
    k = max(2, math.ceil((seconds * fps - 5) / 17 - 1e-6))
    return 17 * k + 5


def h3_prompt(prompt: str, first: bool, last: bool, seconds: float) -> str:
    """把普通描述包成 H3 的结构化提示词；已经是结构化格式的原样使用。"""
    if "integrated_multimodal_description" in prompt:
        return prompt
    head = ""
    if first and last:
        head = (f"The opening follows <Picture 1> at 0.00 seconds. By the end at {seconds:.2f} seconds, "
                "the continuous movement reaches the state shown in <Picture 2>.\n")
    elif first:
        head = "The visual state at 0.00 seconds follows <Picture 1> in [Shot 1].\n"
    return (f"{head}integrated_multimodal_description: [Shot 1] {prompt.strip()}\n"
            "overall_soundscape: Natural ambient sound that matches the scene.\nnon_diegetic_music: N/A")


def qwen_image_graph(prompt: str, seed: int, width: int, height: int, steps: int, refs: list[str],
                     prefix: str, negative: str = "") -> dict:
    g = {
        "1": _n("UNETLoader", unet_name=QWEN_UNET, weight_dtype="default"),
        "2": _n("CLIPLoader", clip_name=QWEN_TE, type="qwen_image", device="default"),
        "3": _n("VAELoader", vae_name=QWEN_VAE),
        "4": _n("TextEncodeQwenImage21", clip=["2", 0], vae=["3", 0], prompt=prompt, negative_prompt=negative,
                resolution=1024),
        "5": _n("EmptyLatentImage", width=width, height=height, batch_size=1),
        "6": _n("KSampler", model=["1", 0], positive=["4", 0], negative=["4", 1], latent_image=["5", 0],
                seed=seed, steps=steps, cfg=1.0, sampler_name="euler", scheduler="simple", denoise=1.0),
        "7": _n("VAEDecode", samples=["6", 0], vae=["3", 0]),
        "8": _n("SaveImage", images=["7", 0], filename_prefix=prefix),
    }
    for i, name in enumerate(refs, 1):
        g[str(20 + i)] = _n("LoadImage", image=name)
        g["4"]["inputs"][f"images.image_{i}"] = [str(20 + i), 0]
    return g


def h3_video_graph(prompt: str, seed: int, width: int, height: int, frames: int, steps: int,
                   first: str | None, last: str | None, prefix: str) -> dict:
    """2026-10-24 本机实测配置（h3-qwen21-harness）：896×512、124 帧、20 步约 5 分钟。"""
    g = {
        "1": _n("CLIPLoader", clip_name=H3_TE, type="minimax", device="default"),
        "2": _n("VAELoader", vae_name=H3_VAE),
        "4": _n("MiniMaxH3ImageToVideo", clip=["1", 0], vae=["2", 0], prompt=prompt, width=width, height=height,
                length=frames),
        "5": _n("UNETLoader", unet_name=H3_UNET, weight_dtype="default"),
        "6": _n("MiniMaxChunkFeedForward", model=["5", 0], chunks=4, seq_threshold=2048),
        "7": _n("BasicGuider", model=["6", 0], conditioning=["4", 0]),
        "8": _n("RandomNoise", noise_seed=seed),
        "9": _n("KSamplerSelect", sampler_name="res_multistep"),
        "10": _n("BasicScheduler", model=["6", 0], scheduler="simple", steps=steps, denoise=1.0),
        "11": _n("SamplerCustomAdvanced", noise=["8", 0], guider=["7", 0], sampler=["9", 0], sigmas=["10", 0],
                 latent_image=["4", 1]),
        "12": _n("LTXVSeparateAVLatent", av_latent=["11", 0]),
        "13": _n("VAEDecodeTiled", samples=["12", 0], vae=["2", 0], tile_size=512, overlap=64, temporal_size=32,
                 temporal_overlap=8),
        "14": _n("VAELoader", vae_name=H3_AUDIO_VAE),
        "15": _n("VAEDecodeAudio", samples=["12", 1], vae=["14", 0]),
        "16": _n("VHS_VideoCombine", images=["13", 0], audio=["15", 0], frame_rate=24.0, loop_count=0,
                 filename_prefix=prefix, format="video/h264-mp4", pix_fmt="yuv420p", crf=18, save_metadata=False,
                 trim_to_audio=False, pingpong=False, save_output=True),
    }
    if first:
        g["3"] = _n("LoadImage", image=first)
        g["4"]["inputs"]["first_frame"] = ["3", 0]
    if last:
        g["17"] = _n("LoadImage", image=last)
        g["4"]["inputs"]["last_frame"] = ["17", 0]
    return g


BUILTIN_NODES = {
    "qwen_image": ["TextEncodeQwenImage21", "UNETLoader", "KSampler"],
    "h3_video": ["MiniMaxH3ImageToVideo", "MiniMaxChunkFeedForward", "LTXVSeparateAVLatent", "VHS_VideoCombine"],
}


class ComfyUI(Adapter):
    provider = "comfyui"
    label = "ComfyUI（本地工作流）"
    kinds = (IMAGE, VIDEO)
    default_base_url = "http://127.0.0.1:8188"
    poll_interval = 3
    doc = "workflow 填 builtin:qwen_image / builtin:h3_video，或 API 格式工作流 JSON 文件路径/内容（含 {{prompt}} 等占位符）"
    fields = {
        "workflow": ("工作流：builtin:qwen_image / builtin:h3_video / 文件路径 / JSON 内容", "builtin:qwen_image"),
        "steps": ("采样步数（0=内置默认：出图 25，视频 20）", 0),
        "free_after_job": ("每个任务结束后让 ComfyUI 卸载模型释放显存（与其他 GPU 服务共用显卡时打开）", False),
        "workflow_dir": ("相对路径工作流所在目录（留空=插件数据目录/workflows）", ""),
    }

    def __init__(self, spec, http):
        super().__init__(spec, http)
        self.client_id = f"astrbot-media-gen-{uuid.uuid4().hex[:8]}"
        if not spec.queue_group:  # 同一个 ComfyUI 上的模型共用一个队列，避免抢显存
            spec.queue_group = f"comfy:{self.base}"
        if not spec.max_concurrency:
            spec.max_concurrency = 1

    def _h(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.spec.api_key:  # 套了反代鉴权的 ComfyUI
            h["Authorization"] = f"Bearer {self.spec.api_key}"
        return h

    async def _upload(self, ref) -> str:
        def form():
            fd = aiohttp.FormData()
            fd.add_field("image", ref.read(), filename=f"{uuid.uuid4().hex[:12]}{os.path.splitext(ref.path)[1] or '.png'}",
                         content_type=ref.mime)
            fd.add_field("subfolder", "astrbot_media_gen")
            fd.add_field("type", "input")
            fd.add_field("overwrite", "true")
            return fd

        headers = {k: v for k, v in self._h().items() if k != "Content-Type"}
        resp = await self.http.request("POST", self.url("/upload/image"), data=form, headers=headers,
                                       idempotent=True, timeout=120)
        name = resp.get("name")
        if not name:
            raise GenError(f"上传参考图失败: {resp}")
        return f"{resp.get('subfolder')}/{name}" if resp.get("subfolder") else name

    def _load_workflow(self, wf: str) -> dict:
        wf = wf.strip()
        if wf.startswith("{"):
            return json.loads(wf)
        path = wf
        if not os.path.isabs(path):
            base = self.spec.opt("workflow_dir") or self.spec.options.get("_data_dir", "")
            path = os.path.join(base, "workflows", wf) if not self.spec.opt("workflow_dir") else os.path.join(base, wf)
        if not os.path.isfile(path):
            raise GenError(f"找不到工作流文件 {path}", user_error=True)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "nodes" in data and "links" in data:
            raise GenError("这是 ComfyUI 的 UI 格式工作流，请在 ComfyUI 里用“导出（API）”另存后再配置", user_error=True)
        return data

    async def build(self, req: GenRequest) -> dict:
        wf = str(self.spec.opt("workflow", "builtin:qwen_image"))
        seed = req.seed if req.seed is not None else random.randint(1, 2 ** 31 - 1)
        uploaded = [await self._upload(r) for r in req.images]
        last = await self._upload(req.last_frame) if req.last_frame else None
        prefix = f"astrbot_media_gen/{uuid.uuid4().hex[:8]}"
        steps = int(self.spec.opt("steps", 0) or 0)
        if wf == "builtin:qwen_image":
            w, h = req.width_height((1024, 1024))
            return qwen_image_graph(req.prompt, seed, snap(w, 16), snap(h, 16), steps or 25, uploaded, prefix,
                                    req.negative_prompt)
        if wf == "builtin:h3_video":
            w, h = req.width_height((896, 512))
            secs = req.duration or 5
            frames = h3_frames(secs)
            prompt = h3_prompt(req.prompt, bool(uploaded), bool(last), (frames - 1) / 24)
            return h3_video_graph(prompt, seed, snap(w, 32), snap(h, 32), frames, steps or 20,
                                  uploaded[0] if uploaded else None, last, prefix)
        graph = self._load_workflow(wf)
        w, h = req.width_height((1024, 1024))
        ctx = {"prompt": req.prompt, "negative_prompt": req.negative_prompt, "seed": seed, "width": w, "height": h,
               "frames": h3_frames(req.duration or 5), "steps": steps or None, "duration": req.duration or None,
               "image": uploaded[0] if uploaded else None, "last_frame": last, "prefix": prefix}
        for i, name in enumerate(uploaded[1:], 2):
            ctx[f"image_{i}"] = name
        return _render(graph, ctx, drop_empty=False)

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        graph = await self.build(req)
        resp = await self.http.post_json(self.url("/prompt"), {"prompt": graph, "client_id": self.client_id},
                                         headers=self._h(), timeout=self.spec.submit_timeout_s or 60)
        if resp.get("node_errors") or resp.get("error"):
            detail = resp.get("node_errors") or resp.get("error")
            raise GenError(f"工作流校验失败: {json.dumps(detail, ensure_ascii=False)[:400]}", user_error=True,
                           code="node_errors")
        pid = resp.get("prompt_id")
        if not pid:
            raise GenError(f"ComfyUI 没有返回 prompt_id: {str(resp)[:200]}")
        return {"id": pid, "kind": req.kind}

    async def poll(self, handle: dict) -> PollResult:
        pid = handle["id"]
        hist = (await self.http.get_json(self.url(f"/history/{pid}"), headers=self._h(), timeout=30)).get(pid)
        if hist:
            status = hist.get("status") or {}
            if status.get("status_str") == "error":
                msgs = [m[1] for m in status.get("messages", []) if m and m[0] == "execution_error"]
                err = msgs[0] if msgs else {}
                text = f"{err.get('node_type', '')}: {err.get('exception_message', '执行出错')}".strip(": ")
                # 显存不足属于可重试（等别的任务释放显存后再来）
                oom = "out of memory" in text.lower() or "allocation" in text.lower()
                return PollResult("failed", error=text[:400], user_error=not oom)
            if status.get("completed") or hist.get("outputs"):
                return PollResult("succeeded", 100, self._assets(hist, handle.get("kind", IMAGE)))
            return PollResult("running")
        queue = await self.http.get_json(self.url("/queue"), headers=self._h(), timeout=30)
        running = [q[1] for q in queue.get("queue_running", [])]
        pending = [q[1] for q in sorted(queue.get("queue_pending", []), key=lambda q: q[0])]
        if pid in running:
            return PollResult("running")
        if pid in pending:
            return PollResult("pending", progress=None)
        handle["missing"] = handle.get("missing", 0) + 1
        if handle["missing"] >= 3:  # ComfyUI 重启过，任务丢了
            return PollResult("failed", error="ComfyUI 队列里找不到该任务（服务可能重启过）")
        return PollResult("pending")

    def _assets(self, hist: dict, kind: str) -> list[Asset]:
        items = [it for out in hist.get("outputs", {}).values()
                 for key in ("images", "gifs", "videos", "video") for it in out.get(key, []) or []]
        want = VID_EXT if kind == VIDEO else IMG_EXT
        assets = []
        for it in items:
            name = it.get("filename", "")
            if it.get("type") == "temp" or not name.lower().endswith(want):
                continue
            query = urlencode({"filename": name, "subfolder": it.get("subfolder", ""), "type": it.get("type", "output")})
            assets.append(Asset(kind, url=self.url(f"/view?{query}"),
                                headers={k: v for k, v in self._h().items() if k != "Content-Type"},
                                ext=os.path.splitext(name)[1]))
        if not assets:
            raise GenError(f"工作流跑完了但没有{'视频' if kind == VIDEO else '图片'}输出（检查保存节点）")
        return assets

    async def cancel(self, handle: dict) -> None:
        pid = handle.get("id")
        try:
            queue = await self.http.get_json(self.url("/queue"), headers=self._h(), timeout=20, retries=0)
            if pid in [q[1] for q in queue.get("queue_running", [])]:
                await self.http.request("POST", self.url("/interrupt"), json_body={"prompt_id": pid}, headers=self._h(),
                                        timeout=20, retries=0, expect="text")
            else:
                await self.http.request("POST", self.url("/queue"), json_body={"delete": [pid]}, headers=self._h(),
                                        timeout=20, retries=0, expect="text")
        except GenError:
            pass

    async def after_job(self) -> None:
        if self.spec.opt("free_after_job", False):
            try:
                queue = await self.http.get_json(self.url("/queue"), headers=self._h(), timeout=20, retries=0)
                if not (queue.get("queue_running") or queue.get("queue_pending")):
                    await self.http.request("POST", self.url("/free"), json_body={"unload_models": True, "free_memory": True},
                                            headers=self._h(), timeout=30, retries=0, expect="text")
            except GenError:
                pass

    async def probe(self) -> str:
        stats = await self.http.get_json(self.url("/system_stats"), headers=self._h(), timeout=15, retries=1)
        dev = (stats.get("devices") or [{}])[0]
        vram = f"，显存空闲 {dev.get('vram_free', 0) / 2**30:.1f}/{dev.get('vram_total', 0) / 2**30:.1f} GiB" if dev else ""
        wf = str(self.spec.opt("workflow", "builtin:qwen_image"))
        missing = []
        if wf.startswith("builtin:"):
            for cls in BUILTIN_NODES.get(wf.split(":", 1)[1], []):
                info = await self.http.get_json(self.url(f"/object_info/{cls}"), headers=self._h(), timeout=15, retries=1)
                if cls not in info:
                    missing.append(cls)
        ver = (stats.get("system") or {}).get("comfyui_version", "?")
        if missing:
            raise GenError(f"ComfyUI {ver} 可达，但缺少节点 {', '.join(missing)}（这台 ComfyUI 跑不了 {wf}）", user_error=True)
        return f"ComfyUI {ver} 可达{vram}"
