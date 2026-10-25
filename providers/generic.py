"""通用模板适配器：不写代码，靠配置对接任意“提交 + 轮询”或同步 HTTP 接口。

请求体模板是 JSON，值里可以写占位符：
  {{prompt}} {{negative_prompt}} {{model}} {{size}} {{width}} {{height}} {{ratio}} {{duration}} {{seed}} {{n}}
  {{image}}（首张参考图：有原链接用链接，否则 data URL）{{image_b64}}（纯 base64）{{images}}（列表）{{last_frame}}
整串就是一个占位符时按原类型替换（数字仍是数字），值为空时该字段会被删掉。

内置预设（preset）覆盖了几家“非 OpenAI 标准”的常见接口，填了 preset 后其余字段可留空。
"""

from __future__ import annotations

import json
import re

from ..core.types import IMAGE, VIDEO, Asset, GenError, GenRequest, PollResult, dig
from .base import Adapter, collect_media

PRESETS: dict[str, dict] = {
    # 硅基流动视频：POST /video/submit → requestId；POST /video/status → status ∈ Succeed/InQueue/InProgress/Failed
    "siliconflow_video": {
        "base_url": "https://api.siliconflow.cn/v1", "kind": VIDEO,
        "submit_path": "/video/submit",
        "body_template": '{"model":"{{model}}","prompt":"{{prompt}}","negative_prompt":"{{negative_prompt}}",'
                         '"image_size":"{{size}}","image":"{{image}}","seed":"{{seed}}"}',
        "id_path": "requestId", "poll_path": "/video/status", "poll_method": "POST",
        "poll_body_template": '{"requestId":"{{id}}"}', "status_path": "status",
        "success_values": "Succeed", "failure_values": "Failed", "result_path": "results.videos.0.url",
        "error_path": "reason",
    },
    # new-api / one-api 统一视频格式：POST /video/generations → task_id；GET /video/generations/{id}
    "newapi_video": {
        "kind": VIDEO, "submit_path": "/video/generations",
        "body_template": '{"model":"{{model}}","prompt":"{{prompt}}","image":"{{image}}","duration":"{{duration}}",'
                         '"width":"{{width}}","height":"{{height}}","seed":"{{seed}}"}',
        "id_path": "task_id|data.task_id|id", "poll_path": "/video/generations/{id}",
        "status_path": "status|data.status|data.task_status",
        "success_values": "succeeded,success,completed,SUCCESS,succeed",
        "failure_values": "failed,failure,FAILURE,fail,cancelled,error",
        "result_path": "url|data.url|data.result_url|metadata.url", "error_path": "error|fail_reason|data.fail_reason",
    },
    # 即梦反代（iptag/jimeng-api）：同步阻塞，直接返回 data[].url
    "jimeng_video": {
        "kind": VIDEO, "submit_path": "/videos/generations",
        "body_template": '{"model":"{{model}}","prompt":"{{prompt}}","ratio":"{{ratio}}","duration":"{{duration}}",'
                         '"file_paths":"{{images}}"}',
        "id_path": "", "result_path": "data.0.url", "submit_timeout_s": 900,
    },
}


def _render(tpl, ctx: dict, drop_empty: bool = True):
    """drop_empty=False 时保留空值字段（ComfyUI 节点输入缺了会校验失败）。"""
    if isinstance(tpl, dict):
        out = {}
        for k, v in tpl.items():
            r = _render(v, ctx, drop_empty)
            if not drop_empty or (r is not None and r != "" and r != []):
                out[k] = r
        return out
    if isinstance(tpl, list):
        items = [_render(v, ctx, drop_empty) for v in tpl]
        return items if not drop_empty else [r for r in items if r not in (None, "")]
    if isinstance(tpl, str):
        m = re.fullmatch(r"\{\{(\w+)\}\}", tpl.strip())
        if m:
            v = ctx.get(m.group(1))
            return ("" if v is None else v) if not drop_empty else v
        return re.sub(r"\{\{(\w+)\}\}", lambda mm: "" if ctx.get(mm.group(1)) is None else str(ctx[mm.group(1)]), tpl)
    return tpl


def _first(obj, paths: str):
    for p in str(paths or "").split("|"):
        if p.strip() and (v := dig(obj, p.strip())) not in (None, ""):
            return v
    return None


def _values(s: str) -> set[str]:
    return {x.strip().lower() for x in str(s or "").split(",") if x.strip()}


