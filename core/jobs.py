"""生成任务调度：排队 → 提交 → 轮询 → 下载 → 校验，带超时、重试、熔断、回退、持久化与重启续跑。

稳定性要点
- 每个任务一条持久化记录（jobs.json，原子写入）。远端任务 ID 一拿到就落盘，插件重启/重载后继续轮询，
  不会重复提交（重复提交 = 重复扣费）
- 队列分组：同一个 queue_group（默认同一个 ComfyUI 地址）共享并发上限，排队时间单独计时（queue_timeout_s）
- 三层超时：单次 HTTP（connect/read）、提交（submit_timeout_s）、整体（timeout_s，从拿到并发槽位开始算）
- 轮询容错：网络抖动/5xx 连续 N 次才算失败；轮询间隔逐步拉长，避免长视频把对方接口刷爆
- 失败分级：内容违规/参数错误/余额不足 → 直接失败不回退；超时/限流/服务挂了 → 熔断计数 + 换回退模型
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field

from .http import Deadline
from .registry import Registry
from .types import IMAGE, VIDEO, Asset, GenError, GenRequest, GenTimeout, log

ACTIVE = ("queued", "submitting", "running", "downloading")
DONE = ("succeeded", "failed", "cancelled")


@dataclass
class Job:
    id: str
    kind: str
    request: dict
    model: str = ""  # 当前/最终使用的模型
    wanted_model: str = ""  # 用户/LLM 指定的模型（空=默认）
    umo: str = ""
    requester: str = ""
    requester_name: str = ""
    status: str = "queued"
    handle: dict | None = None
    tried: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    progress: float | None = None
    files: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    started: float = 0
    finished: float = 0
    deliver: bool = True
    meta: dict = field(default_factory=dict)  # title / tags / automation / publish …

    @property
    def error(self) -> str:
        return self.errors[-1] if self.errors else ""

    def brief(self) -> dict:
        d = {"job_id": self.id, "kind": self.kind, "status": self.status, "model": self.model,
             "prompt": (self.request.get("prompt") or "")[:60]}
        if self.status in ACTIVE:
            d["waited_s"] = int(time.time() - self.created)
            if self.progress is not None:
                d["progress"] = round(self.progress)
        if self.status == "succeeded":
            d["files"] = len(self.files)
            d["cost_s"] = int(self.finished - self.created)
        if self.status == "failed":
            d["error"] = self.error[:200]
        if self.meta.get("publish"):
            d["publish"] = self.meta["publish"]
        return d


def _sniff(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    return ""


async def probe_video(path: str, timeout: float = 30) -> dict:
    """ffprobe 校验视频可解码；没有 ffprobe 时跳过。"""
    if not shutil.which("ffprobe"):
        return {}
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {}
    if proc.returncode != 0:
        raise GenError(f"生成的视频无法解码: {err.decode(errors='replace')[:200]}", retryable=True, code="bad_video")
    info = json.loads(out or b"{}")
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration": float((info.get("format") or {}).get("duration") or 0), "width": v.get("width"),
            "height": v.get("height"), "audio": any(s.get("codec_type") == "audio" for s in info.get("streams", []))}


class JobManager:
    def __init__(self, registry: Registry, config: dict, data_dir: str, on_done=None, on_progress=None):
        self.registry = registry
        self.config = config
        self.data_dir = data_dir
        self.out_dir = os.path.join(data_dir, "outputs")
        self.store = os.path.join(data_dir, "jobs.json")
        self.on_done = on_done
        self.on_progress = on_progress
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._sems: dict[str, tuple[int, asyncio.Semaphore]] = {}
        self._save_lock = asyncio.Lock()
        os.makedirs(self.out_dir, exist_ok=True)
        self._load()

    # ── 持久化 ─────────────────────────────
    def _load(self) -> None:
        try:
            with open(self.store, encoding="utf-8") as f:
                for d in json.load(f):
                    job = Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__})
                    self.jobs[job.id] = job
        except FileNotFoundError:
            pass
        except Exception as e:  # 记录损坏不影响启动
            log.warning(f"[media_gen] 读取任务记录失败，已备份并重建: {e}")
            shutil.copy(self.store, self.store + f".broken-{int(time.time())}")

    def save(self) -> None:
        keep = int(self.config.get("keep_job_records", 300) or 300)
        jobs = sorted(self.jobs.values(), key=lambda j: j.created)
        for j in jobs[:-keep]:
            if j.status in DONE:
                self.jobs.pop(j.id, None)
        tmp = self.store + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(j) for j in self.jobs.values()], f, ensure_ascii=False)
        os.replace(tmp, self.store)

    # ── 对外接口 ─────────────────────────────
    def find(self, ref: str, umo: str = "") -> Job | None:
        ref = (ref or "").strip()
        if not ref or ref in ("last", "最近", "上一个"):
            pool = [j for j in self.jobs.values() if not umo or j.umo == umo]
            return max(pool, key=lambda j: j.created) if pool else None
        if ref in self.jobs:
            return self.jobs[ref]
        hits = [j for j in self.jobs.values() if j.id.startswith(ref)]
        return hits[0] if len(hits) == 1 else None

    def list(self, umo: str = "", limit: int = 10, active_only: bool = False) -> list[Job]:
        pool = [j for j in self.jobs.values() if (not umo or j.umo == umo) and (not active_only or j.status in ACTIVE)]
        return sorted(pool, key=lambda j: j.created, reverse=True)[:limit]

    def count_today(self, requester: str, kind: str) -> int:
        start = time.time() - 86400
        return sum(1 for j in self.jobs.values() if j.requester == requester and j.kind == kind
                   and j.created > start and j.status != "cancelled")

    def queue_position(self, job: Job) -> int:
        return sum(1 for j in self.jobs.values() if j.status in ACTIVE and j.created < job.created
                   and j.model == job.model)

    def submit(self, req: GenRequest, *, model: str = "", umo: str = "", requester: str = "", requester_name: str = "",
               deliver: bool = True, meta: dict | None = None) -> Job:
        chain = self.registry.chain(req.kind, model)  # 先校验模型，非法直接抛给调用方
        spec = chain[0]
        if not req.size and spec.default_size:
            req.size = spec.default_size
        if req.kind == VIDEO and not req.duration and spec.default_duration:
            req.duration = spec.default_duration
        max_active = int(self.config.get("max_active_jobs", 20) or 20)
        if sum(1 for j in self.jobs.values() if j.status in ACTIVE) >= max_active:
            raise GenError(f"排队任务已达上限 {max_active}，请稍后再试", user_error=True)
        job = Job(id=uuid.uuid4().hex[:10], kind=req.kind, request=req.to_dict(), model=spec.name,
                  wanted_model=model, umo=umo, requester=requester, requester_name=requester_name,
                  deliver=deliver, meta=meta or {})
        self.jobs[job.id] = job
        self.save()
        self._start(job)
        return job

    def _start(self, job: Job) -> None:
        task = asyncio.create_task(self._run(job))
        self.tasks[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: self.tasks.pop(jid, None))

    async def wait(self, job: Job, timeout: float) -> bool:
        task = self.tasks.get(job.id)
        if not task:
            return job.status in DONE
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def cancel(self, job: Job) -> str:
        if job.status in DONE:
            return f"任务 {job.id} 已经是 {job.status}"
        task = self.tasks.get(job.id)
        handle, model = job.handle, job.model
        job.status = "cancelled"
        job.finished = time.time()
        self.save()
        if task:
            task.cancel()
        if handle:
            spec = self.registry.get(model, include_disabled=True)
            if spec:
                try:
                    await asyncio.wait_for(self.registry.adapter(spec).cancel(handle), 20)
                except Exception as e:
                    log.info(f"[media_gen] 远端取消 {job.id} 失败（忽略）: {e}")
        return f"已取消任务 {job.id}"

    def retry(self, job: Job) -> Job:
        req = GenRequest.from_dict(job.request)
        return self.submit(req, model=job.wanted_model, umo=job.umo, requester=job.requester,
                           requester_name=job.requester_name, deliver=job.deliver,
                           meta={k: v for k, v in job.meta.items() if k != "publish"} | {"retry_of": job.id})

    def resume_all(self) -> int:
        """插件启动时续跑未完成的任务：已拿到远端 ID 的继续轮询；还没提交的重新排队；
        提交到一半（可能已扣费但没拿到 ID）的标记失败，交给用户决定是否重试。"""
        n = 0
        for job in list(self.jobs.values()):
            if job.status not in ACTIVE or job.id in self.tasks:
                continue
            spec = self.registry.get(job.model, include_disabled=True)
            resumable = spec is not None and self.registry.adapter(spec).resumable
            if job.handle and not job.handle.get("sync") and resumable:
                pass
            elif job.status == "queued" or (job.handle and job.handle.get("sync")):
                job.handle = job.handle if job.handle and job.handle.get("sync") else None
            elif spec is not None and spec.provider == "comfyui":
                job.handle = None  # 本地免费，直接重跑
            else:
                job.status = "failed"
                job.errors.append("插件重启时任务正在提交，结果未知（为防止重复扣费未自动重试，可用 retry 重新生成）")
                job.finished = time.time()
                continue
            n += 1
            self._start(job)
        self.save()
        return n

    async def shutdown(self) -> None:
        """重载插件时：停止本地轮询但保留任务状态（下次启动续跑），不取消远端任务。"""
        for t in list(self.tasks.values()):
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.save()

    # ── 执行 ─────────────────────────────
    def _sem(self, group: str, limit: int) -> asyncio.Semaphore:
        cur = self._sems.get(group)
        if not cur or cur[0] != limit:
            cur = (limit, asyncio.Semaphore(limit))
            self._sems[group] = cur
        return cur[1]

    def _timeout(self, spec) -> float:
        if spec.timeout_s:
            return spec.timeout_s
        return float(self.config.get("video_timeout_s" if spec.kind == VIDEO else "image_timeout_s",
                                     1800 if spec.kind == VIDEO else 300))

    async def _run(self, job: Job) -> None:
        req = GenRequest.from_dict(job.request)
        try:
            chain = self.registry.chain(job.kind, job.wanted_model)
            if job.meta.get("no_fallback"):  # 测试某个模型时不要悄悄换成别的
                chain = chain[:1]
            if job.handle and job.model:  # 续跑：先接着原模型轮询，失败再走没试过的回退模型
                cur = self.registry.get(job.model, include_disabled=True)
                chain = ([cur] if cur else []) + [s for s in chain if s.name != job.model and s.name not in job.tried]
            last_err: GenError | None = None
            attempted: set[str] = set()
            for spec in chain:
                if spec.name in attempted:
                    continue
                attempted.add(spec.name)
                if self.registry.breaker.is_open(spec.name):
                    job.errors.append(f"{spec.name}: 熔断中（{self.registry.breaker.left(spec.name)}s 后恢复）")
                    continue
                try:
                    await self._attempt(job, spec, req)
                    self.registry.breaker.success(spec.name)
                    job.status = "succeeded"
                    break
                except asyncio.CancelledError:
                    raise
                except GenError as e:
                    last_err = e
                    job.errors.append(f"{spec.name}: {e}"[:400])
                    job.handle = None
                    log.warning(f"[media_gen] 任务 {job.id} 在 {spec.name} 失败: {e}")
                    if e.user_error:
                        break
                    self.registry.breaker.failure(spec.name)
                    if isinstance(e, GenTimeout) and not self.config.get("fallback_on_timeout", True):
                        break
                except Exception as e:  # 适配器 bug 等意外错误：记下来，换下一个
                    last_err = GenError(f"{type(e).__name__}: {e}")
                    job.errors.append(f"{spec.name}: 内部错误 {type(e).__name__}: {e}"[:400])
                    job.handle = None
                    log.exception(f"[media_gen] 任务 {job.id} 在 {spec.name} 出现意外错误")
                    self.registry.breaker.failure(spec.name)
            if job.status != "succeeded":
                job.status = "failed"
                if not job.errors:
                    job.errors.append(str(last_err or "没有可用的模型（都在熔断中）"))
        except asyncio.CancelledError:
            if job.status != "cancelled":  # 插件重载：保留状态，下次续跑
                self.save()
            raise
        except GenError as e:
            job.status = "failed"
            job.errors.append(str(e))
        except Exception as e:  # 兜底：任何意外都不能让任务卡在进行中
            job.status = "failed"
            job.errors.append(f"内部错误 {type(e).__name__}: {e}"[:300])
            try:
                log.exception(f"[media_gen] 任务 {job.id} 调度出错")
            except Exception:
                pass
        job.finished = time.time()
        job.progress = None
        self.save()
        if self.on_done:
            try:
                await self.on_done(job)
            except Exception:
                log.exception(f"[media_gen] 任务 {job.id} 完成回调出错")

    async def _attempt(self, job: Job, spec, req: GenRequest) -> None:
        adapter = self.registry.adapter(spec)
        group = spec.queue_group or f"model:{spec.name}"
        limit = spec.max_concurrency or int(self.config.get(
            "default_video_concurrency" if spec.kind == VIDEO else "default_image_concurrency",
            1 if spec.kind == VIDEO else 2) or 1)
        sem = self._sem(group, limit)
        job.model = spec.name
        if spec.name not in job.tried:
            job.tried.append(spec.name)
        job.status = "queued"
        self.save()
        queue_timeout = float(self.config.get("queue_timeout_s", 7200) or 7200)
        try:
            await asyncio.wait_for(sem.acquire(), queue_timeout)
        except asyncio.TimeoutError as e:
            raise GenTimeout(f"排队超过 {int(queue_timeout)} 秒仍未轮到") from e
        try:
            deadline = Deadline(self._timeout(spec))
            job.started = job.started or time.time()
            if not job.handle:
                job.status = "submitting"
                self.save()
                sub_to = (spec.submit_timeout_s or float(self.config.get("submit_timeout_s", 180) or 180)) + 10
                try:
                    job.handle = await asyncio.wait_for(adapter.submit(req), min(sub_to, max(deadline.left(), 5)))
                except asyncio.TimeoutError as e:
                    raise GenTimeout(f"提交超过 {int(sub_to)} 秒无响应") from e
                self.save()
            job.status = "running"
            self.save()
            assets = await self._poll(job, adapter, spec, deadline)
            job.status = "downloading"
            self.save()
            job.files = await self._download(job, adapter, assets)
            if hasattr(adapter, "after_job"):
                await adapter.after_job()
        except BaseException:
            if hasattr(adapter, "after_job"):
                try:
                    await asyncio.wait_for(adapter.after_job(), 30)
                except Exception:
                    pass
            raise
        finally:
            sem.release()

    async def _poll(self, job: Job, adapter, spec, deadline: Deadline) -> list[Asset]:
        interval = spec.poll_interval_s or adapter.poll_interval
        max_interval = float(self.config.get("max_poll_interval_s", 30) or 30)
        max_errors = int(self.config.get("max_poll_errors", 8) or 8)
        errors = 0
        last_state = ""
        while True:
            if deadline.expired:
                try:
                    await asyncio.wait_for(adapter.cancel(job.handle or {}), 20)
                except Exception:
                    pass
                raise GenTimeout(f"超过 {int(deadline.seconds)} 秒仍未完成（已尝试取消远端任务）")
            try:
                res = await asyncio.wait_for(adapter.poll(job.handle), 90)
                errors = 0
            except (GenError, asyncio.TimeoutError) as e:
                if isinstance(e, GenError) and not e.retryable and e.status in (400, 401, 403, 404):
                    raise
                errors += 1
                if errors >= max_errors:
                    raise GenError(f"连续 {errors} 次查询失败: {e}", retryable=True, code="poll") from e
                log.info(f"[media_gen] 任务 {job.id} 查询失败（{errors}/{max_errors}）: {e}")
                await asyncio.sleep(min(interval * (1 + errors), max_interval))
                continue
            if res.state == "succeeded":
                if not res.assets:
                    raise GenError("任务成功但没有产物", retryable=True, code="no_output")
                return res.assets
            if res.state == "failed":
                raise GenError(res.error or "生成失败", user_error=res.user_error, retryable=not res.user_error)
            job.progress = res.progress
            if res.state != last_state:
                last_state = res.state
                if self.on_progress:
                    await self.on_progress(job, res.state)
            await asyncio.sleep(min(interval, max(deadline.left(), 1)))
            interval = min(interval * 1.25, max_interval)

    async def _download(self, job: Job, adapter, assets: list[Asset]) -> list[str]:
        day = time.strftime("%Y%m%d")
        folder = os.path.join(self.out_dir, day)
        os.makedirs(folder, exist_ok=True)
        max_mb = float(self.config.get("max_file_mb", 512) or 512)
        files = []
        for i, a in enumerate(assets[: max(1, int(job.request.get("n") or 1)) if job.kind == IMAGE else 1]):
            dest = os.path.join(folder, f"{job.id}_{i}{a.ext or ''}")
            if a.path and os.path.isfile(a.path):
                shutil.copy(a.path, dest)
            elif a.data:
                from .http import save_b64
                save_b64(a.data, dest)
            elif a.url:
                await adapter.http.download(a.url, dest, headers=a.headers or None, max_bytes=int(max_mb * 1048576))
            else:
                continue
            ext = _sniff(dest) or os.path.splitext(dest)[1] or (".mp4" if job.kind == VIDEO else ".png")
            final = os.path.splitext(dest)[0] + ext
            if final != dest:
                os.replace(dest, final)
            if os.path.getsize(final) < 64:
                raise GenError("产物文件异常（过小）", retryable=True, code="bad_file")
            if job.kind == VIDEO:
                info = await probe_video(final)
                if info:
                    job.meta["video"] = info
            files.append(final)
        if not files:
            raise GenError("没有可下载的产物", retryable=True, code="no_output")
        return files

    def cleanup(self) -> int:
        days = float(self.config.get("keep_files_days", 7) or 7)
        cutoff = time.time() - days * 86400
        removed = 0
        for root, _dirs, files in os.walk(self.out_dir):
            for f in files:
                p = os.path.join(root, f)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
        for d in os.listdir(self.out_dir):
            p = os.path.join(self.out_dir, d)
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        return removed
