"""astrbot_plugin_luoyue_music —— 基于落月 API 的 AstrBot 点歌插件。

功能：
* ``/点歌 歌名`` 搜索并列出候选，``/点歌 序号`` 或 ``/选歌 序号`` 选择播放；
* ``/直接点歌 歌名`` 命中第一条结果后直接播放；
* 支持网易云音乐、QQ 音乐两个音源，``/点歌源`` 可在会话内切换；
* 发送方式支持音乐卡片（OneBot / NapCat）、播放链接、语音、文件；
* 音质、搜索结果数量、候选有效期等均可在 WebUI 中配置。

落月 API 文档：https://doc.vkeys.cn
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_luoyue_music"
DEFAULT_API_BASE = "https://api.vkeys.cn"

SOURCE_LABELS = {
    "netease": "网易云音乐",
    "tencent": "QQ 音乐",
}

# 允许用户在聊天中使用的音源别名
SOURCE_ALIASES = {
    "netease": "netease",
    "163": "netease",
    "wyy": "netease",
    "网易": "netease",
    "网易云": "netease",
    "网易云音乐": "netease",
    "tencent": "tencent",
    "qq": "tencent",
    "qq音乐": "tencent",
    "tencentmusic": "tencent",
    "qqmusic": "tencent",
}

# 允许用户在聊天中指定发送方式的别名
MODE_ALIASES = {
    "card": "card",
    "卡片": "card",
    "音乐卡片": "card",
    "link": "link",
    "链接": "link",
    "播放链接": "link",
    "record": "record",
    "voice": "record",
    "语音": "record",
    "语音条": "record",
    "file": "file",
    "文件": "file",
    "音频文件": "file",
}

MODE_LABELS = {
    "card": "音乐卡片",
    "link": "播放链接",
    "record": "语音",
    "file": "文件",
}

HELP_TEXT = """🎵 落月点歌 使用说明

/点歌 歌名 —— 搜索歌曲并列出候选
/点歌 序号 —— 播放候选中对应序号的歌曲
/选歌 序号 —— 同上
/直接点歌 歌名 —— 直接播放搜索结果第一首
/语音点歌 歌名 —— 直接以语音条发送
/文件点歌 歌名 —— 直接以音频文件发送
/点歌源 —— 查看当前音源
/点歌源 网易云|QQ音乐 —— 切换当前会话音源
/点歌帮助 —— 显示本帮助

在指令末尾加上发送方式，可以临时覆盖默认设置：
/点歌 晴天 语音、/选歌 1 文件、/点歌 晴天 卡片、/点歌 晴天 链接

