"""模型注册表：把配置里的 models 列表变成可用的适配器，并负责选模型、熔断。"""

from __future__ import annotations

import hashlib
import json
import time

from ..providers import ADAPTERS, ModelSpec, build_adapter
from ..providers.base import Adapter
from .http import Http
from .types import IMAGE, VIDEO, GenError, log


class Breaker:
    """连续失败 N 次后熔断 cooldown 秒；熔断期间直接跳到回退模型，不再浪费时间等它超时。"""

    def __init__(self, threshold: int = 3, cooldown: float = 300):
        self.threshold = threshold
        self.cooldown = cooldown
        self.fails: dict[str, int] = {}
        self.open_until: dict[str, float] = {}

    def is_open(self, name: str) -> bool:
        return self.open_until.get(name, 0) > time.time()

    def left(self, name: str) -> int:
        return max(0, int(self.open_until.get(name, 0) - time.time()))

    def success(self, name: str) -> None:
        self.fails.pop(name, None)
        self.open_until.pop(name, None)

    def failure(self, name: str) -> None:
        self.fails[name] = self.fails.get(name, 0) + 1
        if self.fails[name] >= self.threshold:
            self.open_until[name] = time.time() + self.cooldown
            self.fails[name] = 0
            log.warning(f"[media_gen] 模型 {name} 连续失败 {self.threshold} 次，熔断 {int(self.cooldown)} 秒")

    def reset(self, name: str | None = None) -> None:
        if name:
            self.success(name)
        else:
            self.fails.clear()
            self.open_until.clear()


class Registry:
    def __init__(self, config: dict, http_factory, data_dir: str):
        self.config = config
        self.http_factory = http_factory  # (proxy) -> Http
        self.data_dir = data_dir
        self._cache: dict[str, tuple[str, Adapter]] = {}
        self.breaker = Breaker(int(config.get("breaker_threshold", 3) or 3),
                               float(config.get("breaker_cooldown_s", 300) or 300))
        self.errors: dict[str, str] = {}

    # ── 读取 ─────────────────────────────
    def items(self) -> list[dict]:
        return [m for m in self.config.get("models") or [] if isinstance(m, dict)]

    def specs(self, include_disabled: bool = False) -> list[ModelSpec]:
        out, self.errors = [], {}
        for item in self.items():
            try:
                spec = ModelSpec.from_config(item)
            except GenError as e:
                self.errors[str(item.get("name"))] = str(e)
                continue
            if not spec.name or spec.provider not in ADAPTERS:
                self.errors[spec.name or "?"] = f"缺少名称或接口类型无效（{spec.provider}）"
                continue
            if spec.kind not in (IMAGE, VIDEO):
                spec.kind = IMAGE
            if spec.enabled or include_disabled:
                out.append(spec)
        return out

    def get(self, name: str, include_disabled: bool = False) -> ModelSpec | None:
        name = (name or "").strip().lower()
        if not name:
            return None
        specs = self.specs(include_disabled)
        for s in specs:
            if s.name.lower() == name:
                return s
        hits = [s for s in specs if name in s.name.lower() or name == s.model.lower()]
        return hits[0] if len(hits) == 1 else None

    def adapter(self, spec: ModelSpec) -> Adapter:
        spec.options.setdefault("_data_dir", self.data_dir)
        key = hashlib.md5(json.dumps(spec.__dict__, sort_keys=True, default=str).encode()).hexdigest()
        cached = self._cache.get(spec.name)
        if cached and cached[0] == key:
            return cached[1]
        ad = build_adapter(spec, self.http_factory(spec.proxy))
        self._cache[spec.name] = (key, ad)
        return ad

    def default_for(self, kind: str) -> ModelSpec | None:
        name = self.config.get("default_image_model" if kind == IMAGE else "default_video_model", "")
        spec = self.get(name) if name else None
        if spec and spec.kind == kind:
            return spec
        return next((s for s in self.specs() if s.kind == kind), None)

    def chain(self, kind: str, name: str = "") -> list[ModelSpec]:
        """主模型 + 它配置的回退模型（+ 全局自动回退时同类型的其余模型）。"""
        first = self.get(name) if name else self.default_for(kind)
        if name and not first:
            raise GenError(f"没有叫“{name}”的可用模型。可用：{', '.join(s.name for s in self.specs() if s.kind == kind) or '无'}",
                           user_error=True)
        if not first:
            raise GenError(f"还没有配置可用的{'视频' if kind == VIDEO else '图片'}生成模型", user_error=True)
        if first.kind != kind:
            raise GenError(f"模型 {first.name} 是{'视频' if first.kind == VIDEO else '图片'}模型", user_error=True)
        out = [first]
        for fb in first.fallback:
            s = self.get(fb)
            if s and s.kind == kind and s.name not in [o.name for o in out]:
                out.append(s)
        if self.config.get("auto_fallback", False):
            out += [s for s in self.specs() if s.kind == kind and s.name not in [o.name for o in out]]
        return out

    async def close(self) -> None:
        for _, ad in self._cache.values():
            await ad.http.close()
        self._cache.clear()


def make_http_factory(config: dict):
    def factory(proxy: str = "") -> Http:
        return Http(connect_timeout=float(config.get("connect_timeout_s", 15) or 15),
                    read_timeout=float(config.get("read_timeout_s", 120) or 120),
                    retries=int(config.get("http_retries", 3) or 3),
                    backoff=float(config.get("http_backoff_s", 1.5) or 1.5),
                    proxy=proxy or config.get("proxy", ""))
    return factory
