"""ffmpeg 小工具：把多张图做成带缓慢推镜、淡入淡出和底部字幕的图文视频（抖音/B站都只能发视频时用）。"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

from .types import GenError

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]


def _font() -> str:
    return next((f for f in FONT_CANDIDATES if os.path.exists(f)), "")


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


async def slideshow(images: list[str], dest: str, *, size: str = "720x1280", per_image_s: float = 3.0,
                    captions: list[str] | None = None, fps: int = 30, timeout: float = 300) -> str:
    if not shutil.which("ffmpeg"):
        raise GenError("没有 ffmpeg，无法合成图文视频", user_error=True)
    if not images:
        raise GenError("没有图片可合成")
    w, h = (int(x) for x in size.lower().split("x"))
    d = max(1.5, per_image_s)
    frames = int(d * fps)
    font = _font()
    args = []
    for p in images:
        args += ["-loop", "1", "-t", f"{d}", "-i", p]
    args += ["-f", "lavfi", "-t", f"{d * len(images)}", "-i", "anullsrc=r=44100:cl=stereo"]
    chains = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(len(images)):
            f = (f"[{i}:v]scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
                 f"zoompan=z='min(zoom+0.0009,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={fps},"
                 f"fade=t=in:st=0:d=0.35,fade=t=out:st={d - 0.35:.2f}:d=0.35")
            cap = (captions or [None] * len(images))[i] if captions and i < len(captions) else None
            if cap and font:
                txt = os.path.join(td, f"c{i}.txt")
                with open(txt, "w", encoding="utf-8") as fh:
                    fh.write(cap[:40])
                f += (f",drawtext=fontfile='{_esc(font)}':textfile='{_esc(txt)}':fontcolor=white:fontsize={max(24, w // 20)}"
                      f":box=1:boxcolor=black@0.45:boxborderw=18:x=(w-text_w)/2:y=h-text_h-{h // 9}")
            chains.append(f + f",setsar=1[v{i}]")
        concat = "".join(f"[v{i}]" for i in range(len(images))) + f"concat=n={len(images)}:v=1:a=0[outv]"
        args += ["-filter_complex", ";".join(chains + [concat]), "-map", "[outv]", "-map", f"{len(images)}:a",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
                 "-shortest", "-movflags", "+faststart", dest]
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-loglevel", "error", *args,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise GenError("合成图文视频超时") from e
        if proc.returncode:
            raise GenError(f"合成图文视频失败: {err.decode(errors='replace')[-300:]}")
    return dest
