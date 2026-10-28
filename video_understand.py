"""让模型看懂视频内容：下载 → 压缩 → Gemini 描述。"""

import os
import asyncio
import base64
import json
from pathlib import Path

from astrbot.api import logger

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE = "https://api.yoshub.com/v1"
GEMINI_MODEL = "gemini-3.8-flash"

WORK = Path("/tmp/vidunder")
WORK.mkdir(parents=True, exist_ok=True)

# 准确优先：短一点、帧率高一点
MAX_SECONDS = 30
MAX_MB = 18.0

DEFAULT_Q = (
    "你在看一条 B 站视频，我会按时间顺序给你几帧画面。请准确转述，重点是别漏掉梗。\n"
    "1) 先说画面里到底有什么、在做什么（主体、动作、场景、出现的文字或字幕，能看清就照写）；\n"
    "2) 判断这条是不是在玩梗：如果是网络迷因、名场面、知名二创、鬼畜、热梗，直接点出这个梗叫什么、梗点在哪、为什么好笑；\n"
    "3) 如果画面里有文字、弹幕、水印、台词，把它们写出来（这些常常就是梗的钥匙）；\n"
    "4) 看不清、认不出就说看不清，绝对不要编，编出来的假描述比说不知道更糟；\n"
    "5) 不要加标题、不要分点，像跟人聊天一样说两三段话。"
)


async def _run(cmd, timeout=120):
    p = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return -1, b"", b"timeout"
    return p.returncode, out, err


async def _compress(src: Path, dst: Path) -> bool:
    """画质优先：540p/4fps 起，压不下去再逐级降。"""
    tiers = ((540, "900k"), (480, "700k"), (360, "500k"), (240, "300k"))
    for height, kbps in tiers:
        rc, _, err = await _run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-t", str(MAX_SECONDS),
            "-vf", "scale=-2:" + str(height),
            "-r", "4",
            "-b:v", kbps, "-b:a", "64k",
            "-movflags", "+faststart",
            str(dst),
        ], timeout=240)
        if rc == 0 and dst.exists():
            mb = dst.stat().st_size / 1024 / 1024
            logger.info("[vidunder] 压缩 " + str(height) + "p/4fps -> " + format(mb, ".1f") + " MB")
            if mb <= MAX_MB:
                return True
        else:
            logger.warning("[vidunder] ffmpeg 失败: " + err.decode("utf-8", "ignore")[-150:])
    return False


async def _duration(src: Path) -> float:
    rc, out, err = await _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(src),
    ], timeout=60)
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def _frames(src: Path, n: int = 10, span_max: float = 600.0) -> list:
    """覆盖全片均匀抽 n 帧（最多前 10 分钟），返回本地文件列表。"""
    dur = await _duration(src)
    span = min(span_max, dur) if dur > 0 else span_max
    fps = max(0.02, float(n) / max(1.0, span))
    for old in WORK.glob("f*.jpg"):
        try:
            old.unlink()
        except Exception:
            pass
    rc, _, err = await _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-t", str(int(span)), "-vf", "fps=" + str(round(fps, 3)) + ",scale=-2:400",
        str(WORK / "f%02d.jpg"),
    ], timeout=240)
    got = sorted(WORK.glob("f*.jpg"))
    if rc != 0 or not got:
        logger.warning("[vidunder] 抽帧失败: " + err.decode("utf-8", "ignore")[-150:])
    return got[:n]


async def _ask_images(frames, question: str, model: str) -> str:
    content = [{"type": "text", "text": question}]
    for f in frames:
        b64 = base64.b64encode(f.read_bytes()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + b64}})
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "max_tokens": 800, "stream": True}
    pf = WORK / "frames_body.json"
    pf.write_text(json.dumps(body), encoding="utf-8")
    rc, out, err = await _run([
        "curl", "-s", "-N", "-m", "180",
        "-H", "Authorization: Bearer " + GEMINI_KEY,
        "-H", "Content-Type: application/json",
        "-d", "@" + str(pf),
        GEMINI_BASE + "/chat/completions",
    ], timeout=200)
    raw = out.decode("utf-8", "ignore")
    parts = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            j = json.loads(payload)
        except Exception:
            continue
        for ch in (j.get("choices") or []):
            d = ch.get("delta") or {}
            if d.get("content"):
                parts.append(str(d["content"]))
    return "".join(parts).strip()


async def understand_frames(src_path: str, question: str = "", context: str = "") -> str:
    """抽帧 + 多图输入：中转站不吃 input_video，这条路已验证可用。"""
    try:
        src = Path(src_path)
        if not src.exists():
            return ""
        frames = await _frames(src, 10)
        if not frames:
            return ""
        logger.info("[vidunder] 抽到 " + str(len(frames)) + " 帧，走多图通道")
        q = question or DEFAULT_Q
        if context:
            q = ("【这条视频的元信息，可能帮你认出梗】" + chr(10) + context + chr(10) + chr(10) + q)
        q = q + chr(10) + "（下面是同一个视频按时间顺序抽的画面）"
        ans = await _ask_images(frames, q, GEMINI_MODEL)
        for f in frames:
            try:
                f.unlink()
            except Exception:
                pass
        if ans:
            logger.info("[vidunder] 多图理解完成，长度 " + str(len(ans)))
        else:
            logger.warning("[vidunder] 多图通道无返回")
        return ans
    except Exception as exc:
        logger.warning("[vidunder] 多图异常: " + repr(exc))
        return ""
async def understand(src_path: str, question: str = "", context: str = "") -> str:
    """返回模型对视频内容的描述，失败返回空串。"""
    try:
        src = Path(src_path)
        if not src.exists():
            return ""

        quick = await understand_frames(str(src), question, context)
        if quick:
            return quick

        dst = WORK / (src.stem + "_hi.mp4")
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            if not await _compress(src, dst):
                logger.warning("[vidunder] 压缩失败")
                return ""

        mb = dst.stat().st_size / 1024 / 1024
        if mb > MAX_MB:
            logger.warning("[vidunder] 压缩后仍 " + format(mb, ".1f") + " MB，跳过")
            return ""

        b64 = base64.b64encode(dst.read_bytes()).decode()
        body = {
            "model": GEMINI_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "input_video", "input_video": {"data": b64, "format": "mp4"}},
                {"type": "text", "text": question or DEFAULT_Q},
            ]}],
            "max_tokens": 700,
            "stream": True,
        }
        pf = WORK / "body.json"
        pf.write_text(json.dumps(body), encoding="utf-8")

        rc, out, err = await _run([
            "curl", "-s", "-N", "-m", "300",
            "-H", "Authorization: Bearer " + GEMINI_KEY,
            "-H", "Content-Type: application/json",
            "-d", "@" + str(pf),
            GEMINI_BASE + "/chat/completions",
        ], timeout=320)

        raw = out.decode("utf-8", "ignore")
        parts = []
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                continue
            try:
                j = json.loads(payload)
            except Exception:
                continue
            for ch in (j.get("choices") or []):
                d = ch.get("delta") or {}
                if d.get("content"):
                    parts.append(str(d["content"]))
        ans = "".join(parts).strip()
        if not ans:
            logger.warning("[vidunder] 模型无返回, raw 前 200: " + raw[:200])
        else:
            logger.info("[vidunder] 理解完成，长度 " + str(len(ans)))
        return ans
    except Exception as exc:
        logger.warning("[vidunder] 异常: " + repr(exc))
        return ""