示例：/点歌 晴天
音质、搜索结果数量、默认发送方式可在 AstrBot WebUI 的插件配置中调整。"""


class LuoyueError(Exception):
    """落月 API 返回的业务错误。"""


def _config_value(config: AstrBotConfig | dict | None, key: str, default):
    """安全读取配置，兼容配置缺失或值类型异常的情况。"""
    if not config:
        return default
    try:
        value = config.get(key, default)
    except Exception:  # noqa: BLE001 - 防御异常配置对象
        return default
    return default if value is None else value


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_connection_error(exc: Exception) -> bool:
    """判断是否为网络层错误（此时继续重试其它音质/关键词没有意义）。"""
    text = str(exc)
    return "无法连接落月 API" in text or "无法解析的数据" in text


def _stop_ai_reply(handler):
    """命令处理结束后阻止事件继续进入 AstrBot 默认的 LLM 链路。

    指令插件通常只希望返回自己的结果，不希望再让 AI 回复一遍。
    """

    @functools.wraps(handler)
    async def wrapper(self, event):
        try:
            async for result in handler(self, event):
                yield result
        finally:
            event.stop_event()

    return wrapper


@dataclass
class Song:
    """一首歌曲的归一化信息。"""

    source: str
    song_id: str
    title: str
    singer: str = ""
    mid: str = ""
    album: str = ""
    subtitle: str = ""
    cover: str = ""
    pay: str = ""
    quality: str = ""
    song_type: int = 0
    link: str = ""
    duration: str = ""
    size: str = ""
    # 搜索时的关键词与序号，部分接口（网易云 choose 模式）取链时需要使用
    keyword: str = ""
    index: int = 0

    @classmethod
    def from_api(cls, source: str, data: dict) -> Song:
        return cls(
            source=source,
            song_id=str(data.get("id") or data.get("mid") or "").strip(),
            mid=str(data.get("mid") or "").strip(),
            title=str(data.get("song") or data.get("title") or "").strip(),
            singer=str(data.get("singer") or "").strip(),
            album=str(data.get("album") or "").strip(),
            subtitle=str(data.get("subtitle") or "").strip(),
            cover=str(data.get("cover") or "").strip(),
            pay=str(data.get("pay") or "").strip(),
            quality=str(data.get("quality") or "").strip(),
            song_type=_as_int(data.get("type"), 0),
            link=str(data.get("link") or "").strip(),
            duration=str(data.get("interval") or "").strip(),
            size=str(data.get("size") or "").strip(),
        )


@dataclass
class SearchSession:
    """一次搜索的候选缓存。"""

    source: str
    keyword: str
    songs: list[Song]
    created_at: float
    mode: str | None = None


class LuoyueClient:
    """落月 API 的异步客户端。"""

    def __init__(self, config: AstrBotConfig | dict | None):
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        base = str(_config_value(self.config, "api_base", DEFAULT_API_BASE)).strip()
        base = base.rstrip("/")
        base = base.removesuffix("/v2")
        return base or DEFAULT_API_BASE

    @property
    def timeout(self) -> int:
        return max(5, _as_int(_config_value(self.config, "request_timeout", 15), 15))

    @property
    def proxy(self) -> str:
        return str(_config_value(self.config, "proxy", "") or "").strip()

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "AstrBot/astrbot_plugin_luoyue_music "
                        "(+https://github.com/xZhongjie/astrbot_plugin_luoyue_music)"
                    ),
                    "Accept": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------
    async def _request(self, source: str, params: dict) -> dict:
        """请求落月 API，并在音质超限时自动降级重试。

        落月 API 会按线路限制最高音质（例如网易云当前仅允许 1-3），
        报错信息形如“quality 必须小于等于 3”。这里解析该上限并自动重试，
        避免用户把配置调高后整个功能不可用。
        """
        try:
            return await self._request_raw(source, params)
        except LuoyueError as exc:
            match = re.search(r"quality\s*必须小于等于\s*(\d+)", str(exc))
            if match and "quality" in params:
                limit = int(match.group(1))
                if _as_int(params.get("quality"), 0) > limit:
                    retry_params = dict(params)
                    retry_params["quality"] = limit
                    logger.info("落月 API 音质上限为 %s，自动降级后重试", limit)
                    return await self._request_raw(source, retry_params)
            raise

    async def _request_raw(self, source: str, params: dict) -> dict:
        """请求落月 API 并返回 JSON。

        网络错误 / 超时会自动重试一次；业务错误直接抛出 :class:`LuoyueError`。
        """
        if source not in SOURCE_LABELS:
            raise LuoyueError(f"不支持的音源：{source}")

        url = f"{self.base_url}/v2/music/{source}"
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                session = await self._get_session()
                async with session.get(
                    url,
                    params=params,
                    proxy=self.proxy or None,
                ) as response:
                    text = await response.text()
                    try:
                        body = json.loads(text)
                    except ValueError:
                        raise LuoyueError(
                            f"落月 API 返回了无法解析的数据（HTTP {response.status}）"
                        ) from None

                    if response.status >= 400:
                        message = body.get("message") if isinstance(body, dict) else ""
                        raise LuoyueError(
                            message or f"落月 API 请求失败（HTTP {response.status}）"
                        )

                    if not isinstance(body, dict):
                        raise LuoyueError("落月 API 返回格式异常")

                    code = body.get("code")
                    if code not in (0, 200, "0", "200"):
                        message = body.get("message") or f"接口返回错误码 {code}"
                        raise LuoyueError(str(message))
                    return body
            except LuoyueError:
                raise
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                logger.warning(
                    "落月 API 请求失败（第 %s 次）：%s %s",
                    attempt + 1,
                    url,
                    exc,
                )
                if attempt == 0:
                    await asyncio.sleep(0.6)

        raise LuoyueError(f"无法连接落月 API：{last_error}")

    # ------------------------------------------------------------------
    # 业务接口
    # ------------------------------------------------------------------
    async def search(
        self,
        source: str,
        keyword: str,
        limit: int,
        quality: int,
    ) -> list[Song]:
        """搜索歌曲，返回候选列表。"""
        body = await self._request(
            source,
            {
                "word": keyword,
                "page": 1,
                "num": limit,
                "quality": quality,
            },
        )
        data = body.get("data")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise LuoyueError("接口没有返回搜索结果")

        songs = [
            Song.from_api(source, item)
            for item in data
            if isinstance(item, dict) and (item.get("song") or item.get("title"))
        ]
        songs = [song for song in songs if song.song_id or song.mid]
        for index, song in enumerate(songs, 1):
            song.keyword = keyword
            song.index = index
        return songs

    async def play(self, song: Song, quality: int) -> dict:
        """获取歌曲播放信息。

        网易云会先按配置音质请求，再降到标准音质重试，避免线路只提供低音质；
        QQ 音乐在 id 失败时再尝试 mid；网易云 id 失败时再尝试 choose 模式。
        """
        if song.source == "netease":
            # 免费可听的最高音质是标准 128k（2），再高需要 VIP，直接按 2 请求
            quality = min(max(quality, 1), 2)
            qualities = [quality]
            if quality > 1:
                qualities.append(1)
        else:
            qualities = [quality]

        errors: list[str] = []
        connection_failed = False
        for quality_value in qualities:
            for params in self._play_attempts(song, quality_value):
                if not any(params.get(key) for key in ("id", "mid", "choose")):
                    continue
                try:
                    body = await self._request(song.source, params)
                except LuoyueError as exc:
                    errors.append(str(exc))
                    if _is_connection_error(exc):
                        connection_failed = True
                        break
                    continue

                data = body.get("data")
                if isinstance(data, list) and data:
                    data = data[0]
                if isinstance(data, dict):
                    url = str(data.get("url") or "").strip()
                    if url:
                        return data
                errors.append("接口没有返回播放链接")
            if connection_failed:
                break

        detail = errors[-1] if errors else "未知错误"
        raise LuoyueError(
            f"该歌曲暂时没有可用的播放链接（可能受版权 / VIP 限制）：{detail}"
        )

    @staticmethod
    def _play_attempts(song: Song, quality: int) -> list[dict]:
        """构造同一音质下的多种取链请求参数。"""
        attempts: list[dict] = [{"id": song.song_id, "quality": quality}]
        if song.source == "tencent":
            attempts[0]["type"] = song.song_type
            if song.mid and song.mid != song.song_id:
                attempts.append(
                    {"mid": song.mid, "quality": quality, "type": song.song_type}
                )
        elif song.source == "netease" and song.keyword and song.index:
            attempts.append(
                {
                    "word": song.keyword,
                    "choose": song.index,
                    "quality": quality,
                }
            )
        return attempts

    async def _probe_audio(self, url: str) -> bool:
        """探测 URL 是否真的是音频文件（排除 404 页面等）。"""
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=min(self.timeout, 5))
            async with session.get(
                url,
                headers={
                    "Range": "bytes=0-1",
                    "User-Agent": "Mozilla/5.0 (compatible; AstrBot)",
                },
                allow_redirects=True,
                timeout=timeout,
            ) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                return response.status in (200, 206) and (
                    "audio/" in content_type or "octet-stream" in content_type
                )
        except (TimeoutError, aiohttp.ClientError):
            return False

    async def netease_outer(self, song: Song) -> dict | None:
        """网易云官方标准音质备用线路（无需登录，免费歌曲可用）。"""
        if song.source != "netease" or not song.song_id.isdigit():
            return None

        url = f"https://music.163.com/song/media/outer/url?id={song.song_id}.mp3"
        if not await self._probe_audio(url):
            return None
        return {
            "url": url,
            "quality": "标准音质（免费线路 128k）",
            "interval": song.duration,
            "size": song.size,
            "link": f"https://music.163.com/#/song?id={song.song_id}",
            "_force_custom_card": True,
        }

    async def download(self, url: str, dest: Path) -> None:
        """把音频下载到本地文件（用于语音分片）。"""
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
        try:
            async with session.get(
                url,
                proxy=self.proxy or None,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    raise LuoyueError(f"音频下载失败（HTTP {response.status}）")
                with dest.open("wb") as file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        file.write(chunk)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise LuoyueError(f"音频下载失败：{exc}") from exc


def _build_music_card(song: Song, audio_url: str, force_custom: bool = False):
    """构造 OneBot 音乐卡片组件。

    说明：AstrBot 的 ``Comp.Music`` 使用 pydantic 模型，字段 ``_type`` 以下划线
    开头——pydantic v1 会忽略构造参数中的同名字段，pydantic v2 则把它当作私有
    属性。这里在构造完成后用 ``object.__setattr__`` 直接写入实例字典，保证
    ``toDict()`` 能序列化出正确的 OneBot ``music`` 消息段。

    返回 ``None`` 表示无法构造卡片，此时调用方应退回链接 / 文件模式。
    """
    card = Comp.Music()

    # 网易云：原生 163 音乐卡片，只需要歌曲 id
    if not force_custom and song.source == "netease" and song.song_id.isdigit():
        object.__setattr__(card, "id", int(song.song_id))
        object.__setattr__(card, "_type", "163")
        return card

    # QQ 音乐：原生 qq 音乐卡片，需要 songmid
    if not force_custom and song.source == "tencent" and song.mid:
        object.__setattr__(card, "id", song.mid)
        object.__setattr__(card, "_type", "qq")
        return card

    # 兜底：自定义音乐卡片（直链播放）
    if audio_url:
        object.__setattr__(card, "_type", "custom")
        object.__setattr__(card, "url", song.link or "")
        object.__setattr__(card, "audio", audio_url)
        object.__setattr__(card, "title", f"{song.title} - {song.singer}".strip(" -"))
        object.__setattr__(card, "image", song.cover or "")
        return card

    return None


def _parse_duration(value: str) -> int | None:
    """把“4分29秒”“3:03”之类的时长解析为秒数。解析失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None

    match = re.fullmatch(
        r"(?:(?P<h>\d+)\s*(?:小时|时|h))?\s*"
        r"(?:(?P<m>\d+)\s*(?:分钟|分|m))?\s*"
        r"(?:(?P<s>\d+)\s*(?:秒|s))?",
        text,
        flags=re.IGNORECASE,
    )
    if match and any(match.groups()):
        hours = int(match.group("h") or 0)
        minutes = int(match.group("m") or 0)
        seconds = int(match.group("s") or 0)
        return hours * 3600 + minutes * 60 + seconds

    match = re.fullmatch(r"(\d+):(\d{1,2})", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    return None


def _safe_filename(song: Song, url: str) -> str:
    suffix = PurePosixPath(urlparse(url).path).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix or ""):
        suffix = ".mp3"
    stem = f"{song.title} - {song.singer}".strip(" -").replace("/", "_")
    stem = re.sub(r'[\\:*?"<>|\r\n]+', "_", stem) or "song"
    return f"{stem[:80]}{suffix}"


