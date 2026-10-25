"""自动化生成：配方（recipe）CRUD + 轻量定时器 + 运行状态。

配方存在插件配置的 automations 列表里（WebUI 和聊天里都能改）；运行状态（上次运行、今日次数、
最近标题——用来避免选题重复）存在 automation_state.json。真正的“构思 → 生成 → 发布”流程由插件注入的
runner 回调完成，这里只负责“什么时候该跑、跑没跑、别跑重了”。

schedule 写法：
  10:00 / 09:30,20:00 / 每天10点 / 每天 9点半   —— 每天固定时间（错过 2 小时内会补跑，更久的跳过）
  every 6h / every 90m / 每6小时 / 每30分钟       —— 按间隔
  manual / 手动 / 空                             —— 只手动触发
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta

from .types import GenError, log

CATCH_UP_S = 7200
FIELDS = {
    "name": "", "enabled": True, "schedule": "10:00", "kind": "video", "topic": "", "style": "", "model": "",
    "count": 1, "size": "", "duration": 0.0, "platform": "none", "account": "", "auto_publish": False,
    "notify_umo": "", "max_per_day": 3,
}
ALIASES = {"名称": "name", "时间": "schedule", "计划": "schedule", "频率": "schedule", "类型": "kind", "主题": "topic",
           "方向": "topic", "风格": "style", "要求": "style", "模型": "model", "张数": "count", "数量": "count",
           "尺寸": "size", "时长": "duration", "平台": "platform", "账号": "account", "自动发布": "auto_publish",
           "启用": "enabled", "每天上限": "max_per_day", "推送会话": "notify_umo"}
KIND_ALIASES = {"视频": "video", "图片": "image", "图": "image", "图文": "slides", "图文视频": "slides", "幻灯片": "slides"}
PLATFORM_ALIASES = {"抖音": "douyin", "dy": "douyin", "b站": "bilibili", "bili": "bilibili", "哔哩哔哩": "bilibili",
                    "不发布": "none", "无": "none", "": "none"}


def parse_schedule(s: str) -> tuple[str, object]:
    s = (s or "").strip().lower()
    if not s or s in ("manual", "手动", "不定时", "none"):
        return "manual", None
    m = re.match(r"^(?:every\s*|每\s*)(\d+(?:\.\d+)?)\s*(m|min|分钟|分|h|hour|小时|d|day|天)$", s)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        sec = n * (60 if unit in ("m", "min", "分钟", "分") else 86400 if unit in ("d", "day", "天") else 3600)
        if sec < 600:
            raise GenError("间隔不能小于 10 分钟", user_error=True)
        return "interval", sec
    times = []
    for hh, mm, half in re.findall(r"(\d{1,2})\s*(?:[:：点时]\s*(\d{1,2})?\s*(半)?)", s):
        h, mi = int(hh), (30 if half else int(mm or 0))
        if h > 23 or mi > 59:
            raise GenError(f"时间不对：{hh}:{mm}", user_error=True)
        times.append((h, mi))
    if not times:
        raise GenError(f"看不懂的时间计划：{s}（例：10:00、09:30,20:00、every 6h、manual）", user_error=True)
    return "daily", sorted(set(times))


def normalize_recipe(changes: dict) -> dict:
    out = {}
    for raw, v in changes.items():
        k = ALIASES.get(str(raw).strip(), str(raw).strip())
        if k not in FIELDS:
            raise GenError(f"不认识的字段“{raw}”。可用：{', '.join(FIELDS)}", user_error=True)
        d = FIELDS[k]
        if isinstance(d, bool):
            v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on", "是", "开", "开启", "要")
        elif isinstance(d, (int, float)):
            v = type(d)(float(str(v).replace("秒", "").replace("张", "").strip() or 0))
        else:
            v = str(v).strip()
        if k == "kind":
            v = KIND_ALIASES.get(v, v)
            if v not in ("video", "image", "slides"):
                raise GenError("kind 只能是 video / image / slides", user_error=True)
        if k == "platform":
            v = PLATFORM_ALIASES.get(v.lower(), v.lower())
            if v not in ("none", "douyin", "bilibili"):
                raise GenError("platform 只能是 none / douyin / bilibili", user_error=True)
        if k == "schedule":
            parse_schedule(v)
        out[k] = v
    return out


class Automations:
    def __init__(self, config, data_dir: str, runner, saver=None):
        self.config = config
        self.runner = runner  # async (recipe: dict, reason: str) -> dict
        self.saver = saver or (lambda: getattr(config, "save_config", lambda: None)())
        self.state_path = os.path.join(data_dir, "automation_state.json")
        self.state: dict[str, dict] = {}
        self.running: set[str] = set()
        self._task: asyncio.Task | None = None
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (FileNotFoundError, ValueError):
            self.state = {}

    # ── 配方 ─────────────────────────────
    def recipes(self) -> list[dict]:
        if not isinstance(self.config.get("automations"), list):
            self.config["automations"] = []
        return self.config["automations"]

    def find(self, name: str) -> dict | None:
        name = (name or "").strip().lower()
        for r in self.recipes():
            if str(r.get("name", "")).lower() == name:
                return r
        hits = [r for r in self.recipes() if name and name in str(r.get("name", "")).lower()]
        return hits[0] if len(hits) == 1 else None

    def add(self, values: dict) -> dict:
        item = {"__template_key": "recipe", **FIELDS, **normalize_recipe(values)}
        if not item["name"]:
            raise GenError("请给自动化任务起个名字", user_error=True)
        if self.find(item["name"]) and self.find(item["name"])["name"].lower() == item["name"].lower():
            raise GenError(f"已有同名任务 {item['name']}", user_error=True)
        if not item["topic"]:
            raise GenError("请说明主题/创作方向（topic）", user_error=True)
        self.recipes().append(item)
        self._arm(item["name"])
        self.saver()
        return item

    def update(self, name: str, changes: dict) -> dict:
        r = self.find(name)
        if not r:
            raise GenError(f"没有自动化任务 {name}", user_error=True)
        r.update(normalize_recipe(changes))
        if "schedule" in changes:
            self._arm(r["name"], reset=True)
        self.saver()
        return r

    def remove(self, name: str) -> dict:
        r = self.find(name)
        if not r:
            raise GenError(f"没有自动化任务 {name}", user_error=True)
        self.recipes().remove(r)
        self.state.pop(r["name"], None)
        self._save_state()
        self.saver()
        return r

    def view(self) -> list[dict]:
        out = []
        for r in self.recipes():
            st = self.state.get(r.get("name", ""), {})
            item = {k: r.get(k) for k in ("name", "enabled", "schedule", "kind", "topic", "model", "platform",
                                          "auto_publish") if r.get(k) not in ("", None)}
            item["topic"] = str(item.get("topic", ""))[:40]
            if st.get("last_run"):
                item["last_run"] = time.strftime("%m-%d %H:%M", time.localtime(st["last_run"]))
                item["last_result"] = st.get("last_result", "")[:60]
            if nxt := self.next_run(r):
                item["next_run"] = time.strftime("%m-%d %H:%M", time.localtime(nxt))
            out.append(item)
        return out

    # ── 状态 ─────────────────────────────
    def _save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def _arm(self, name: str, reset: bool = False) -> None:
        st = self.state.setdefault(name, {})
        if reset or "armed_at" not in st:
            st["armed_at"] = time.time()
        self._save_state()

    def record(self, name: str, result: str, title: str = "") -> None:
        st = self.state.setdefault(name, {})
        st["last_result"] = result[:200]
        if title:
            st["titles"] = ([title] + st.get("titles", []))[:15]
        self._save_state()

    def recent_titles(self, name: str) -> list[str]:
        return self.state.get(name, {}).get("titles", [])

    def runs_today(self, name: str) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(1 for t in self.state.get(name, {}).get("runs", []) if t.startswith(today))

    # ── 定时 ─────────────────────────────
    def next_run(self, r: dict, now: float | None = None) -> float | None:
        if not r.get("enabled", True):
            return None
        try:
            mode, val = parse_schedule(r.get("schedule", ""))
        except GenError:
            return None
        st = self.state.get(r.get("name", ""), {})
        base = max(st.get("last_run", 0), st.get("armed_at", 0))
        now = now or time.time()
        if mode == "interval":
            return base + val
        if mode == "daily":
            day = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
            for offset in (0, 1):
                for h, m in val:
                    t = (day + timedelta(days=offset)).replace(hour=h, minute=m).timestamp()
                    if t > base and t > now - CATCH_UP_S:
                        return t
        return None

    def due(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        out = []
        for r in self.recipes():
            name = r.get("name", "")
            if not name or name in self.running:
                continue
            nxt = self.next_run(r, now)
            if nxt and nxt <= now:
                if self.runs_today(name) >= int(r.get("max_per_day") or 3):
                    self.state.setdefault(name, {})["last_run"] = now  # 今天额度用完：跳过这一档
                    self.record(name, "今日运行次数已达上限，跳过")
                    continue
                out.append(r)
        return out

    async def trigger(self, r: dict, reason: str) -> dict:
        name = r["name"]
        if name in self.running:
            return {"error": f"{name} 正在运行中"}
        self.running.add(name)
        st = self.state.setdefault(name, {})
        st["last_run"] = time.time()
        st["runs"] = ([datetime.now().strftime("%Y-%m-%d %H:%M")] + st.get("runs", []))[:30]
        self._save_state()
        try:
            res = await self.runner(r, reason)
            self.record(name, json.dumps(res, ensure_ascii=False)[:200])
            return res
        except Exception as e:
            log.exception(f"[media_gen] 自动化 {name} 运行失败")
            self.record(name, f"失败: {e}")
            return {"error": str(e)[:300]}
        finally:
            self.running.discard(name)

    def start(self) -> None:
        for r in self.recipes():
            if r.get("name"):
                self.state.setdefault(r["name"], {}).setdefault("armed_at", time.time())
        self._save_state()
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                for r in self.due():
                    asyncio.create_task(self.trigger(r, "schedule"))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[media_gen] 自动化定时器出错")
            await asyncio.sleep(30)


def parse_plan(text: str) -> dict:
    """从 LLM 回复里抠出 JSON（容忍 ```json 包裹和前后废话）。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复里没有 JSON")
    plan = json.loads(text[start:end + 1])
    if not isinstance(plan, dict) or not plan.get("prompt"):
        raise ValueError("JSON 缺少 prompt")
    tags = plan.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[\s,，#]+", tags) if t]
    plan["tags"] = [str(t).lstrip("#")[:20] for t in tags][:6]
    plan["title"] = str(plan.get("title") or "")[:40]
    if isinstance(plan.get("prompts"), list):
        plan["prompts"] = [str(p) for p in plan["prompts"] if p]
    return plan
