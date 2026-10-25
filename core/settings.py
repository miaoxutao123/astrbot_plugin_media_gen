"""自然语言配置的落地层：LLM 把用户的话翻译成 add/set/remove… 调用，这里负责校验、类型转换、备份与回滚。

- 字段名接受中文别名（“地址”“密钥”“超时”…），值按 schema 类型自动转换（"10分钟" → 600）
- 每次写入前把整份配置备份到 config_backups/，undo 可回滚最近一次
- 展示时密钥打码，绝不把完整 key 回显到聊天记录里
"""

from __future__ import annotations

import copy
import json
import os
import re
import time

from ..providers import ADAPTERS, GENERIC_PRESETS, VENDOR_PRESETS
from ..providers.base import COMMON_KEYS
from .types import GenError

SECRET_KEYS = {"api_key", "secret_key", "access_key"}

ALIASES = {
    "名称": "name", "名字": "name", "类型": "kind", "地址": "base_url", "接口地址": "base_url", "url": "base_url",
    "baseurl": "base_url", "密钥": "api_key", "key": "api_key", "apikey": "api_key", "令牌": "api_key", "token": "api_key",
    "模型": "model", "模型id": "model", "描述": "description", "简介": "description", "说明": "description",
    "超时": "timeout_s", "超时时间": "timeout_s", "提交超时": "submit_timeout_s", "轮询间隔": "poll_interval_s",
    "并发": "max_concurrency", "并发数": "max_concurrency", "队列": "queue_group", "分组": "queue_group",
    "回退": "fallback", "备用": "fallback", "备选": "fallback", "代理": "proxy", "尺寸": "default_size",
    "分辨率": "default_size", "默认尺寸": "default_size", "时长": "default_duration", "默认时长": "default_duration",
    "启用": "enabled", "开关": "enabled", "费用": "cost_hint", "价格": "cost_hint", "额外参数": "extra",
    "工作流": "workflow", "步数": "steps", "ak": "access_key", "sk": "secret_key",
    "默认图片模型": "default_image_model", "默认视频模型": "default_video_model",
    "普通用户出图": "allow_non_admin_image", "普通用户视频": "allow_non_admin_video",
    "图片上限": "user_daily_image_limit", "视频上限": "user_daily_video_limit",
    "视频超时": "video_timeout_s", "图片超时": "image_timeout_s", "等待时间": "tool_wait_s",
    "自动回退": "auto_fallback", "全局代理": "proxy", "保留天数": "keep_files_days",
}

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(秒|s|sec|分钟|分|min|m|小时|h|hour)?\s*$", re.I)


def mask(v: str) -> str:
    v = str(v or "")
    if not v:
        return "（未设置）"
    return f"{v[:4]}…{v[-3:]}（已设置）" if len(v) > 10 else "***（已设置）"