def format_search_result(
    source: str,
    keyword: str,
    songs: list[Song],
    ttl: int,
    mode: str | None = None,
) -> str:
    lines = [
        f"🎵 落月点歌 · {SOURCE_LABELS.get(source, source)}",
        f"🔍 搜索：{keyword}（共 {len(songs)} 首）",
        "",
    ]
    for index, song in enumerate(songs, 1):
        head = f"{index}. {song.title}"
        if song.singer:
            head += f" - {song.singer}"
        lines.append(head)

        meta: list[str] = []
        if song.album:
            meta.append(f"《{song.album}》")
        if song.quality:
            meta.append(song.quality)
        if song.pay and song.pay not in {"免费", "0", ""}:
            meta.append(song.pay)
        if meta:
            lines.append("    " + " · ".join(meta))

    lines.extend(["", f"回复「/点歌 序号」或「/选歌 序号」播放，{ttl} 秒内有效。"])
    if mode:
        lines.append(f"本次发送方式：{MODE_LABELS.get(mode, mode)}")
    return "\n".join(lines)


def format_play_info(song: Song, play: dict) -> str:
    quality = str(play.get("quality") or song.quality or "").strip()
    duration = str(play.get("interval") or song.duration or "").strip()
    size = str(play.get("size") or song.size or "").strip()
    link = str(play.get("link") or song.link or "").strip()

    lines = [f"🎵 {song.title}"]
    if song.singer:
        lines.append(f"👤 歌手：{song.singer}")
    if song.album:
        lines.append(f"💿 专辑：{song.album}")

    detail: list[str] = []
    if quality:
        detail.append(quality)
    if duration:
        detail.append(f"⏱ {duration}")
    if size:
        detail.append(size)
    if detail:
        lines.append("🎧 " + " · ".join(detail))

    if link:
        lines.append(f"🔗 {link}")
    return "\n".join(lines)


