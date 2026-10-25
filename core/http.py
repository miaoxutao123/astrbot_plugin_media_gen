"""带重试/超时/限流退避的 HTTP 客户端，供所有适配器共用。

重试原则（避免重复扣费）：
- 幂等请求（GET、查询类 POST）：连接失败、读超时、429、5xx 都重试
- 非幂等请求（提交生成任务）：只在“请求肯定没被受理”时重试——连接没建立、429、503；
  读超时 / 500 / 502 / 504 可能已经在对方排上队了，直接报错交给上层（上层可换模型）
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import random
import time
from urllib.parse import urlparse

import aiohttp

from .types import GenError, log

RETRY_STATUS_IDEMPOTENT = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}
RETRY_STATUS_SUBMIT = {429, 503}


def _retry_after(resp: aiohttp.ClientResponse | None, attempt: int, base: float) -> float:
    if resp is not None:
        ra = resp.headers.get("Retry-After", "")
        if ra.replace(".", "", 1).isdigit():
            return min(float(ra), 120.0)
    return min(base * (2 ** attempt), 60.0) * (0.75 + random.random() / 2)


def _err_text(body) -> str:
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    return str(body or "")[:500]


class Http:
    def __init__(self, *, connect_timeout: float = 15, read_timeout: float = 120, retries: int = 3,
                 backoff: float = 1.5, proxy: str = "", verify_ssl: bool = True):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retries = retries
        self.backoff = backoff
        self.proxy = proxy or None
        self.verify_ssl = verify_ssl
        self._sessions: dict[bool, aiohttp.ClientSession] = {}

    async def session(self, url: str = "") -> aiohttp.ClientSession:
        """本机/内网地址（ComfyUI、局域网反代）直连，不走 HTTP(S)_PROXY 环境变量里的系统代理。"""
        direct = is_local(url)
        sess = self._sessions.get(direct)
        if sess is None or sess.closed:
            sess = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=32, ssl=None if self.verify_ssl else False),
                trust_env=not direct,
            )
            self._sessions[direct] = sess
        return sess

    def _proxy_for(self, url: str) -> str | None:
        return None if is_local(url) else self.proxy

    async def close(self) -> None:
        for sess in self._sessions.values():
            if not sess.closed:
                await sess.close()
        self._sessions.clear()

    def _timeout(self, timeout: float | None) -> aiohttp.ClientTimeout:
        read = timeout or self.read_timeout
        return aiohttp.ClientTimeout(total=read + self.connect_timeout, connect=self.connect_timeout, sock_read=read)

    async def request(self, method: str, url: str, *, json_body=None, data=None, headers: dict | None = None,
                      params: dict | None = None, timeout: float | None = None, idempotent: bool | None = None,
                      retries: int | None = None, expect: str = "json"):
        """返回解析后的 JSON（expect=json）/ bytes / text；HTTP 错误抛 GenError（带 retryable）。"""
        idempotent = (method.upper() == "GET") if idempotent is None else idempotent
        retries = self.retries if retries is None else retries
        retry_status = RETRY_STATUS_IDEMPOTENT if idempotent else RETRY_STATUS_SUBMIT
        sess = await self.session(url)
        attempt = 0
        while True:
            wait = 0.0
            try:
                if callable(data):  # multipart 等一次性 body 需要每次重建
                    payload = data()
                else:
                    payload = data
                async with sess.request(method, url, json=json_body, data=payload, headers=headers, params=params,
                                        timeout=self._timeout(timeout), proxy=self._proxy_for(url)) as resp:
                    raw = await resp.read()
                    if resp.status >= 400:
                        body = _decode(raw)
                        if resp.status in retry_status and attempt < retries:
                            wait = _retry_after(resp, attempt, self.backoff)
                            log.info(f"[http] {method} {_short(url)} -> {resp.status}，{wait:.1f}s 后重试 ({attempt + 1}/{retries})")
                        else:
                            raise _http_error(resp.status, body)
                    else:
                        if expect == "bytes":
                            return raw
                        if expect == "text":
                            return raw.decode("utf-8", "replace")
                        if expect == "response":
                            return resp.status, dict(resp.headers), raw
                        return _decode(raw, strict=True)
            except GenError:
                raise
            except aiohttp.ClientConnectorError as e:
                # 连接都没建立：任何请求都可以安全重试
                if attempt >= retries:
                    raise GenError(f"连接失败: {e}", retryable=True, code="connect") from e
                wait = _retry_after(None, attempt, self.backoff)
                log.info(f"[http] {method} {_short(url)} 连接失败，{wait:.1f}s 后重试: {e}")
            except (asyncio.TimeoutError, aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError,
                    aiohttp.ClientOSError) as e:
                if not idempotent or attempt >= retries:
                    kind = "超时" if isinstance(e, asyncio.TimeoutError) else "连接中断"
                    raise GenError(f"请求{kind}: {type(e).__name__} {e}", retryable=True, code="timeout"
                                   if isinstance(e, asyncio.TimeoutError) else "disconnect") from e
                wait = _retry_after(None, attempt, self.backoff)
                log.info(f"[http] {method} {_short(url)} {type(e).__name__}，{wait:.1f}s 后重试")
            except aiohttp.ClientError as e:
                raise GenError(f"请求失败: {type(e).__name__} {e}", retryable=True, code="client") from e
            attempt += 1
            await asyncio.sleep(wait)

    async def get_json(self, url: str, **kw):
        return await self.request("GET", url, **kw)

    async def post_json(self, url: str, body: dict, *, submit: bool = True, **kw):
        """submit=True 表示提交生成任务（非幂等）。"""
        return await self.request("POST", url, json_body=body, idempotent=not submit, **kw)

    async def download(self, url: str, dest: str, *, headers: dict | None = None, max_bytes: int = 1 << 30,
                       timeout: float = 600, retries: int = 3) -> str:
        """流式下载到 dest（先写 .part 再改名），带重试与大小上限。data: URL 直接解码。"""
        if url.startswith("data:"):
            return save_b64(url, dest)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        sess = await self.session(url)
        tmp = dest + ".part"
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with sess.get(url, headers=headers, proxy=self._proxy_for(url), allow_redirects=True,
                                    timeout=aiohttp.ClientTimeout(total=timeout, connect=self.connect_timeout,
                                                                  sock_read=120)) as resp:
                    if resp.status >= 400:
                        body = _decode(await resp.read())
                        err = _http_error(resp.status, body)
                        if resp.status in RETRY_STATUS_IDEMPOTENT and attempt < retries:
                            last = err
                            await asyncio.sleep(_retry_after(resp, attempt, self.backoff))
                            continue
                        raise err
                    size = 0
                    with open(tmp, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1 << 16):
                            size += len(chunk)
                            if size > max_bytes:
                                raise GenError(f"产物超过大小上限 {max_bytes >> 20}MB", code="too_large")
                            f.write(chunk)
                if size == 0:
                    raise GenError("下载到的文件为空", retryable=True, code="empty")
                os.replace(tmp, dest)
                return dest
            except GenError as e:
                last = e
                if not e.retryable or attempt >= retries:
                    break
                await asyncio.sleep(_retry_after(None, attempt, self.backoff))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = GenError(f"下载失败: {type(e).__name__} {e}", retryable=True, code="download")
                if attempt >= retries:
                    break
                await asyncio.sleep(_retry_after(None, attempt, self.backoff))
        if os.path.exists(tmp):
            os.remove(tmp)
        raise last or GenError("下载失败")


def is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith((".local", ".lan", ".internal")) or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in ipaddress.ip_network("100.64.0.0/10")


def save_b64(data: str, dest: str) -> str:
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data)
    if not raw:
        raise GenError("base64 产物为空")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "wb") as f:
        f.write(raw)
    return dest


def _decode(raw: bytes, strict: bool = False):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        if strict:
            raise GenError(f"接口返回的不是 JSON: {raw[:200]!r}", retryable=True, code="bad_json") from None
        return raw.decode("utf-8", "replace")[:500]


def _http_error(status: int, body) -> GenError:
    text = _err_text(body)
    low = text.lower()
    # 余额不足 / 鉴权 / 内容审核 / 参数错误：换个时间重试也没用
    user = status in (400, 401, 402, 403, 404, 413, 422) or any(
        k in low for k in ("insufficient", "quota", "balance", "余额", "sensitive", "moderation", "违规",
                           "safety", "content_policy", "invalidparameter", "invalid_parameter"))
    retryable = status in RETRY_STATUS_IDEMPOTENT and not user
    code = ""
    if isinstance(body, dict):
        err = body.get("error")
        code = str((err or {}).get("code") or (err or {}).get("type") or "") if isinstance(err, dict) else ""
        code = code or str(body.get("code") or body.get("status_code") or "")
    return GenError(f"HTTP {status}: {text}", retryable=retryable, user_error=user and status not in (401, 403, 404),
                    status=status, code=code)


def _short(url: str) -> str:
    return url.split("?")[0][-80:]


class Deadline:
    """总时限：剩余时间不足时直接抛超时。"""

    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds
        self.seconds = seconds

    def left(self) -> float:
        return self.end - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.left() <= 0
