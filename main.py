"""
AstrBot AI 图片/视频生成插件

- 多接口兼容：OpenAI 图片/Sora、百炼、火山方舟、可灵、MiniMax、Gemini/Veo、Replicate、fal、ComfyUI 本地工作流、通用模板
- 配好的模型通过 4 个 LLM 工具暴露给 AI：生成 / 任务 / 设置 / 自动化；设置与自动化支持自然语言（管理员）
- 任务持久化 + 超时/重试/熔断/回退，重启后续跑
- 联动账号运维插件：生成结果一键/自动提交发布审批
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .core import bridge
from .core.automation import Automations, parse_plan
from .core.http import Http, is_local
from .core.jobs import Job, JobManager
from .core.media import slideshow
from .core.registry import Registry, make_http_factory
from .core.settings import Settings
from .core.types import IMAGE, VIDEO, GenError, GenRequest, RefImage, parse_size, size_from_ratio

PLUGIN = "astrbot_plugin_media_gen"
KIND_WORDS = {"image": IMAGE, "图片": IMAGE, "图": IMAGE, "画": IMAGE, "picture": IMAGE, "photo": IMAGE,
              "video": VIDEO, "视频": VIDEO, "影片": VIDEO, "动画": VIDEO}
RATIO_RE = re.compile(r"^\s*(\d{1,2})\s*[:：/]\s*(\d{1,2})\s*$")


def _kind(k: str) -> str:
    v = KIND_WORDS.get((k or "").strip().lower())
    if not v:
        raise GenError("kind 只能是 image 或 video", user_error=True)
    return v


def _values(v) -> dict:
    """工具参数里的“值”：JSON 对象，或 key=value; key2=value2。"""
    if isinstance(v, dict):
        return v
    v = (v or "").strip()
    if not v:
        return {}
    if v.startswith("{"):
        try:
            d = json.loads(v)
            if isinstance(d, dict):
                return d
        except ValueError as e:
            raise GenError(f"values 不是合法 JSON：{e}", user_error=True) from e
    out = {}
    for part in re.split(r"[;；\n]", v):
        m = re.match(r"\s*([^=：:]+?)\s*(?:=|：|:(?!//))\s*(.+)$", part)
        if m:
            out[m.group(1)] = m.group(2).strip()
    if not out:
        raise GenError("values 请用 JSON，如 {\"api_key\": \"sk-...\"}", user_error=True)
    return out


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


@register(PLUGIN, "toddmiao", "AI 图片/视频生成：多接口兼容、LLM 工具、自然语言配置、联动账号运维自动发布", "0.1.0",
          "https://github.com/miaoxutao123/astrbot_plugin_media_gen")
class MediaGenPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.context = context
        self.config = config
        try:
            self.data_dir = str(StarTools.get_data_dir(PLUGIN))
        except Exception:
            self.data_dir = os.path.abspath(f"data/plugin_data/{PLUGIN}")
        os.makedirs(self.data_dir, exist_ok=True)
        self.inputs_dir = os.path.join(self.data_dir, "inputs")
        os.makedirs(self.inputs_dir, exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "workflows"), exist_ok=True)
        self.registry = Registry(config, make_http_factory(config), self.data_dir)
        self.jobs = JobManager(self.registry, config, self.data_dir, on_done=self._on_done)
        self.settings = Settings(config, self.data_dir)
        self.autos = Automations(config, self.data_dir, runner=self._run_recipe)
        self.http = Http(read_timeout=60, retries=2)
        self._batches: dict[str, dict] = {}
        self._gc_task = None

    async def initialize(self):
        import asyncio
        n = self.jobs.resume_all()
        if n:
            logger.info(f"[media_gen] 续跑 {n} 个未完成的生成任务")
        self.autos.start()
        self._gc_task = asyncio.create_task(self._gc_loop())
        problems = self.settings.check()
        logger.info(f"[media_gen] 已加载 {len(self.registry.specs())} 个模型，{len(self.autos.recipes())} 个自动化任务"
                    + (f"；配置提示 {problems.get('missing') or problems.get('problems')}"
                       if problems.get("missing") or problems.get("problems") else ""))

    async def terminate(self):
        if self._gc_task:
            self._gc_task.cancel()
        await self.autos.stop()
        await self.jobs.shutdown()
        await self.registry.close()
        await self.http.close()

    async def _gc_loop(self):
        import asyncio
        while True:
            try:
                n = self.jobs.cleanup()
                cutoff = time.time() - 3 * 86400
                for f in os.listdir(self.inputs_dir):
                    p = os.path.join(self.inputs_dir, f)
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                if n:
                    logger.info(f"[media_gen] 清理过期产物 {n} 个")
            except Exception as e:
                logger.warning(f"[media_gen] 清理失败: {e}")
            await asyncio.sleep(3600)

    # ==================== 工具方法 ====================

    async def _send(self, umo: str, chain: list) -> bool:
        if not umo:
            return False
        try:
            await self.context.send_message(umo, MessageChain(chain))
            return True
        except Exception as e:
            logger.warning(f"[media_gen] 推送到 {umo} 失败: {e}")
            return False

    def _media(self, path: str, kind: str):
        if kind == IMAGE or path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return Comp.Image.fromFileSystem(path)
        if os.path.getsize(path) / 1048576 > float(self.config.get("send_as_file_mb", 100) or 100):
            return Comp.File(file=path, name=os.path.basename(path))
        return Comp.Video.fromFileSystem(path)

    def _avg_seconds(self, model: str) -> int | None:
        done = [j for j in self.jobs.jobs.values() if j.model == model and j.status == "succeeded" and j.started]
        done = sorted(done, key=lambda j: j.finished)[-5:]
        if not done:
            return None
        return int(sorted(j.finished - j.started for j in done)[len(done) // 2])

    def _check_user(self, event: AstrMessageEvent, kind: str) -> None:
        if event.is_admin():
            return
        if not self.config.get("allow_non_admin_video" if kind == VIDEO else "allow_non_admin_image",
                               kind == IMAGE):
            raise GenError(f"{'视频' if kind == VIDEO else '图片'}生成目前只对管理员开放，请礼貌告知用户", user_error=True)
        limit = int(self.config.get("user_daily_video_limit" if kind == VIDEO else "user_daily_image_limit",
                                    3 if kind == VIDEO else 20) or 0)
        if limit and self.jobs.count_today(str(event.get_sender_id()), kind) >= limit:
            raise GenError(f"该用户今天的{'视频' if kind == VIDEO else '图片'}额度（{limit}）已用完", user_error=True)

    def _stash(self, src: str, ext: str = "") -> str:
        dest = os.path.join(self.inputs_dir, f"{uuid.uuid4().hex[:12]}{ext or os.path.splitext(src)[1] or '.png'}")
        shutil.copy(src, dest)
        return dest

    async def _event_images(self, event: AstrMessageEvent) -> list[RefImage]:
        comps = list(getattr(event.message_obj, "message", None) or [])
        for c in list(comps):
            if isinstance(c, Comp.Reply) and getattr(c, "chain", None):
                comps += list(c.chain)
        out = []
        for c in comps:
            if isinstance(c, Comp.Image) and len(out) < 4:
                try:
                    path = await c.convert_to_file_path()
                    out.append(RefImage(path=self._stash(path)))  # 聊天图片链接常有时效/防盗链，统一用文件内容
                except Exception as e:
                    logger.warning(f"[media_gen] 读取聊天图片失败: {e}")
        return out

    def _job_images(self, job: Job | None) -> list[RefImage]:
        if not job or job.status != "succeeded":
            return []
        return [RefImage(path=p) for p in job.files if p.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]

    async def _resolve_images(self, event: AstrMessageEvent, spec: str) -> list[RefImage]:
        spec = (spec or "").strip()
        if not spec:
            return await self._event_images(event)
        refs: list[RefImage] = []
        for part in [p.strip() for p in re.split(r"[,，\s]+", spec) if p.strip()]:
            low = part.lower()
            if low in ("last", "上一张", "刚才", "上次", "previous"):
                pool = [j for j in self.jobs.list(event.unified_msg_origin, 30) if j.kind == IMAGE and j.status == "succeeded"]
                imgs = self._job_images(pool[0] if pool else None)
                if not imgs:
                    raise GenError("这个会话里还没有生成过图片", user_error=True)
                refs += imgs[:1]
            elif low.startswith("job:") or re.fullmatch(r"[0-9a-f]{6,10}", low):
                imgs = self._job_images(self.jobs.find(part.split(":", 1)[-1]))
                if not imgs:
                    raise GenError(f"任务 {part} 没有可用的图片", user_error=True)
                refs += imgs
            elif low.startswith(("http://", "https://")):
                if is_local(part):
                    raise GenError("不允许使用内网地址作为参考图", user_error=True)
                dest = os.path.join(self.inputs_dir, f"{uuid.uuid4().hex[:12]}.img")
                await self.http.download(part, dest, max_bytes=20 << 20, timeout=60)
                refs.append(RefImage(path=dest, url=part))
            elif low in ("msg", "message", "聊天", "这张", "用户的图"):
                refs += await self._event_images(event)
            else:
                path = os.path.abspath(part)
                if not path.startswith(self.data_dir) or not os.path.isfile(path):  # 不让 AI 读任意本地文件外发
                    raise GenError(f"无法使用参考图 {part[:60]}", user_error=True)
                refs.append(RefImage(path=path))
        return refs[:6]

    @staticmethod
    def _size(size: str, kind: str) -> str:
        size = (size or "").strip().lower()
        if not size:
            return ""
        if m := RATIO_RE.match(size):
            w, h = size_from_ratio(f"{m.group(1)}:{m.group(2)}", 1280 if kind == VIDEO else 1024)
            return f"{w}x{h}"
        named = {"竖屏": "9:16", "竖版": "9:16", "横屏": "16:9", "横版": "16:9", "方形": "1:1", "正方形": "1:1"}
        if size in named:
            w, h = size_from_ratio(named[size], 1280 if kind == VIDEO else 1024)
            return f"{w}x{h}"
        wh = parse_size(size)
        return f"{wh[0]}x{wh[1]}" if wh else ""

    # ==================== 任务完成：推送 / 图文 / 自动发布 ====================

    async def _on_done(self, job: Job):
        if job.meta.get("batch"):
            await self._batch_done(job)
            return
        if job.deliver and job.umo:
            await self._deliver(job)
        if job.status == "succeeded" and job.meta.get("auto_publish") and job.meta.get("platform", "none") != "none":
            res = await self._publish_job(job, None, job.meta["platform"], job.meta.get("title", ""),
                                          job.meta.get("tags", []), job.meta.get("account", ""))
            if res.get("status") in ("rejected", "unavailable"):
                await self._send(job.umo, [Comp.Plain(f"⚠️ 自动发布未提交：{res.get('error')}")])

    async def _deliver(self, job: Job):
        took = int((job.finished or time.time()) - job.created)
        if job.status == "succeeded":
            head = f"✅ {job.meta.get('title') or ('视频' if job.kind == VIDEO else '图片')}已生成（{job.model}，{took}s，任务 {job.id}）"
            if job.meta.get("automation"):
                tags = " ".join(f"#{t}" for t in job.meta.get("tags", []))
                head = f"🤖 自动化「{job.meta['automation']}」{head}\n{tags}"
            chain = [Comp.Plain(head)] + [self._media(p, job.kind) for p in job.files]
        elif job.status == "failed":
            chain = [Comp.Plain(f"❌ 生成失败（任务 {job.id}，试过 {', '.join(job.tried) or job.model}）：{job.error[:200]}\n"
                                "可以说“重试”或换个模型。")]
        else:
            return
        if not await self._send(job.umo, chain) and job.status == "succeeded":
            await self._send(job.umo, [Comp.Plain(f"{chain[0].text}\n（文件发送失败，可让我重新发送任务 {job.id}）")])

    async def _batch_done(self, job: Job):
        b = self._batches.get(job.meta["batch"])
        if not b:
            return
        jobs = [self.jobs.jobs.get(i) for i in b["jobs"]]
        if any(j is None or j.status not in ("succeeded", "failed", "cancelled") for j in jobs):
            return
        self._batches.pop(job.meta["batch"], None)
        files = [p for j in jobs if j.status == "succeeded" for p in j.files]
        umo, plan, recipe = b["umo"], b["plan"], b["recipe"]
        if not files:
            await self._send(umo, [Comp.Plain(f"❌ 自动化「{recipe['name']}」图片全部生成失败：{jobs[0].error[:150]}")])
            self.autos.record(recipe["name"], "图片全部失败")
            return
        tags = " ".join(f"#{t}" for t in plan.get("tags", []))
        text = f"🤖 自动化「{recipe['name']}」：{plan.get('title', '')}\n{plan.get('desc', '')}\n{tags}".strip()
        if recipe.get("kind") != "slides":
            await self._send(umo, [Comp.Plain(text)] + [Comp.Image.fromFileSystem(p) for p in files])
            return
        try:
            out = os.path.join(os.path.dirname(files[0]), f"slides_{uuid.uuid4().hex[:8]}.mp4")
            size = recipe.get("size") or "720x1280"
            captions = plan.get("captions") if isinstance(plan.get("captions"), list) else None
            await slideshow(files, out, size=size if parse_size(size) else "720x1280", captions=captions)
        except GenError as e:
            await self._send(umo, [Comp.Plain(f"{text}\n⚠️ 合成图文视频失败：{e}")] + [Comp.Image.fromFileSystem(p) for p in files])
            return
        # 合成结果登记为一个任务，便于后续“发布刚才那个”
        vjob = Job(id=uuid.uuid4().hex[:10], kind=VIDEO, request={"prompt": plan.get("title", "")}, model="slideshow",
                   umo=umo, requester="automation", status="succeeded", files=[out], finished=time.time(),
                   meta={"title": plan.get("title"), "tags": plan.get("tags", []), "desc": plan.get("desc", ""),
                         "automation": recipe["name"], "from_jobs": b["jobs"]})
        self.jobs.jobs[vjob.id] = vjob
        self.jobs.save()
        await self._send(umo, [Comp.Plain(f"{text}\n（图文视频，任务 {vjob.id}）"), self._media(out, VIDEO)])
        if recipe.get("auto_publish") and recipe.get("platform", "none") != "none":
            res = await self._publish_job(vjob, None, recipe["platform"], plan.get("title", ""), plan.get("tags", []),
                                          recipe.get("account", ""))
            if res.get("status") in ("rejected", "unavailable"):
                await self._send(umo, [Comp.Plain(f"⚠️ 自动发布未提交：{res.get('error')}")])

    async def _publish_job(self, job: Job, event, platform: str, title: str, tags: list[str], account: str = "") -> dict:
        if job.status != "succeeded" or not job.files:
            return {"status": "rejected", "error": f"任务 {job.id} 还没有成功的产物"}
        path = job.files[0]
        if job.kind == IMAGE:  # 平台只收视频：多图合成图文视频
            out = os.path.join(os.path.dirname(path), f"{job.id}_slides.mp4")
            if not os.path.exists(out):
                await slideshow(job.files, out, size="720x1280")
            path = out
        title = (title or job.meta.get("title") or job.request.get("prompt", "")[:20]).strip()[:80]
        tags = [t.lstrip("#") for t in (tags or job.meta.get("tags") or []) if t][:10]
        if platform == "bilibili" and not tags:
            tags = ["AI生成"]
        ev = event or bridge.BridgeEvent(job.umo, sender_id=job.requester or "media_gen",
                                         sender_name=job.requester_name or "AI 生成插件", admin=True)
        # 定时任务往往没人盯着：审批有效期放长（运维插件 0.10.1+ 支持，旧版本会忽略这个参数）
        ttl = int(self.config.get("publish_confirm_ttl_s", 43200) or 0) if job.meta.get("automation") else 0
        res = await bridge.publish(self.context, self.config.get("ops_plugin", "astrbot_plugin_videos_analysis"), ev,
                                   path, title, tags, platform, account, desc=job.meta.get("desc", ""), confirm_ttl=ttl)
        job.meta["publish"] = {"platform": platform, "status": res.get("status"), "code": res.get("code"),
                               "at": int(time.time())}
        self.jobs.save()
        return res

    # ==================== 自动化 ====================

    async def _planner_provider(self, umo: str = "") -> str:
        pid = self.config.get("planner_provider_id", "")
        if pid:
            return pid
        if umo:
            try:
                return await self.context.get_current_chat_provider_id(umo)
            except Exception:
                pass
        prov = self.context.get_using_provider()
        return prov.meta().id if prov else ""

    async def _plan(self, r: dict) -> dict:
        kind = r.get("kind", "video")
        count = max(1, int(r.get("count") or 1))
        recent = self.autos.recent_titles(r["name"])
        sys_prompt = (
            "你是短视频/图文内容策划，负责给 AI 生成模型写素材。只输出一个 JSON 对象，不要其他文字。字段：\n"
            "title: 发布标题（≤30 字，有吸引力，不要标点堆砌）\n"
            "tags: 3-5 个话题词（不带 #）\n"
            "desc: 一两句简介\n"
            "prompt: 给生成模型的画面描述（具体写主体、动作、场景、镜头、光线、风格；"
            + ("视频要写清这几秒内的连续动作和镜头运动，避免多场景切换" if kind == "video" else "单张画面") + "）\n"
            + (f"prompts: 长度为 {count} 的数组，每张图各自的画面描述（同一主题、同一画风，构成连续的图文）\n"
               f"captions: 长度为 {count} 的数组，每张图配的一句短文案（≤16 字）\n" if kind in ("image", "slides") and count > 1 else "")
        )
        user = (f"主题/方向：{r.get('topic')}\n风格与要求：{r.get('style') or '无特别要求'}\n"
                f"产出：{'视频' if kind == 'video' else '图文'}，尺寸 {r.get('size') or '默认'}"
                + (f"，时长约 {r.get('duration')} 秒" if kind == "video" and r.get("duration") else "")
                + (f"，发布平台 {r.get('platform')}" if r.get("platform", "none") != "none" else "")
                + (f"\n最近已经做过（不要重复）：{'；'.join(recent[:10])}" if recent else "")
                + f"\n今天是 {time.strftime('%Y-%m-%d %A')}")
        pid = await self._planner_provider(r.get("notify_umo", ""))
        if pid:
            for attempt in range(2):
                try:
                    resp = await self.context.llm_generate(chat_provider_id=pid, prompt=user, system_prompt=sys_prompt)
                    return parse_plan(resp.completion_text)
                except Exception as e:
                    logger.warning(f"[media_gen] 自动化构思失败（第 {attempt + 1} 次）: {e}")
        logger.warning("[media_gen] 构思失败（没有可用的聊天模型或回复格式不对），直接用主题作提示词")
        topic = str(r.get("topic", ""))
        return {"title": topic[:20], "tags": [], "desc": "", "prompt": f"{topic}。{r.get('style') or ''}"}

    async def _run_recipe(self, r: dict, reason: str) -> dict:
        umo = r.get("notify_umo", "")
        plan = await self._plan(r)
        kind = r.get("kind", "video")
        meta = {"automation": r["name"], "title": plan.get("title"), "tags": plan.get("tags", []),
                "desc": plan.get("desc", ""), "platform": r.get("platform", "none"), "account": r.get("account", ""),
                "auto_publish": bool(r.get("auto_publish")), "reason": reason}
        if plan.get("title"):
            self.autos.record(r["name"], "已提交生成", plan["title"])
        if kind == "video":
            req = GenRequest(kind=VIDEO, prompt=plan["prompt"], size=self._size(r.get("size", ""), VIDEO),
                             duration=float(r.get("duration") or 0))
            job = self.jobs.submit(req, model=r.get("model", ""), umo=umo, requester="automation",
                                   requester_name=f"自动化:{r['name']}", meta=meta)
            return {"job_id": job.id, "title": plan.get("title"), "model": job.model}
        count = max(1, min(int(r.get("count") or 1), 9))
        prompts = plan.get("prompts") if isinstance(plan.get("prompts"), list) and plan["prompts"] else [plan["prompt"]]
        batch = uuid.uuid4().hex[:8]
        ids = []
        for i in range(count):
            req = GenRequest(kind=IMAGE, prompt=prompts[i % len(prompts)], size=self._size(r.get("size", ""), IMAGE))
            job = self.jobs.submit(req, model=r.get("model", ""), umo=umo, requester="automation",
                                   requester_name=f"自动化:{r['name']}", deliver=False,
                                   meta={**meta, "batch": batch, "auto_publish": False})
            ids.append(job.id)
        self._batches[batch] = {"jobs": ids, "umo": umo, "plan": plan, "recipe": dict(r)}
        return {"jobs": ids, "title": plan.get("title"), "batch": batch}

    # ==================== LLM 工具实现 ====================

    async def _tool(self, fn, *args) -> str:
        try:
            result = await fn(*args)
        except GenError as e:
            result = {"error": str(e)[:300]}
        except Exception as e:
            logger.exception(f"[media_gen] 工具 {fn.__name__} 出错")
            result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        return _dump(result)

    async def t_generate(self, event, kind, prompt, model, images, size, duration, count, negative_prompt) -> dict:
        kind = _kind(kind)
        if not (prompt or "").strip():
            raise GenError("prompt 不能为空", user_error=True)
        self._check_user(event, kind)
        refs = await self._resolve_images(event, images)
        req = GenRequest(kind=kind, prompt=prompt.strip(), negative_prompt=(negative_prompt or "").strip(),
                         size=self._size(size, kind), duration=float(duration or 0),
                         n=max(1, min(int(count or 1), 4)) if kind == IMAGE else 1)
        if kind == VIDEO:
            req.images = refs[:1]
            req.last_frame = refs[1] if len(refs) > 1 else None
        else:
            req.images = refs
        job = self.jobs.submit(req, model=(model or "").strip(), umo=event.unified_msg_origin,
                               requester=str(event.get_sender_id()), requester_name=str(event.get_sender_name() or ""),
                               meta={"source": "llm"})
        base = {"job_id": job.id, "model": job.model, "refs": len(refs)}
        if kind == IMAGE:
            await self.jobs.wait(job, float(self.config.get("tool_wait_s", 60) or 60))
            if job.status == "succeeded":
                return {**base, "status": "done", "images": len(job.files),
                        "note": "图片已经发到聊天里了，不要再发链接或重复描述图片，简短回应即可"}
            if job.status == "failed":
                return {**base, "status": "failed", "error": job.error[:200]}
        avg = self._avg_seconds(job.model)
        ahead = self.jobs.queue_position(job)
        eta = f"约 {max(1, round(avg * (ahead + 1) / 60))} 分钟" if avg else "几分钟"
        return {**base, "status": "generating", "queue_ahead": ahead, "eta": eta,
                "note": "还在生成，完成后会自动发到这个聊天。如实告诉用户已开始生成及预计时间，不要声称已完成"}

    async def t_task(self, event, action, job_id, platform, title, tags, account) -> dict:
        action = (action or "").strip().lower()
        umo = event.unified_msg_origin
        if action == "models":
            out = []
            for s in self.registry.specs():
                item = {"name": s.name, "kind": s.kind, "api": self.registry.adapter(s).label}
                if s.description:
                    item["desc"] = s.description[:80]
                if s.cost_hint:
                    item["cost"] = s.cost_hint[:40]
                if s.name in (self.config.get("default_image_model"), self.config.get("default_video_model")):
                    item["default"] = True
                if self.registry.breaker.is_open(s.name):
                    item["status"] = f"熔断中 {self.registry.breaker.left(s.name)}s"
                if avg := self._avg_seconds(s.name):
                    item["avg_s"] = avg
                out.append(item)
            return {"models": out} if out else {"models": [], "note": "还没有配置模型，管理员可以用 media_settings 添加"}
        if action == "list":
            return {"jobs": [j.brief() for j in self.jobs.list(umo, 8)]}
        job = self.jobs.find(job_id, umo)
        if not job:
            return {"error": f"找不到任务 {job_id or '（最近的）'}"}
        mine = job.requester == str(event.get_sender_id()) or event.is_admin()
        if action == "status":
            return job.brief() | ({"errors": job.errors[-3:]} if job.status == "failed" else {})
        if action == "cancel":
            if not mine:
                return {"error": "只能取消自己的任务"}
            return {"result": await self.jobs.cancel(job)}
        if action == "retry":
            if not mine:
                return {"error": "只能重试自己的任务"}
            self._check_user(event, job.kind)
            new = self.jobs.retry(job)
            return {"status": "generating", "job_id": new.id, "model": new.model, "note": "完成后自动发到聊天"}
        if action == "resend":
            if job.status != "succeeded":
                return {"error": f"任务状态是 {job.status}，没有可发送的文件"}
            ok = await self._send(umo, [self._media(p, job.kind) for p in job.files if os.path.exists(p)])
            return {"status": "sent" if ok else "send_failed"}
        if action == "publish":
            plat = {"抖音": "douyin", "b站": "bilibili", "bili": "bilibili"}.get((platform or "").lower(), (platform or "").lower())
            if plat not in ("douyin", "bilibili"):
                return {"error": "platform 填 douyin 或 bilibili"}
            tag_list = [t for t in re.split(r"[\s,，#]+", tags or "") if t]
            res = await self._publish_job(job, event, plat, title, tag_list, account)
            return res
        return {"error": "action 只能是 models / list / status / cancel / retry / resend / publish"}

    async def t_settings(self, event, action, model, values, preset) -> dict:
        if not event.is_admin():
            return {"error": "只有管理员可以修改生成插件的设置，请礼貌拒绝"}
        action = (action or "").strip().lower()
        s = self.settings
        if action == "show":
            return s.show(model)
        if action == "presets":
            return s.presets()
        if action == "check":
            return s.check()
        if action == "add":
            return s.add(model, preset=(preset or "").strip(), values=_values(values))
        if action == "set":
            return s.set(model, _values(values)) if model else s.set_global(_values(values))
        if action in ("enable", "disable"):
            return s.set(model, {"enabled": action == "enable"})
        if action == "remove":
            return s.remove(model)
        if action == "default":
            return s.set_default(model)
        if action == "undo":
            return s.undo()
        if action in ("probe", "test"):
            spec = self.registry.get(model, include_disabled=True)
            if not spec:
                return {"error": f"没有模型 {model}"}
            if action == "probe":
                try:
                    msg = await self.registry.adapter(spec).probe()
                    self.registry.breaker.reset(spec.name)
                    return {"model": spec.name, "ok": True, "result": msg}
                except GenError as e:
                    return {"model": spec.name, "ok": False, "error": str(e)[:300]}
            prompt = _values(values).get("prompt") if (values or "").strip().startswith("{") else (values or "")
            req = GenRequest(kind=spec.kind, prompt=prompt or ("a cute orange cat sitting on a windowsill, soft morning light"
                                                                if spec.kind == IMAGE else
                                                                "A cute orange cat slowly turns its head toward the camera and blinks, soft morning light"),
                             size=spec.default_size, duration=spec.default_duration or (4 if spec.kind == VIDEO else 0))
            self.registry.breaker.reset(spec.name)
            job = self.jobs.submit(req, model=spec.name, umo=event.unified_msg_origin, requester=str(event.get_sender_id()),
                                   meta={"title": f"测试 {spec.name}", "no_fallback": True})
            return {"status": "testing", "job_id": job.id, "note": "已提交一次真实生成测试，结果会自动发到聊天（可能产生费用）"}
        return {"error": "action 只能是 show / presets / check / add / set / enable / disable / remove / default / probe / test / undo"}

    async def t_automation(self, event, action, name, values) -> dict:
        if not event.is_admin():
            return {"error": "只有管理员可以管理自动化任务，请礼貌拒绝"}
        action = (action or "").strip().lower()
        a = self.autos
        if action == "list":
            return {"automations": a.view()}
        if action == "add":
            vals = _values(values)
            vals["name"] = name or vals.get("name", "")
            vals.setdefault("notify_umo", event.unified_msg_origin)
            item = a.add(vals)
            nxt = a.next_run(item)
            return {"status": "added", "name": item["name"], "schedule": item["schedule"],
                    "next_run": time.strftime("%m-%d %H:%M", time.localtime(nxt)) if nxt else "仅手动",
                    "auto_publish": item["auto_publish"],
                    "note": "自动发布也只会提交申请，管理员在聊天里确认后才真正发布" if item["auto_publish"] else ""}
        if action in ("set", "enable", "disable"):
            vals = _values(values) if action == "set" else {"enabled": action == "enable"}
            r = a.update(name, vals)
            return {"status": "updated", "name": r["name"], "changed": vals}
        if action == "remove":
            return {"status": "removed", "name": a.remove(name)["name"]}
        if action == "run":
            r = a.find(name)
            if not r:
                return {"error": f"没有自动化任务 {name}"}
            if not r.get("notify_umo"):
                r["notify_umo"] = event.unified_msg_origin
            res = await a.trigger(r, "manual")
            return {**res, "note": "已开始，构思与生成结果会推送到设定的会话"} if "error" not in res else res
        return {"error": "action 只能是 list / add / set / enable / disable / remove / run"}

    # ==================== LLM 工具注册 ====================
    # 描述保持静态（不含模型名等动态内容），结果保持简短：AstrBot 会把工具结果写进对话历史。

    @filter.llm_tool(name="media_generate")
    async def llm_media_generate(self, event: AstrMessageEvent, kind: str, prompt: str, model: str = "",
                                 images: str = "", size: str = "", duration: float = 0, count: int = 1,
                                 negative_prompt: str = ""):
        """用 AI 生成图片或视频，完成后插件会自动把文件发到当前聊天（你不需要再发）。图片一般几十秒内完成；视频要几分钟，工具会立即返回任务号，你如实告诉用户“已开始生成、完成后自动发送”即可。prompt 要具体：主体、动作、场景、镜头、光线、风格；用户说得简单时由你扩写，保持用户原意。用户发了图片或回复了图片并要求参考/改图/让它动起来时，images 留空即可自动使用那张图；“把刚才生成的图做成视频”时 images 填 last。

        Args:
            kind(string): image 或 video
            prompt(string): 画面描述（可中文）
            model(string): 模型名，留空用默认模型；用户点名模型或不确定有哪些模型时先调用 media_task(action=models)
            images(string): 参考图：留空=用户消息里的图；last=本会话上一次生成的图；也可填任务号或图片链接（逗号分隔）。视频第 1 张作首帧、第 2 张作尾帧
            size(string): 尺寸，如 1024x1024、1280x720、16:9、9:16、竖屏、横屏；留空=模型默认
            duration(number): 视频时长（秒），留空=模型默认
            count(number): 图片张数（1-4）
            negative_prompt(string): 不希望出现的内容（可选）
        """
        return await self._tool(self.t_generate, event, kind, prompt, model, images, size, duration, count, negative_prompt)

    @filter.llm_tool(name="media_task")
    async def llm_media_task(self, event: AstrMessageEvent, action: str, job_id: str = "", platform: str = "",
                             title: str = "", tags: str = "", account: str = ""):
        """查看可用的生成模型，或管理生成任务。models=列出可用模型（名称、类型、特点、默认）；list=本会话最近的任务；status=查某个任务进度；cancel=取消；retry=重新生成；resend=重新发送结果文件；publish=把生成好的视频/图片（多图会合成图文视频）交给账号运维插件发布到抖音/B站——只会提交审批，管理员确认后才发布，如实告知用户“已提交审批”。

        Args:
            action(string): models / list / status / cancel / retry / resend / publish
            job_id(string): 任务号，留空=本会话最近一个任务
            platform(string): publish 用：douyin 或 bilibili
            title(string): publish 用：标题，留空用生成时的标题/提示词
            tags(string): publish 用：话题，空格分隔
            account(string): publish 用：账号运维插件里的账号名，留空=当前账号
        """
        return await self._tool(self.t_task, event, action, job_id, platform, title, tags, account)

    @filter.llm_tool(name="media_settings")
    async def llm_media_settings(self, event: AstrMessageEvent, action: str, model: str = "", values: str = "",
                                 preset: str = ""):
        """用自然语言配置生成模型（仅管理员）。show=查看所有模型与全局设置（model 填名字看详情）；presets=查看可用的服务商预设和接口类型；check=配置体检；add=新增模型（优先用 preset，再在 values 里补 api_key 等）；set=修改模型字段，model 留空则改全局设置；enable/disable=启用停用；remove=删除；default=设为同类型默认模型；probe=免费连通性检查；test=真实生成一次测试（可能花钱，先征得同意）；undo=撤销上一次修改。不确定字段名时先 show 或 presets。

        Args:
            action(string): show / presets / check / add / set / enable / disable / remove / default / probe / test / undo
            model(string): 模型名称（add 时是新模型的名字）
            values(string): JSON 对象，如 {"api_key":"sk-xxx","base_url":"https://...","超时":"10分钟","fallback":"备用模型名"}；test 时可填测试提示词
            preset(string): add 用的服务商预设，如 siliconflow_image、dashscope_wan_video、ark_seedance、kling_video、comfy_qwen_image、comfy_h3_video、openai_gpt_image
        """
        return await self._tool(self.t_settings, event, action, model, values, preset)

    @filter.llm_tool(name="media_automation")
    async def llm_media_automation(self, event: AstrMessageEvent, action: str, name: str = "", values: str = ""):
        """管理定时自动生成任务（仅管理员），例如“每天早上 9 点做一条治愈系猫咪短视频，发抖音”。到点后插件会让 AI 构思标题/话题/画面 → 生成 → 推送到当前会话 →（可选）提交给账号运维插件发布审批。list=查看；add=新建；set=修改；enable/disable；remove=删除；run=立即运行一次。

        Args:
            action(string): list / add / set / enable / disable / remove / run
            name(string): 任务名称
            values(string): JSON 对象。字段：schedule(如 "09:00"、"09:00,20:00"、"every 6h"、"manual")、kind(video/image/slides，slides=多图合成图文视频)、topic(主题方向，必填)、style(风格要求)、model、count(图片张数)、size(如 720x1280)、duration(秒)、platform(none/douyin/bilibili)、account、auto_publish(true/false)、max_per_day
        """
        return await self._tool(self.t_automation, event, action, name, values)

    # ==================== 命令 ====================

    @filter.command("mg", alias={"生成帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """生成插件状态与帮助"""
        specs = self.registry.specs()
        active = [j for j in self.jobs.list(limit=50, active_only=True)]
        lines = ["🎨 AI 生成插件",
                 f"模型 {len(specs)} 个：" + ("、".join(f"{s.name}({'视频' if s.kind == VIDEO else '图'})" for s in specs) or "未配置"),
                 f"默认：图片 {self.config.get('default_image_model') or '-'} / 视频 {self.config.get('default_video_model') or '-'}",
                 f"进行中任务 {len(active)} 个；自动化 {len(self.autos.recipes())} 个",
                 "", "直接对我说“画一只…”“生成一段…的视频”即可；也可用命令：",
                 "/画 <描述>  /视频 <描述>（可带图）",
                 "/mg_jobs 最近任务  /mg_cancel <任务号>",
                 "/mg_models 模型列表  /mg_probe [模型] 连通性检查（管理员）",
                 "/mg_auto [list|run 名称] 自动化（管理员）"]
        if issues := self.settings.check().get("missing"):
            lines.append("⚠️ " + "；".join(issues))
        yield event.plain_result("\n".join(lines))

    async def _quick(self, event: AstrMessageEvent, kind: str):
        text = re.sub(r"^\S+\s*", "", (event.message_str or "").strip(), count=1).strip()
        if not text:
            yield event.plain_result(f"用法：/{'画' if kind == IMAGE else '视频'} <描述>")
            return
        try:
            self._check_user(event, kind)
            refs = await self._event_images(event)
            req = GenRequest(kind=kind, prompt=text, images=refs[:1] if kind == VIDEO else refs)
            job = self.jobs.submit(req, umo=event.unified_msg_origin, requester=str(event.get_sender_id()),
                                   requester_name=str(event.get_sender_name() or ""), meta={"source": "command"})
        except GenError as e:
            yield event.plain_result(f"❌ {e}")
            return
        yield event.plain_result(f"⏳ 已提交（{job.model}，任务 {job.id}），完成后自动发送")

    @filter.command("画", alias={"mg_image"})
    async def cmd_image(self, event: AstrMessageEvent):
        """生成图片：/画 <描述>（可附图作参考）"""
        async for r in self._quick(event, IMAGE):
            yield r

    @filter.command("视频", alias={"mg_video"})
    async def cmd_video(self, event: AstrMessageEvent):
        """生成视频：/视频 <描述>（可附图作首帧）"""
        async for r in self._quick(event, VIDEO):
            yield r

    @filter.command("mg_jobs")
    async def cmd_jobs(self, event: AstrMessageEvent):
        """最近的生成任务"""
        jobs = self.jobs.list(None if event.is_admin() else event.unified_msg_origin, 10)
        if not jobs:
            yield event.plain_result("还没有生成任务")
            return
        icon = {"succeeded": "✅", "failed": "❌", "cancelled": "🚫"}
        lines = [f"{icon.get(j.status, '⏳')} {j.id} {j.kind} {j.model} {j.status} "
                 f"{time.strftime('%m-%d %H:%M', time.localtime(j.created))} {j.request.get('prompt', '')[:20]}"
                 + (f"\n   {j.error[:60]}" if j.status == "failed" else "") for j in jobs]
        yield event.plain_result("\n".join(lines))

    @filter.command("mg_cancel")
    async def cmd_cancel(self, event: AstrMessageEvent, job_id: str = ""):
        """取消生成任务：/mg_cancel <任务号>"""
        job = self.jobs.find(job_id, event.unified_msg_origin)
        if not job:
            yield event.plain_result("找不到这个任务")
            return
        if job.requester != str(event.get_sender_id()) and not event.is_admin():
            yield event.plain_result("只能取消自己的任务")
            return
        yield event.plain_result(await self.jobs.cancel(job))

    @filter.command("mg_models")
    async def cmd_models(self, event: AstrMessageEvent):
        """生成模型列表"""
        rows = []
        for s in self.registry.specs(include_disabled=True):
            flag = "" if s.enabled else "（停用）"
            if self.registry.breaker.is_open(s.name):
                flag += f"（熔断 {self.registry.breaker.left(s.name)}s）"
            rows.append(f"• {s.name}{flag} [{'视频' if s.kind == VIDEO else '图片'}] {self.registry.adapter(s).label}"
                        f" {s.model or s.options.get('workflow', '')}" + (f"\n  {s.description[:50]}" if s.description else ""))
        yield event.plain_result("\n".join(rows) or "还没有配置模型。管理员可以在 WebUI 插件配置里添加，或直接对我说“帮我加一个…模型”。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mg_probe")
    async def cmd_probe(self, event: AstrMessageEvent, name: str = ""):
        """检查模型连通性（不产生费用）：/mg_probe [模型名]"""
        specs = [self.registry.get(name, True)] if name else self.registry.specs(include_disabled=True)
        lines = []
        for s in [x for x in specs if x]:
            try:
                lines.append(f"✅ {s.name}: {await self.registry.adapter(s).probe()}")
                self.registry.breaker.reset(s.name)
            except Exception as e:
                lines.append(f"❌ {s.name}: {str(e)[:150]}")
        yield event.plain_result("\n".join(lines) or "没有找到模型")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mg_auto")
    async def cmd_auto(self, event: AstrMessageEvent, action: str = "list", name: str = ""):
        """自动化任务：/mg_auto list | /mg_auto run <名称>"""
        if action == "run" and name:
            res = await self.t_automation(event, "run", name, "")
            yield event.plain_result(_dump(res))
            return
        items = self.autos.view()
        if not items:
            yield event.plain_result("还没有自动化任务。可以对我说：每天 9 点生成一条…的短视频并发到抖音")
            return
        yield event.plain_result("\n".join(
            f"{'🟢' if i.get('enabled', True) else '⚪'} {i['name']} [{i.get('schedule')}] {i.get('kind')} → {i.get('platform', 'none')}"
            f"\n   主题：{i.get('topic', '')}  下次：{i.get('next_run', '-')}  上次：{i.get('last_result', '-')}" for i in items))