class LuoyueMusicPlugin(Star):
    """落月点歌插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = LuoyueClient(self.config)
        self._sessions: dict[str, SearchSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._source_cache: dict[str, str] = {}
        self._outer_blocked_until = 0.0
        logger.info(
            "落月点歌插件已加载，注册指令：点歌 / 选歌 / 直接点歌 / "
            "语音点歌 / 文件点歌 / 点歌源 / 点歌帮助"
        )

    async def terminate(self):
        """插件卸载 / 停用时关闭 HTTP 会话。"""
        await self.client.close()

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _limit(self) -> int:
        return min(
            10, max(1, _as_int(_config_value(self.config, "search_limit", 5), 5))
        )

    def _ttl(self) -> int:
        return min(
            600, max(15, _as_int(_config_value(self.config, "selection_ttl", 120), 120))
        )

    def _quality(self, source: str) -> int:
        if source == "netease":
            value = _as_int(
                _config_value(self.config, "quality_netease", 2),
                2,
            )
            return min(2, max(1, value))
        value = _as_int(_config_value(self.config, "quality_tencent", 8), 8)
        return min(16, max(0, value))

    def _auto_play_single(self) -> bool:
        return bool(_config_value(self.config, "auto_play_single", True))

    def _auto_source_fallback(self) -> bool:
        return bool(_config_value(self.config, "auto_source_fallback", True))

    def _netease_outer_fallback(self) -> bool:
        return bool(_config_value(self.config, "netease_outer_fallback", True))

    def _show_cover(self) -> bool:
        return bool(_config_value(self.config, "show_cover", False))

    def _voice_max_seconds(self) -> int:
        return max(
            0,
            _as_int(_config_value(self.config, "voice_max_seconds", 60), 60),
        )

    def _voice_split(self) -> bool:
        return bool(_config_value(self.config, "voice_split", True))

    def _send_mode(
        self,
        event: AstrMessageEvent,
        explicit: str | None = None,
    ) -> str:
        mode = (explicit or "").lower() or str(
            _config_value(self.config, "send_mode", "auto")
        ).lower()
        platform = event.get_platform_name()

        if mode == "auto":
            return "card" if platform == "aiocqhttp" else "link"
        if mode == "card" and platform != "aiocqhttp":
            return "link"
        if mode not in {"card", "link", "record", "file"}:
            return "link"
        return mode

    # ------------------------------------------------------------------
    # 会话状态
    # ------------------------------------------------------------------
    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}::{event.get_sender_id()}"

    def _prune_sessions(self) -> None:
        now = time.time()
        ttl = self._ttl()
        expired = [
            key
            for key, session in self._sessions.items()
            if now - session.created_at > ttl
        ]
        for key in expired:
            self._sessions.pop(key, None)

        # 限制缓存条数，避免长时间运行后无限增长
        if len(self._sessions) > 300:
            oldest = sorted(
                self._sessions.items(),
                key=lambda item: item[1].created_at,
            )[: len(self._sessions) - 300]
            for key, _ in oldest:
                self._sessions.pop(key, None)

    async def _get_source(self, event: AstrMessageEvent) -> str:
        umo = event.unified_msg_origin
        cached = self._source_cache.get(umo)
        if cached in SOURCE_LABELS:
            return cached

        default = str(_config_value(self.config, "default_source", "tencent")).lower()
        if default not in SOURCE_LABELS:
            default = "tencent"

        saved = ""
        try:
            saved = str(await self.get_kv_data(f"luoyue_source:{umo}", ""))
        except Exception:  # noqa: BLE001 - 旧版本 AstrBot 无 KV 存储
            saved = ""

        source = saved if saved in SOURCE_LABELS else default
        self._source_cache[umo] = source
        return source

    async def _set_source(self, event: AstrMessageEvent, source: str) -> None:
        self._source_cache[event.unified_msg_origin] = source
        try:
            await self.put_kv_data(f"luoyue_source:{event.unified_msg_origin}", source)
        except Exception:  # noqa: BLE001 - 旧版本 AstrBot 无 KV 存储
            logger.debug("保存音源偏好失败，仅本次运行有效")

    # ------------------------------------------------------------------
    # 指令参数解析
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_args(event: AstrMessageEvent, names: tuple[str, ...]) -> str:
        """从消息文本中取出指令名之后的参数。

        不使用 GreedyStr 是为了让 ``/点歌``（不带参数）也能直接返回帮助，
        而不是在指令过滤阶段抛出“必要参数缺失”。
        """
        text = (event.message_str or "").strip()
        if not text:
            return ""

        alternatives = "|".join(
            re.escape(name) for name in sorted(names, key=len, reverse=True)
        )
        match = re.search(
            rf"(?:^|\s)(?:{alternatives})(?:\s+|$)",
            text,
        )
        if not match:
            return ""
        return text[match.end() :].strip()

    @staticmethod
    def _split_mode(text: str) -> tuple[str, str | None]:
        """拆分参数末尾的发送方式，例如“晴天 语音” -> ("晴天", "record")。"""
        parts = text.rsplit(maxsplit=1)
        if len(parts) == 2:
            mode = MODE_ALIASES.get(parts[1].strip().lower())
            if mode:
                return parts[0].strip(), mode
        return text.strip(), None

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("点歌", alias={"落月点歌"})
    @_stop_ai_reply
    async def cmd_request(self, event: AstrMessageEvent):
        """点歌：/点歌 歌名 搜索，/点歌 序号 选择候选。"""
        keyword, mode = self._split_mode(
            self._extract_args(event, ("点歌", "落月点歌"))
        )
        if not keyword:
            yield event.plain_result(HELP_TEXT)
            return

        if keyword.isdigit():
            components = await self._select_song(event, int(keyword), mode)
            if components:
                yield event.chain_result(components)
            return

        components = await self._search(event, keyword, mode=mode)
        if components:
            yield event.chain_result(components)

    @filter.command("选歌", alias={"选"})
    @_stop_ai_reply
    async def cmd_select(self, event: AstrMessageEvent):
        """选择搜索候选中的歌曲：/选歌 序号 [语音|文件|卡片|链接]。"""
        args, mode = self._split_mode(self._extract_args(event, ("选歌", "选")))
        if not args.isdigit():
            yield event.plain_result(
                "用法：/选歌 序号 [语音|文件|卡片|链接]\n"
                "请先发送「/点歌 歌名」获取候选列表。"
            )
            return
        components = await self._select_song(event, int(args), mode)
        if components:
            yield event.chain_result(components)

    @filter.command("直接点歌", alias={"点播", "来一首"})
    @_stop_ai_reply
    async def cmd_play_direct(self, event: AstrMessageEvent):
        """直接播放搜索结果第一首：/直接点歌 歌名 [语音|文件]。"""
        keyword, mode = self._split_mode(
            self._extract_args(event, ("直接点歌", "点播", "来一首"))
        )
        if not keyword:
            yield event.plain_result(
                "用法：/直接点歌 歌名 [语音|文件]\n示例：/直接点歌 晴天"
            )
            return
        components = await self._search(event, keyword, direct=True, mode=mode)
        if components:
            yield event.chain_result(components)

    @filter.command("语音点歌", alias={"语音点播"})
    @_stop_ai_reply
    async def cmd_voice(self, event: AstrMessageEvent):
        """直接以语音条发送：/语音点歌 歌名。"""
        keyword = self._extract_args(event, ("语音点歌", "语音点播"))
        if not keyword:
            yield event.plain_result(
                "用法：/语音点歌 歌名\n示例：/语音点歌 晴天\n"
                "注意：语音模式需要 AstrBot 所在机器安装 ffmpeg。"
            )
            return
        components = await self._search(event, keyword, direct=True, mode="record")
        if components:
            yield event.chain_result(components)

    @filter.command("文件点歌", alias={"文件点播"})
    @_stop_ai_reply
    async def cmd_file(self, event: AstrMessageEvent):
        """直接以音频文件发送：/文件点歌 歌名。"""
        keyword = self._extract_args(event, ("文件点歌", "文件点播"))
        if not keyword:
            yield event.plain_result("用法：/文件点歌 歌名\n示例：/文件点歌 晴天")
            return
        components = await self._search(event, keyword, direct=True, mode="file")
        if components:
            yield event.chain_result(components)

    @filter.command("点歌源", alias={"音源"})
    @_stop_ai_reply
    async def cmd_source(self, event: AstrMessageEvent):
        """查看或切换音源：/点歌源 网易云|QQ音乐。"""
        args = self._extract_args(event, ("点歌源", "音源"))
        current = await self._get_source(event)

        if not args:
            yield event.plain_result(
                "🎧 当前音源："
                f"{SOURCE_LABELS[current]}\n"
                "可用音源：网易云音乐（netease）、QQ 音乐（tencent）\n"
                "发送「/点歌源 网易云」或「/点歌源 QQ音乐」切换。"
            )
            return

        target = SOURCE_ALIASES.get(args.strip().lower())
        if target is None:
            yield event.plain_result(
                "❌ 无法识别的音源。\n可用：网易云音乐（netease）、QQ 音乐（tencent）"
            )
            return

        await self._set_source(event, target)
        yield event.plain_result(f"✅ 已切换音源为：{SOURCE_LABELS[target]}")

    @filter.command("点歌帮助", alias={"点歌菜单", "点歌说明"})
    @_stop_ai_reply
    async def cmd_help(self, event: AstrMessageEvent):
        """显示点歌帮助。"""
        yield event.plain_result(HELP_TEXT)

    # ------------------------------------------------------------------
    # 业务逻辑
    # ------------------------------------------------------------------
    async def _search(
        self,
        event: AstrMessageEvent,
        keyword: str,
        direct: bool = False,
        mode: str | None = None,
    ) -> list:
        source = await self._get_source(event)
        self._prune_sessions()

        try:
            songs = await self.client.search(
                source,
                keyword,
                self._limit(),
                self._quality(source),
            )
        except LuoyueError as exc:
            logger.warning("搜索歌曲失败：%s", exc)
            return [Comp.Plain(f"❌ 搜索失败：{exc}")]
        except Exception:  # noqa: BLE001 - 插件兜底，避免中断消息处理
            logger.exception("搜索歌曲时发生未预期错误")
            return [Comp.Plain("❌ 搜索失败：插件内部错误，请查看 AstrBot 日志。")]

        if not songs:
            return [Comp.Plain(f"🔍 没有找到「{keyword}」相关的歌曲，换个关键词试试～")]

        if direct or (self._auto_play_single() and len(songs) == 1):
            return await self._play(event, songs[0], mode)

        self._sessions[self._session_key(event)] = SearchSession(
            source=source,
            keyword=keyword,
            songs=songs,
            created_at=time.time(),
            mode=mode,
        )
        return [
            Comp.Plain(format_search_result(source, keyword, songs, self._ttl(), mode))
        ]

    async def _select_song(
        self,
        event: AstrMessageEvent,
        index: int,
        mode: str | None = None,
    ) -> list:
        self._prune_sessions()
        session = self._sessions.get(self._session_key(event))
        if session is None:
            return [Comp.Plain("⚠️ 当前没有待选择的歌曲，请先发送「/点歌 歌名」。")]

        if not 1 <= index <= len(session.songs):
            return [
                Comp.Plain(
                    f"⚠️ 序号超出范围，请输入 1 - {len(session.songs)} 之间的数字。"
                )
            ]

        return await self._play(event, session.songs[index - 1], mode or session.mode)

    async def _play(
        self,
        event: AstrMessageEvent,
        song: Song,
        mode: str | None = None,
    ) -> list:
        key = self._session_key(event)
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            # 网易云优先使用官方免费线路（标准 128k），避免先请求高音质再兜底
            if (
                song.source == "netease"
                and self._netease_outer_fallback()
                and time.time() >= self._outer_blocked_until
                and hasattr(self.client, "netease_outer")
            ):
                outer = await self.client.netease_outer(song)
                if outer:
                    self._sessions.pop(key, None)
                    components = await self._maybe_send_split_voice(
                        event, song, outer, mode
                    )
                    if components is not None:
                        return components
                    return self._build_play_chain(
                        event,
                        song,
                        outer,
                        mode,
                        "🎧 已直接使用网易云标准音质免费线路（128k）。\n",
                    )
                # 免费线路被风控/失败后，10 分钟内不再反复探测
                self._outer_blocked_until = time.time() + 600

            try:
                play = await self.client.play(song, self._quality(song.source))
            except LuoyueError as exc:
                logger.warning("获取歌曲播放链接失败：%s", exc)
                components, ok = await self._fallback_play(event, song, mode, exc)
                if ok:
                    self._sessions.pop(key, None)
                return components
            except Exception:  # noqa: BLE001 - 插件兜底，避免中断消息处理
                logger.exception("获取歌曲播放链接时发生未预期错误")
                return [Comp.Plain("❌ 播放失败：插件内部错误，请查看 AstrBot 日志。")]

            self._sessions.pop(key, None)
            components = await self._maybe_send_split_voice(event, song, play, mode)
            if components is not None:
                return components
            return self._build_play_chain(event, song, play, mode)

    async def _fallback_play(
        self,
        event: AstrMessageEvent,
        song: Song,
        mode: str | None,
        error: Exception,
    ) -> tuple[list, bool]:
        """当前音源取不到链接时，自动到另一音源搜索同名歌曲。

        返回 ``(消息组件, 是否成功)``。
        """
        if not self._auto_source_fallback():
            return self._play_error_chain(song, error), False

        other = "tencent" if song.source == "netease" else "netease"

        # 不同音源对关键词的容忍度不同：先试“歌名 歌手”，再试“歌名+歌手”，
        # 最后退回只搜歌名。上游对空格的处理偶尔不稳定，多试几种更可靠。
        keywords: list[str] = []
        for value in (
            f"{song.title} {song.singer}".strip(),
            f"{song.title}+{song.singer}".strip("+ "),
            song.title.strip(),
        ):
            if value and value not in keywords:
                keywords.append(value)

        candidates: list[Song] = []
        for keyword in keywords:
            try:
                candidates = await self.client.search(
                    other,
                    keyword,
                    self._limit(),
                    self._quality(other),
                )
            except Exception as exc:  # noqa: BLE001 - 兜底流程失败时继续尝试下一种关键词
                logger.warning("跨音源搜索失败：%s %s", other, keyword)
                if _is_connection_error(exc):
                    break
                continue
            if candidates:
                break

        candidate = self._pick_similar(song, candidates)
        if candidate is not None:
            try:
                play = await self.client.play(candidate, self._quality(other))
            except LuoyueError as exc:
                logger.warning("跨音源取链失败：%s", exc)
            else:
                note = (
                    f"🔁 {SOURCE_LABELS[song.source]}暂无可用播放链接，"
                    f"已自动切换到 {SOURCE_LABELS[other]}。\n"
                )
                return (
                    self._build_play_chain(event, candidate, play, mode, note),
                    True,
                )

        return self._play_error_chain(song, error), False

    @staticmethod
    def _pick_similar(song: Song, candidates: list[Song]) -> Song | None:
        """从另一音源的搜索结果中挑出与目标最接近的一首。"""
        if not candidates:
            return None

        def normalize(value: str) -> str:
            return re.sub(r"[\s\-_/·・]+", "", value or "").lower()

        target_title = normalize(song.title)
        target_singer = normalize(song.singer)

        for candidate in candidates:
            if normalize(candidate.title) == target_title:
                return candidate

        for candidate in candidates:
            title = normalize(candidate.title)
            if target_title and (target_title in title or title in target_title):
                return candidate

        for candidate in candidates:
            if target_singer and normalize(candidate.singer) == target_singer:
                return candidate

        return candidates[0]

    @staticmethod
    def _web_link(song: Song) -> str:
        """没有可用播放链接时，构造歌曲播放页地址。"""
        if song.link:
            return song.link
        if song.source == "netease" and song.song_id:
            return f"https://music.163.com/#/song?id={song.song_id}"
        if song.source == "tencent":
            mid = song.mid or song.song_id
            if mid:
                return f"https://i.y.qq.com/v8/playsong.html?songmid={mid}&type=0"
        return ""

    def _play_error_chain(self, song: Song, error: Exception) -> list:
        lines = [f"❌ 播放失败：{error}"]
        link = self._web_link(song)
        if link:
            lines.append(f"🔗 播放页：{link}")
        lines.append(
            "💡 可以试试切换音源（/点歌源 网易云 或 /点歌源 QQ音乐），或换一首歌。"
        )
        return [Comp.Plain("\n".join(lines))]

    async def _maybe_send_split_voice(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
        mode: str | None = None,
    ) -> list | None:
        """语音超过单段上限时，下载并分片发送。返回 ``[]`` 表示已发送。"""
        if self._send_mode(event, mode) != "record":
            return None

        limit = self._voice_max_seconds()
        if limit <= 0 or not self._voice_split():
            return None
        if shutil.which("ffmpeg") is None:
            return None

        audio_url = str(play.get("url") or "").strip()
        if not audio_url:
            return None

        duration = _parse_duration(str(play.get("interval") or song.duration or ""))
        if duration is None:
            duration = await self._probe_audio_duration(audio_url)
        if duration is None or duration <= limit:
            return None

        try:
            return await self._send_split_voice(event, song, play, limit)
        except Exception:  # noqa: BLE001 - 分片失败时退回文件发送
            logger.exception("语音分片失败，改用音频文件发送")
            play["_voice_split_failed"] = True
            return None

    async def _send_split_voice(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
        limit: int,
    ) -> list:
        work_dir = Path(tempfile.mkdtemp(prefix="astrbot_luoyue_voice_"))
        try:
            source = work_dir / "source"
            await self.client.download(str(play["url"]), source)
            chunks = await self._split_audio(source, limit, work_dir)
            if not chunks:
                raise RuntimeError("没有生成任何语音分片")

            total = len(chunks)
            header = (
                format_play_info(song, play)
                + f"\n🎤 已按 {limit} 秒分片，共 {total} 段。"
            )
            for index, chunk in enumerate(chunks, 1):
                components: list = []
                if index == 1:
                    components.append(Comp.Plain(header))
                else:
                    components.append(Comp.Plain(f"第 {index}/{total} 段"))
                components.append(Comp.Record(file=str(chunk), url=str(chunk)))
                await event.send(event.chain_result(components))
                await asyncio.sleep(0.5)
            return []
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _split_audio(
        self,
        source: Path,
        seconds: int,
        work_dir: Path,
    ) -> list[Path]:
        out_dir = work_dir / "chunks"
        out_dir.mkdir()
        ffmpeg = shutil.which("ffmpeg")
        pattern = str(out_dir / "part_%03d.wav")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(seconds),
            "-reset_timestamps",
            "1",
            pattern,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", "replace")[-400:])
        return sorted(out_dir.glob("part_*.wav"))

    async def _probe_audio_duration(self, url: str) -> int | None:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            url,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            value = stdout.decode("utf-8", "replace").strip()
            return int(float(value))
        except (ValueError, OSError):
            return None

    def _build_play_chain(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
        mode: str | None = None,
        extra_note: str = "",
    ) -> list:
        audio_url = str(play.get("url") or "").strip()
        if not audio_url:
            return [Comp.Plain("❌ 接口返回的播放链接为空，请稍后再试。")]

        info = extra_note + format_play_info(song, play)
        if not song.link:
            song.link = str(play.get("link") or "")
        mode = self._send_mode(event, mode)
        note = ""

        if mode == "record" and shutil.which("ffmpeg") is None:
            # AstrBot 发送语音前会将音频转码为 wav，缺少 ffmpeg 会直接失败
            logger.warning("未检测到 ffmpeg，语音模式改为音频文件发送")
            mode = "file"
            note = (
                "\n⚠️ 未检测到 ffmpeg，语音发送已改为音频文件。"
                "安装 ffmpeg 后即可正常发送语音。"
            )

        if mode == "record":
            limit = self._voice_max_seconds()
            seconds = _parse_duration(str(play.get("interval") or song.duration or ""))
            over_limit = limit > 0 and seconds is not None and seconds > limit
            if play.get("_voice_split_failed") or over_limit:
                logger.info(
                    "语音分片未完成（时长 %s 秒，单段上限 %s 秒），改为文件发送",
                    seconds,
                    limit,
                )
                mode = "file"
                note = (
                    "\n⚠️ 语音分片未完成或超过单段上限，已改为发送音频文件。"
                    "可在配置中调整 voice_max_seconds / voice_split。"
                )

        components: list = [Comp.Plain(info + note)]

        if mode == "card":
            card = _build_music_card(
                song,
                audio_url,
                force_custom=bool(play.get("_force_custom_card")),
            )
            if card is not None:
                components.append(card)
            else:
                logger.warning("未能构造音乐卡片，退回链接模式")
                mode = "link"
        elif mode == "record":
            # 语音模式：AstrBot 会下载音频并转码为 wav 后发送（需要 ffmpeg）
            components.append(Comp.Record(file=audio_url, url=audio_url))
        elif mode == "file":
            components.append(
                Comp.File(name=_safe_filename(song, audio_url), url=audio_url)
            )

        if mode == "link" and self._show_cover() and song.cover:
            components.insert(0, Comp.Image.fromURL(song.cover))

        return components