def to_seconds(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = _DURATION.match(str(v))
    if not m:
        raise ValueError(f"看不懂的时间：{v}")
    n, unit = float(m.group(1)), (m.group(2) or "").lower()
    return n * (60 if unit in ("分钟", "分", "min", "m") else 3600 if unit in ("小时", "h", "hour") else 1)


def _coerce(key: str, value, default):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on", "开", "开启", "打开", "启用", "是", "允许"):
            return True
        if s in ("0", "false", "no", "off", "关", "关闭", "禁用", "否", "不允许"):
            return False
        raise ValueError(f"{key} 需要 是/否")
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        n = to_seconds(value) if key.endswith("_s") else float(str(value).strip())
        return int(n) if isinstance(default, int) else n
    if key == "extra" and isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if key == "default_size":
        s = str(value).lower().replace("*", "x").replace("×", "x").replace(" ", "")
        if s and not re.fullmatch(r"\d{2,5}x\d{2,5}", s):
            raise ValueError("尺寸格式应为 宽x高，如 1280x720")
        return s
    return str(value).strip() if not isinstance(value, (list, dict)) else value


def field_defaults(provider: str) -> dict:
    from ..tools.gen_schema import COMMON
    d = {k: v.get("default") for k, v in COMMON.items()}
    cls = ADAPTERS.get(provider)
    if cls:
        d["kind"] = cls.kinds[0]
        d.update({k: default for k, (_desc, default) in cls.fields.items()})
    return d


class Settings:
    def __init__(self, config, data_dir: str, saver=None):
        self.config = config
        self.backup_dir = os.path.join(data_dir, "config_backups")
        self.saver = saver or (lambda: getattr(config, "save_config", lambda: None)())

    # ── 基础 ─────────────────────────────
    def models(self) -> list[dict]:
        if not isinstance(self.config.get("models"), list):
            self.config["models"] = []
        return self.config["models"]

    def find(self, name: str) -> dict | None:
        name = (name or "").strip().lower()
        for m in self.models():
            if str(m.get("name", "")).lower() == name:
                return m
        hits = [m for m in self.models() if name and name in str(m.get("name", "")).lower()]
        return hits[0] if len(hits) == 1 else None

    def _backup(self) -> None:
        os.makedirs(self.backup_dir, exist_ok=True)
        snap = {k: copy.deepcopy(v) for k, v in dict(self.config).items()}
        path = os.path.join(self.backup_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        files = sorted(os.listdir(self.backup_dir))
        for old in files[:-20]:
            os.remove(os.path.join(self.backup_dir, old))

    def _commit(self) -> None:
        self.saver()

    @staticmethod
    def view(m: dict, full: bool = False) -> dict:
        out = {}
        for k, v in m.items():
            if k == "__template_key":
                out["provider"] = v
            elif k in SECRET_KEYS:
                if v or full:
                    out[k] = mask(v)
            elif full or (v not in ("", None, 0, 0.0, False) or k == "enabled"):
                out[k] = v
        return out

    # ── 动作 ─────────────────────────────
    def show(self, name: str = "") -> dict:
        if name:
            m = self.find(name)
            if not m:
                return {"error": f"没有模型 {name}", "models": [x.get("name") for x in self.models()]}
            return {"model": self.view(m, full=True)}
        top = {k: v for k, v in dict(self.config).items() if k not in ("models", "automations")}
        top["proxy"] = top.get("proxy") or ""
        return {"models": [self.view(m) for m in self.models()], "settings": top}

    @staticmethod
    def presets() -> dict:
        out = {}
        for k, p in VENDOR_PRESETS.items():
            out[k] = {"provider": p["provider"], "kind": p["kind"], "model": p.get("model", ""),
                      "needs": "AK+SK" if p["provider"] == "kling" else
                      ("无需 key（本地）" if p["provider"] == "comfyui" or "jimeng" in k else "api_key")}
        return {"vendor_presets": out, "providers": {k: c.label for k, c in ADAPTERS.items()},
                "generic_presets": list(GENERIC_PRESETS)}

    def normalize(self, provider: str, changes: dict) -> dict:
        defaults = field_defaults(provider)
        out = {}
        for raw_key, value in changes.items():
            key = ALIASES.get(str(raw_key).strip().lower(), ALIASES.get(str(raw_key).strip(), str(raw_key).strip()))
            if key in ("provider", "__template_key"):
                continue
            if key not in defaults:
                raise GenError(f"不认识的字段“{raw_key}”。可用字段：{', '.join(defaults)}", user_error=True)
            try:
                out[key] = _coerce(key, value, defaults[key])
            except ValueError as e:
                raise GenError(f"字段 {key} 的值有误：{e}", user_error=True) from e
        if "kind" in out and out["kind"] not in ADAPTERS[provider].kinds:
            raise GenError(f"{ADAPTERS[provider].label} 只支持 {'/'.join(ADAPTERS[provider].kinds)}", user_error=True)
        if "fallback" in out and isinstance(out["fallback"], list):
            out["fallback"] = ",".join(out["fallback"])
        return out

    def add(self, name: str, preset: str = "", provider: str = "", values: dict | None = None) -> dict:
        name = (name or "").strip()
        values = dict(values or {})
        if not name:
            raise GenError("请给模型起个名字", user_error=True)
        if any(str(m.get("name", "")).lower() == name.lower() for m in self.models()):
            raise GenError(f"已经有叫 {name} 的模型了，要修改请用 set", user_error=True)
        base: dict = {}
        if preset:
            if preset not in VENDOR_PRESETS:
                raise GenError(f"没有预设 {preset}。可用：{', '.join(VENDOR_PRESETS)}", user_error=True)
            base = dict(VENDOR_PRESETS[preset])
            provider = base.pop("provider")
        provider = provider or values.pop("provider", "") or values.pop("__template_key", "")
        if provider not in ADAPTERS:
            raise GenError(f"请指定 preset 或 provider（{', '.join(ADAPTERS)}）", user_error=True)
        item = {"__template_key": provider, **{k: v for k, v in field_defaults(provider).items()}}
        item.update(self.normalize(provider, base))
        item.update(self.normalize(provider, values))
        item["name"] = name
        problems = self.lint(item)
        self._backup()
        self.models().append(item)
        kind = item.get("kind", "image")
        default_key = "default_image_model" if kind == "image" else "default_video_model"
        became_default = False
        if not self.config.get(default_key):
            self.config[default_key] = name
            became_default = True
        self._commit()
        return {"status": "added", "model": self.view(item), "default": became_default, "warnings": problems}

    def set(self, name: str, changes: dict) -> dict:
        m = self.find(name)
        if not m:
            return self.set_global(changes) if not name else {"error": f"没有模型 {name}"}
        norm = self.normalize(m.get("__template_key", ""), changes)
        if "name" in norm and norm["name"].lower() != str(m["name"]).lower() and self.find(norm["name"]):
            raise GenError(f"名称 {norm['name']} 已被占用", user_error=True)
        self._backup()
        old_name = m.get("name")
        m.update(norm)
        if "name" in norm:  # 改名：同步默认模型与其他模型的回退引用
            for key in ("default_image_model", "default_video_model"):
                if self.config.get(key) == old_name:
                    self.config[key] = norm["name"]
            for other in self.models():
                fb = [x.strip() for x in str(other.get("fallback") or "").split(",") if x.strip()]
                if old_name in fb:
                    other["fallback"] = ",".join(norm["name"] if x == old_name else x for x in fb)
        self._commit()
        shown = {k: (mask(v) if k in SECRET_KEYS else v) for k, v in norm.items()}
        return {"status": "updated", "model": m.get("name"), "changed": shown, "warnings": self.lint(m)}

    def set_global(self, changes: dict) -> dict:
        from ..tools.gen_schema import build
        schema = build()
        out = {}
        for raw_key, value in changes.items():
            key = ALIASES.get(str(raw_key).strip(), str(raw_key).strip())
            if key in ("models", "automations") or key not in schema:
                raise GenError(f"不认识的设置“{raw_key}”", user_error=True)
            default = schema[key].get("default")
            try:
                out[key] = _coerce(key, value, default)
            except ValueError as e:
                raise GenError(f"{key} 的值有误：{e}", user_error=True) from e
            if key in ("default_image_model", "default_video_model") and out[key]:
                m = self.find(out[key])
                if not m:
                    raise GenError(f"没有模型 {out[key]}", user_error=True)
                out[key] = m["name"]
        self._backup()
        for k, v in out.items():
            self.config[k] = v
        self._commit()
        return {"status": "updated", "changed": out}

    def remove(self, name: str) -> dict:
        m = self.find(name)
        if not m:
            return {"error": f"没有模型 {name}"}
        self._backup()
        self.models().remove(m)
        for key in ("default_image_model", "default_video_model"):
            if self.config.get(key) == m["name"]:
                self.config[key] = ""
        self._commit()
        return {"status": "removed", "model": m["name"]}

    def set_default(self, name: str) -> dict:
        m = self.find(name)
        if not m:
            return {"error": f"没有模型 {name}"}
        key = "default_video_model" if m.get("kind") == "video" else "default_image_model"
        return self.set_global({key: m["name"]})

    def undo(self) -> dict:
        files = sorted(os.listdir(self.backup_dir)) if os.path.isdir(self.backup_dir) else []
        if not files:
            return {"error": "没有可回滚的备份"}
        path = os.path.join(self.backup_dir, files[-1])
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        for k, v in snap.items():
            self.config[k] = v
        self._commit()
        os.remove(path)
        return {"status": "restored", "backup": files[-1]}

    def lint(self, m: dict) -> list[str]:
        """配置体检：缺 key、地址可疑、Docker 里写了 127.0.0.1 等。"""
        provider = m.get("__template_key", "")
        warns = []
        needs_key = provider not in ("comfyui", "generic") and "jimeng" not in str(m.get("base_url", ""))
        if provider == "kling":
            if not (m.get("api_key") or (m.get("access_key") and m.get("secret_key"))):
                warns.append("可灵需要 AccessKey 和 SecretKey")
        elif needs_key and not m.get("api_key"):
            warns.append("还没填 api_key")
        if provider in ("replicate", "fal", "gemini", "comfyui") and not m.get("model") and provider != "comfyui":
            warns.append("还没填模型 ID")
        url = str(m.get("base_url") or "")
        if os.path.exists("/.dockerenv") and re.search(r"//(127\.0\.0\.1|localhost)[:/]", url):
            warns.append("插件运行在 Docker 里，127.0.0.1 指向容器自己；访问宿主机服务请改成 host.docker.internal")
        if url and not url.startswith(("http://", "https://")):
            warns.append("接口地址应以 http:// 或 https:// 开头")
        for fb in str(m.get("fallback") or "").split(","):
            if fb.strip() and not self.find(fb.strip()):
                warns.append(f"回退模型 {fb.strip()} 不存在")
        return warns

    def check(self) -> dict:
        report = {"models": len(self.models()), "problems": {}}
        for m in self.models():
            if w := self.lint(m):
                report["problems"][m.get("name", "?")] = w
        for kind, key in (("image", "default_image_model"), ("video", "default_video_model")):
            if not any(m.get("kind") == kind and m.get("enabled", True) for m in self.models()):
                report.setdefault("missing", []).append(f"没有启用的{'图片' if kind == 'image' else '视频'}模型")
            elif self.config.get(key) and not self.find(self.config[key]):
                report.setdefault("missing", []).append(f"{key} 指向不存在的模型 {self.config[key]}")
        return report


# 方便 LLM：哪些字段是公共字段（其余是接口特有字段）
PUBLIC_FIELDS = sorted(COMMON_KEYS - {"__template_key", "provider"})