class Generic(Adapter):
    provider = "generic"
    label = "通用 HTTP 模板"
    kinds = (IMAGE, VIDEO)
    poll_interval = 6
    doc = "用 JSON 模板 + 字段路径对接任意接口；preset 可选 siliconflow_video / newapi_video / jimeng_video"
    fields = {
        "preset": ("内置预设：siliconflow_video / newapi_video / jimeng_video（留空=全部手填）", ""),
        "submit_path": ("提交路径", "/generate"),
        "submit_method": ("提交方法", "POST"),
        "body_template": ("请求体 JSON 模板（支持 {{prompt}} 等占位符）", '{"model":"{{model}}","prompt":"{{prompt}}"}'),
        "id_path": ("提交响应里任务 ID 的路径（a.b.0.c，| 分隔多个候选）；留空=同步接口", ""),
        "poll_path": ("查询路径，{id} 会被替换", "/tasks/{id}"),
        "poll_method": ("查询方法 GET / POST", "GET"),
        "poll_body_template": ("POST 查询时的请求体模板（可用 {{id}}）", ""),
        "status_path": ("状态字段路径", "status"),
        "success_values": ("成功状态值（逗号分隔，不区分大小写）", "succeeded,success,completed,done"),
        "failure_values": ("失败状态值", "failed,failure,error,cancelled,canceled"),
        "result_path": ("产物 URL/base64 的路径（留空=自动在响应里找）", ""),
        "error_path": ("失败原因字段路径", "error|message"),
        "auth_header": ("鉴权头名", "Authorization"),
        "auth_prefix": ("鉴权值前缀", "Bearer "),
        "headers": ("额外请求头 JSON", ""),
    }

    def __init__(self, spec, http):
        super().__init__(spec, http)
        preset = PRESETS.get(spec.opt("preset", ""), {})
        for k, v in preset.items():
            if k == "base_url" and not spec.base_url:
                spec.base_url = v
            elif k == "submit_timeout_s" and not spec.submit_timeout_s:
                spec.submit_timeout_s = float(v)
            elif k != "kind" and spec.options.get(k) in (None, ""):
                spec.options[k] = v

    def _o(self, key: str):
        return self.spec.opt(key, self.fields.get(key, ("", ""))[1])

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.spec.api_key:
            h[self._o("auth_header")] = f"{self._o('auth_prefix')}{self.spec.api_key}"
        extra = self._o("headers")
        if extra:
            h.update(json.loads(extra) if isinstance(extra, str) else extra)
        return h

    def _ctx(self, req: GenRequest) -> dict:
        w, h = req.width_height() if (req.size or req.aspect_ratio) else (None, None)
        first = req.images[0] if req.images else None
        return {
            "prompt": req.prompt, "negative_prompt": req.negative_prompt or None, "model": self.spec.model,
            "size": f"{w}x{h}" if w else None, "width": w, "height": h,
            "ratio": req.ratio() if (req.size or req.aspect_ratio) else None,
            "duration": int(round(req.duration)) if req.duration and req.kind == VIDEO else None, "seed": req.seed, "n": req.n,
            "image": first.url_or_data() if first else None, "image_b64": first.b64() if first else None,
            "images": [r.url_or_data() for r in req.images] or None,
            "last_frame": req.last_frame.url_or_data() if req.last_frame else None,
        }

    async def submit(self, req: GenRequest) -> dict:
        self.want(req)
        try:
            tpl = json.loads(self._o("body_template"))
        except ValueError as e:
            raise GenError(f"body_template 不是合法 JSON: {e}", user_error=True) from e
        body = self.body(_render(tpl, self._ctx(req)), req)
        resp = await self.http.request(self._o("submit_method").upper(), self.url(self._o("submit_path")),
                                       json_body=body, headers=self._headers(), idempotent=False,
                                       timeout=self.spec.submit_timeout_s or None)
        if not self._o("id_path"):
            return self.sync_handle(self._assets(resp, req.kind))
        tid = _first(resp, self._o("id_path"))
        if not tid:
            raise GenError(f"响应里找不到任务 ID（{self._o('id_path')}）: {str(resp)[:200]}", code="no_id")
        return {"id": str(tid), "kind": req.kind}

    def _assets(self, resp, kind: str) -> list[Asset]:
        rp = self._o("result_path")
        val = _first(resp, rp) if rp else None
        if isinstance(val, str):
            return [Asset(kind, url=val) if val.startswith("http") else Asset(kind, data=val)]
        if isinstance(val, list):
            return [Asset(kind, url=v) if v.startswith("http") else Asset(kind, data=v) for v in val if isinstance(v, str)]
        return collect_media(resp, kind)

    async def poll(self, handle: dict) -> PollResult:
        if handle.get("sync"):
            return await super().poll(handle)
        path = self._o("poll_path").replace("{id}", handle["id"])
        if self._o("poll_method").upper() == "POST":
            body = _render(json.loads(self._o("poll_body_template") or "{}"), {"id": handle["id"]})
            resp = await self.http.request("POST", self.url(path), json_body=body, headers=self._headers(),
                                           idempotent=True, timeout=30)
        else:
            resp = await self.http.get_json(self.url(path), headers=self._headers(), timeout=30)
        status = str(_first(resp, self._o("status_path")) or "").lower()
        if status in _values(self._o("success_values")):
            assets = self._assets(resp, handle.get("kind", VIDEO))
            if not assets:
                return PollResult("failed", error=f"状态成功但找不到产物: {str(resp)[:200]}")
            return PollResult("succeeded", 100, assets)
        if status in _values(self._o("failure_values")):
            return PollResult("failed", error=str(_first(resp, self._o("error_path")) or status)[:300])
        return PollResult("running" if status in ("running", "processing", "in_progress", "inprogress") else "pending")

    async def probe(self) -> str:
        if not self._o("id_path"):
            return "同步接口，无法免费探测，请直接 test"
        try:
            await self.poll({"id": "probe-nonexistent", "kind": IMAGE})
        except GenError as e:
            if e.status in (401, 403):
                raise
            return f"接口可达（查询不存在的任务返回 {e.status or e.code}）"
        return "接口可达"
