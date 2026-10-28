"""视频拉取 & B站动态总结：B站走 yt-dlp，抖音需要 cookie。"""

import asyncio
import json
import shutil
import os
import re
import time
import urllib.parse
from dataclasses import field
from pathlib import Path
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.components import Video
from astrbot.core.message.message_event_result import MessageChain

try:
    from .video_understand import understand
except ImportError:
    understand = None

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TMP = Path("/tmp/videofetch")
TMP.mkdir(parents=True, exist_ok=True)

# 由插件实例在 initialize 时注入
_PLUGIN = None


def _cfg(key: str, default=None):
    try:
        return (_PLUGIN.conf or {}).get(key, default) if _PLUGIN else default
    except Exception:
        return default


async def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", "执行超时"
    return proc.returncode, out.decode("utf-8", errors="ignore"), err.decode("utf-8", errors="ignore")


def _detect(url: str) -> str:
    u = url.lower()
    if "bilibili.com" in u or "b23.tv" in u:
        return "bili"
    if "douyin.com" in u or "iesdouyin.com" in u:
        return "douyin"
    return "other"


@dataclass
class FetchVideoTool(FunctionTool[AstrAgentContext]):
    """拉取 B 站 / 抖音视频的元信息，可选择下载。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}
    context: Any = None

    name: str = "fetch_video"
    description: str = (
        "获取 B站/抖音视频的信息（标题、作者、时长、播放量）。"
        "默认只要信息，不要传 download —— 用户发链接往往只是想让你看看这是什么。"
        "只有用户明确说「下载」「存下来」「发给我文件」时才传 download=true。"
        "B站走 yt-dlp，抖音走 mc-ai 的浏览器抓取服务。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "视频链接（B站/抖音，支持 b23.tv 短链）"},
                "download": {"type": "boolean", "description": "只有用户明确要求下载/保存/发文件时才设 true。默认 false 就够", "default": False},
            },
            "required": ["url"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], url: str, download: bool = False) -> ToolExecResult:
        try:
            u = (url or "").strip()
            if not u.startswith("http"):
                return "请给一个 http/https 链接。"
            site = _detect(u)

            # 抖音走 mc-ai 上的浏览器抓取服务（yt-dlp 的 Douyin extractor
            # 过不了 a_bogus 签名，给它 cookie 也没用）
            if site == "douyin":
                api_base = str(_cfg("douyin_api", "http://127.0.0.1:8899") or "").rstrip("/")
                if not api_base:
                    return "抖音需要 douyin_api 配置（默认 http://127.0.0.1:8899）"
                q = urllib.parse.quote(u, safe="")
                rc, out, err = await _run(
                    ["curl", "-sL", "-m", "120", api_base + "/douyin?url=" + q],
                    timeout=130,
                )
                if rc != 0 or not out.strip():
                    return "抖音抓取服务无响应: " + (err[:200] or out[:200])
                try:
                    data = json.loads(out)
                except Exception:
                    return "抖音服务返回异常: " + out[:300]
                if not data.get("ok"):
                    return "抖音抓取失败: " + str(data.get("error", "未知错误"))
                size_mb = (data.get("size") or 0) / 1024 / 1024
                parts = [
                    "《" + str(data.get("title", "")) + "》",
                    "作者: " + str(data.get("author", "")),
                    "大小: " + format(size_mb, ".1f") + " MB",
                    "id: " + str(data.get("id", "")),
                ]
                if download:
                    local = TMP / ("douyin_" + str(data.get("id", "video")) + ".mp4")
                    rc2, _, err2 = await _run(
                        ["curl", "-sL", "-m", "180", "-o", str(local),
                         api_base + "/douyin?mode=file&url=" + q],
                        timeout=200,
                    )
                    if rc2 != 0 or not local.exists() or local.stat().st_size < 10240:
                        parts.append("视频下载失败: " + (err2[:150] or "无内容"))
                        return chr(10).join(parts)

                    # NapCat 在另一个容器，看不到本容器路径 —— 复制到共享卷再发
                    send_path = local
                    shared_dir = Path("/shared_audio")
                    try:
                        if shared_dir.is_dir():
                            sf = shared_dir / local.name
                            shutil.copy2(local, sf)
                            os.chmod(sf, 0o644)
                            send_path = sf
                    except Exception as exc:
                        logger.warning(f"[videofetch] 复制到共享目录失败: {exc}")

                    try:
                        ev = context.context.event
                        await self.context.send_message(
                            ev.unified_msg_origin,
                            MessageChain([Video(file=str(send_path))]),
                        )
                        parts.append("视频已发送（" + format(local.stat().st_size / 1024 / 1024, ".1f") + " MB）")
                    except Exception as exc:
                        logger.error(f"[videofetch] 发视频失败: {exc}")
                        parts.append("视频发不出去: " + str(exc)[:150])

                return chr(10).join(parts)

            base = ["yt-dlp", "--no-warnings", "--no-playlist"]
            cookie = str(_cfg("douyin_cookie", "") or "").strip()
            if site == "douyin" and cookie:
                cpath = TMP / "douyin_cookie.txt"
                cpath.write_text(cookie, encoding="utf-8")
                base += ["--cookies", str(cpath)]

            if not download:
                base += ["--skip-download", "--print",
                         "%(title)s\n%(uploader)s\n%(duration)s\n%(view_count)s\n%(webpage_url)s"]
                rc, out, err = await _run(base + [u], timeout=60)
                if rc != 0:
                    if site == "douyin" and ("cookie" in err.lower() or "403" in err):
                        return ("抖音需要登录态 cookie。请在插件配置里填 douyin_cookie"
                                "（浏览器 F12 → Application → Cookies 复制整串），或改用 B 站链接。")
                    return f"取信息失败: {err.strip()[:300] or out.strip()[:300]}"
                lines = [x for x in out.strip().splitlines() if x.strip()]
                if len(lines) < 3:
                    return f"返回内容异常: {out[:300]}"
                title = lines[0]
                uploader = lines[1]
                dur = lines[2]
                try:
                    dur_s = f"{float(dur):.0f} 秒"
                except Exception:
                    dur_s = dur
                views = lines[3] if len(lines) > 3 else "?"
                link = lines[4] if len(lines) > 4 else u
                return (f"《{title}》\n作者: {uploader}\n时长: {dur_s}\n播放: {views}\n链接: {link}")

            outtmpl = str(TMP / "%(id)s.%(ext)s")
            base += ["-o", outtmpl, "--print", "after_move:filepath"]
            rc, out, err = await _run(base + [u], timeout=300)
            if rc != 0:
                if site == "douyin" and ("cookie" in err.lower() or "403" in err):
                    return "抖音需要 cookie（配置 douyin_cookie）或用 B 站链接。"
                return f"下载失败: {err.strip()[:300]}"
            paths = [p.strip() for p in out.strip().splitlines() if p.strip().startswith("/")]
            if not paths:
                return f"下载完成但没拿到路径: {out[:200]}"
            p = Path(paths[-1])
            size_mb = p.stat().st_size / 1024 / 1024 if p.exists() else 0
            # 让模型看完视频并描述内容
            desc = ""
            if understand is not None:
                try:
                    desc = await understand(str(p))
                except Exception as exc:
                    logger.warning("[videofetch] 理解失败: " + repr(exc))
            if desc:
                return "视频内容：" + chr(10) + desc + chr(10) + chr(10) + "（" + str(round(size_mb, 1)) + " MB）"
            return ("已下载: " + str(p) + "，但没能解析内容。")
        except Exception as exc:
            logger.error(f"[videofetch] fetch_video 失败: {exc}")
            return f"出错: {exc}"


@dataclass
class BiliDynamicTool(FunctionTool[AstrAgentContext]):
    """拉取某个 B 站 UP 主的最近动态。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}
    context: Any = None

    name: str = "bili_dynamic"
    description: str = (
        "拉取某个 B 站 UP 主的最近动态（图文/视频），返回文字内容摘要。"
        "uid 是 UP 主的数字 ID（空间地址 space.bilibili.com/<uid>）。"
        "想看某个 UP 最近发了什么、做总结时调用。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "B站 UP 主 UID（数字）"},
                "count": {"type": "integer", "description": "拉几条，1-10，默认 5", "default": 5},
            },
            "required": ["uid"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], uid: str, count: int = 5) -> ToolExecResult:
        try:
            u = str(uid).strip()
            if not u.isdigit():
                m = re.search(r"space\.bilibili\.com/(\d+)", u)
                if m:
                    u = m.group(1)
                else:
                    return "uid 需要是纯数字，或者给 space.bilibili.com/<uid> 形式的链接。"

            n = max(1, min(int(count or 5), 10))
            api = (
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
                f"?host_mid={u}&features=itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote"
            )
            rc, out, err = await _run(
                ["curl", "-sL", "-m", "15", "-A", UA,
                 "-H", "Referer: https://space.bilibili.com/",
                 api],
                timeout=25,
            )
            if rc != 0 or not out:
                return f"请求 B 站 API 失败: {err[:200]}"
            data = json.loads(out)
            if data.get("code") != 0:
                return f"B站返回错误 code={data.get('code')} msg={data.get('message')}"

            items = ((data.get("data") or {}).get("items") or [])[:n]
            if not items:
                return f"UID {u} 最近没有公开动态。"

            lines = []
            for i, it in enumerate(items, 1):
                mod = it.get("modules") or {}
                author = ((mod.get("module_author") or {}).get("name")) or u
                pub = (mod.get("module_author") or {}).get("pub_time") or ""
                dyn = mod.get("module_dynamic") or {}
                desc = (dyn.get("desc") or {}).get("text") or ""
                major = dyn.get("major") or {}
                title = ""
                if major.get("archive"):
                    title = "【视频】" + str(major["archive"].get("title") or "")
                elif major.get("opus"):
                    title = "【图文】" + str((major["opus"].get("title") or ""))
                elif major.get("article"):
                    title = "【专栏】" + str(major["article"].get("title") or "")
                body = (desc or title).strip().replace("\n", " ")
                if len(body) > 220:
                    body = body[:220] + "…"
                lines.append(f"{i}. [{pub}] {body or '（无正文）'}")
            return f"UP {u} 最近 {len(lines)} 条动态：\n" + "\n".join(lines)
        except json.JSONDecodeError:
            return "B站返回的不是 JSON，可能被风控了，稍后再试。"
        except Exception as exc:
            logger.error(f"[videofetch] bili_dynamic 失败: {exc}")
            return f"出错: {exc}"


@register(
    "astrbot_plugin_videofetch",
    "小肥鱼",
    "拉取 B站/抖音视频信息与下载，以及 B站 UP 主动态总结",
    "1.0.0",
)
class VideoFetchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.conf = config or {}
        global _PLUGIN
        _PLUGIN = self

    async def initialize(self) -> None:
        self.context.add_llm_tools(
            FetchVideoTool(context=self.context),
            BiliDynamicTool(context=self.context),
        )
        logger.info("[videofetch] 已注册工具 fetch_video / bili_dynamic")
