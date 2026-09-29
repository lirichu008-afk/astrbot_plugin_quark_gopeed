"""astrbot_plugin_quark_gopeed —— 微信发送夸克链接，自动调用 GoPeed 下载。

工作流程::

    微信文本消息
      -> 正则识别 pan.quark.cn/s/xxx 与提取码
      -> 白名单校验
      -> 夸克：换取 stoken -> 列分享 -> 转存 -> 轮询 -> 换直链
      -> GoPeed：逐个创建下载任务
      -> 回复用户结果

设计要点：
* 全部 I/O 均为异步，不阻塞 AstrBot 事件循环；
* 先回复「已受理」，再 await 解析，最后回复结果（AstrBot 支持 async generator 多次 yield）；
* 全程不打印 Cookie / Token 明文，避免日志泄露凭据。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

try:  # AstrBot 环境
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
except Exception:  # pragma: no cover - 便于脱离 AstrBot 做静态检查
    logger = logging.getLogger("astrbot_plugin_quark_gopeed")
    AstrMessageEvent = Any  # type: ignore[assignment,misc]

    class _Stub:
        """脱离 AstrBot 时的占位实现，仅为让模块可被导入。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _StubFilter:  # noqa: D101
        class EventMessageType:  # noqa: D106
            ALL = "all"

        @staticmethod
        def event_message_type(*args: Any, **kwargs: Any) -> Any:
            def decorator(func: Any) -> Any:
                return func

            return decorator

    def register(*args: Any, **kwargs: Any) -> Any:  # noqa: D103
        def decorator(cls: Any) -> Any:
            return cls

        return decorator

    Context = Star = _Stub  # type: ignore[assignment,misc]
    filter = _StubFilter()  # type: ignore[assignment]

# 兼容两种加载方式：作为包加载时用相对导入，作为顶层模块时用绝对导入
try:
    from .gopeed_client import (
        DEFAULT_API_PATH,
        DEFAULT_AUTH_HEADER,
        DEFAULT_AUTH_SCHEME,
        DEFAULT_PAYLOAD_TEMPLATE,
        GopeedClient,
    )
    from .quark_client import (
        DEFAULT_USER_AGENT,
        QuarkAuthError,
        QuarkClient,
        QuarkError,
        QuarkPasscodeError,
    )
    from .search_base import DownloadLink, SearchError, SearchResult
    from .search_engine import SearchEngine, available_sources, create_sources
    from .session import SessionStore
except ImportError:  # pragma: no cover
    from gopeed_client import (  # type: ignore[no-redef]
        DEFAULT_API_PATH,
        DEFAULT_AUTH_HEADER,
        DEFAULT_AUTH_SCHEME,
        DEFAULT_PAYLOAD_TEMPLATE,
        GopeedClient,
    )
    from quark_client import (  # type: ignore[no-redef]
        DEFAULT_USER_AGENT,
        QuarkAuthError,
        QuarkClient,
        QuarkError,
        QuarkPasscodeError,
    )
    from search_base import (  # type: ignore[no-redef]
        DownloadLink,
        SearchError,
        SearchResult,
    )
    from search_engine import (  # type: ignore[no-redef]
        SearchEngine,
        available_sources,
        create_sources,
    )
    from session import SessionStore  # type: ignore[no-redef]


PLUGIN_NAME = "astrbot_plugin_quark_gopeed"
PLUGIN_VERSION = "1.3.3"

# --------------------------------------------------------------------------- #
# 指令正则
# --------------------------------------------------------------------------- #
#: 搜索指令：搜索稻香 / 搜 稻香 / 查找 稻香
#: 刻意不收「找」这种过宽的前缀，避免把「找你有事」当成搜索词。
#: 「搜(?!索)」用于挡住正则回溯：否则只发「搜索」两字时，
#: 会先试「搜索」失败、再回溯到「搜」，把剩下的「索」当成关键词。
SEARCH_CMD_RE = re.compile(r"^(?:搜索|搜一下|搜(?!索)|查找)\s*[:：]?\s*(.+)$")
#: 确认下载：下载1 / 下载 1 / 下载第1个 / 下 2
CONFIRM_CMD_RE = re.compile(r"^(?:下载|下)\s*第?\s*(\d+)\s*个?$")


# --------------------------------------------------------------------------- #
# 链接解析
# --------------------------------------------------------------------------- #
#: 匹配 pan.quark.cn / drive.quark.cn 的分享链接
QUARK_LINK_RE = re.compile(
    r"https?://(?:pan|drive)\.quark\.cn/s/([0-9A-Za-z_-]+)", re.IGNORECASE
)
#: 链接内嵌的提取码，如 ?pwd=abcd
PWD_INLINE_RE = re.compile(r"[?&]pwd=([0-9A-Za-z]{1,8})", re.IGNORECASE)
#: 文本里的提取码，如「提取码：1234」「密码: abcd」
PWD_TEXT_RE = re.compile(
    r"(?:提取码|访问码|提取密码|密码|pwd)\s*[:：=]?\s*([0-9A-Za-z]{4})",
    re.IGNORECASE,
)


def parse_quark_link(text: str) -> tuple[str, str | None] | None:
    """从一段文本中解析夸克分享链接与提取码。

    Args:
        text: 用户发送的原始消息文本。

    Returns:
        ``(share_id, password)`` —— 未找到链接时返回 ``None``；
        找到链接但无提取码时 ``password`` 为 ``None``。

    Examples:
        >>> parse_quark_link("https://pan.quark.cn/s/abc123 提取码：8f2k")
        ('abc123', '8f2k')
        >>> parse_quark_link("看看这个 https://pan.quark.cn/s/abc123?pwd=9x7q")
        ('abc123', '9x7q')
        >>> parse_quark_link("今天天气不错") is None
        True
    """
    if not text:
        return None

    match = QUARK_LINK_RE.search(text)
    if not match:
        return None

    share_id = match.group(1)

    password: str | None = None
    inline = PWD_INLINE_RE.search(text)
    if inline:
        password = inline.group(1)
    else:
        textual = PWD_TEXT_RE.search(text)
        if textual:
            password = textual.group(1)

    return share_id, password


# --------------------------------------------------------------------------- #
# 文件名处理
# --------------------------------------------------------------------------- #
#: 匹配「纯 hash 文件名」，例如 6762d0f94c1b9a7f58584885833290176c0ae13f
HASH_NAME_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
#: Windows/Linux 文件名里的非法字符
ILLEGAL_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def looks_like_hash_name(name: str) -> bool:
    """判断文件名是否像网盘的 hash 命名。

    网盘分享里常见上传者直接把文件存成 hash 名（一串 16 位以上的十六进制），
    这种名字对用户毫无意义；若还拿不到别的名字，界面（和下载目录）里就会出现
    「一串字符」。空名字也算不可读。
    """
    if not name:
        return True
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return bool(HASH_NAME_RE.match(stem.strip()))


def sanitize_filename(name: str) -> str:
    """清掉文件名里的非法字符并限长，避免写入失败。"""
    cleaned = ILLEGAL_NAME_RE.sub("_", name or "").strip(" .")
    return cleaned[:150]


def build_name_from_hint(hint: str, original: str) -> str:
    """用可读的名字（如「歌名 — 歌手」）替换不可读的文件名，尽量保留扩展名。"""
    ext = ""
    if "." in (original or ""):
        ext = "." + original.rsplit(".", 1)[1]
    return sanitize_filename(f"{hint}{ext}")


# --------------------------------------------------------------------------- #
# 事件字段提取（兼容不同 AstrBot 版本的字段差异）
# --------------------------------------------------------------------------- #
def _extract_text(event: Any) -> str:
    """尽力从事件中取出纯文本内容。"""
    for attr in ("message_str",):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    message_obj = getattr(event, "message_obj", None)
    if message_obj is not None:
        value = getattr(message_obj, "message_str", None)
        if isinstance(value, str) and value.strip():
            return value.strip()

        chain = getattr(message_obj, "message", None)
        if chain:
            parts: list[str] = []
            for component in chain:
                text = getattr(component, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            joined = " ".join(parts).strip()
            if joined:
                return joined

    value = getattr(event, "message", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _extract_sender_id(event: Any) -> str:
    """尽力从事件中取出发送者 ID。"""
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            value = getter()
            if value:
                return str(value)
        except Exception:  # pragma: no cover - 不同版本实现差异
            pass
    for attr in ("sender_id", "user_id", "sender"):
        value = getattr(event, attr, None)
        if value:
            return str(value)
    return ""


def _extract_group_id(event: Any) -> str:
    """尽力从事件中取出群 ID（私聊返回空串）。"""
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
            if value:
                return str(value)
        except Exception:  # pragma: no cover
            pass
    value = getattr(event, "group_id", None)
    return str(value) if value else ""


# --------------------------------------------------------------------------- #
# 需求中约定的模块级函数
# --------------------------------------------------------------------------- #
async def get_quark_download_url(
    share_id: str,
    password: str | None,
    cookie: str,
    *,
    max_files: int = 5,
    recursive: bool = False,
    timeout: float = 30.0,
    save_dir_fid: str = "0",
    task_timeout: float = 90.0,
) -> list[str]:
    """解析夸克分享，返回下载直链列表。

    Args:
        share_id: 分享 ID。
        password: 提取码，可为 ``None``。
        cookie: 夸克账号 Cookie。
        max_files: 最多解析的文件数。
        recursive: 是否递归处理文件夹。
        timeout: HTTP 超时秒数。
        save_dir_fid: 转存目标目录 fid。
        task_timeout: 等待转存任务的最长秒数。

    Returns:
        下载直链字符串列表。

    Raises:
        QuarkAuthError: Cookie 失效。
        QuarkPasscodeError: 提取码错误或缺失。
        QuarkError: 其他解析失败。
    """
    async with QuarkClient(
        cookie, timeout=timeout, save_dir_fid=save_dir_fid
    ) as client:
        downloads = await client.resolve_share(
            share_id,
            password,
            max_files=max_files,
            recursive=recursive,
            task_timeout=task_timeout,
        )
    return [item["url"] for item in downloads]


async def create_gopeed_task(
    download_url: str,
    host: str,
    port: int | str,
    token: str,
    *,
    api_path: str = DEFAULT_API_PATH,
    auth_header: str = "Authorization",
    auth_scheme: str = "Bearer",
    payload_template: str = DEFAULT_PAYLOAD_TEMPLATE,
    name: str = "",
    path: str = "",
    use_ssl: bool = False,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """向 GoPeed 提交一个下载任务。

    Returns:
        ``(是否成功, 提示信息)``
    """
    async with GopeedClient(
        host=host,
        port=port,
        token=token,
        api_path=api_path,
        auth_header=auth_header,
        auth_scheme=auth_scheme,
        payload_template=payload_template,
        use_ssl=use_ssl,
        timeout=timeout,
    ) as client:
        return await client.create_task(download_url, name=name, path=path)


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
@register(
    PLUGIN_NAME,
    "lirichu008-afk",
    "微信发送夸克网盘分享链接，自动解析直链并调用 GoPeed 创建下载任务",
    PLUGIN_VERSION,
)
class QuarkGopeedPlugin(Star):
    """夸克网盘 -> GoPeed 下载插件。"""

    def __init__(self, context: Any, config: Any = None) -> None:
        super().__init__(context)
        self.config = config
        #: 最近的提交记录，用于去重：{share_id: 提交时间戳}
        self._recent: dict[str, float] = {}
        #: 会话状态：等待用户确认下载的搜索结果
        self._sessions = SessionStore(
            ttl=self._cfg_int("search_session_ttl", 300),
            max_sessions=self._cfg_int("search_max_sessions", 500),
        )
        #: 搜索源实例（懒加载）
        self._search_engine: SearchEngine | None = None
        logger.info(
            "%s v%s 已加载（搜索源：%s）",
            PLUGIN_NAME,
            PLUGIN_VERSION,
            ", ".join(available_sources()),
        )

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def _cfg(self, key: str, default: Any = None) -> Any:
        """读取配置项，兼容 dict / 对象 / AstrBotConfig 三种形态。"""
        cfg = self.config
        if cfg is None:
            return default

        if isinstance(cfg, dict):
            value = cfg.get(key, default)
            return default if value is None else value

        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                value = getter(key, default)
                return default if value is None else value
            except Exception:  # pragma: no cover
                pass

        value = getattr(cfg, key, default)
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_list(self, key: str, default: list[str] | None = None) -> list[str]:
        value = self._cfg(key, default or [])
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,\s;]+", value) if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    # ------------------------------------------------------------------ #
    # 权限与去重
    # ------------------------------------------------------------------ #
    def _is_allowed(self, event: Any) -> bool:
        allowed = self._cfg_list("allowed_users")
        if not allowed:
            return True  # 未配置则允许所有人

        allowed_set = {item.lower() for item in allowed}
        candidates = {
            _extract_sender_id(event).lower(),
            _extract_group_id(event).lower(),
        }
        candidates.discard("")
        return bool(candidates & allowed_set)

    def _is_duplicate(self, share_id: str) -> bool:
        window = self._cfg_int("dedup_seconds", 300)
        if window <= 0:
            return False
        now = time.monotonic()
        # 顺手清理过期记录，避免字典无限增长
        self._recent = {
            key: ts for key, ts in self._recent.items() if now - ts < window
        }
        last = self._recent.get(share_id)
        if last is not None and now - last < window:
            return True
        self._recent[share_id] = now
        return False

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: Any):
        """消息总入口，按优先级依次尝试：搜索指令 -> 确认下载 -> 夸克链接。"""
        text = _extract_text(event)
        if not text:
            return

        stripped = text.strip()

        # ---- 1. 搜索指令 ----
        match = SEARCH_CMD_RE.match(stripped)
        if match:
            async for chunk in self._handle_search(event, match.group(1).strip()):
                yield chunk
            return

        # ---- 2. 确认下载 ----
        match = CONFIRM_CMD_RE.match(stripped)
        if match:
            async for chunk in self._handle_confirm(event, int(match.group(1))):
                yield chunk
            return

        # ---- 3. 夸克分享链接（原有逻辑）----
        parsed = parse_quark_link(text)
        if not parsed:
            return

        if not self._is_allowed(event):
            logger.info(
                "用户 %s 不在 allowed_users 白名单中，已忽略",
                _extract_sender_id(event) or "未知",
            )
            return

        share_id, password = parsed

        if self._is_duplicate(share_id):
            yield event.plain_result("⚠️ 该链接刚刚已提交过，请勿重复发送。")
            return

        cookie = str(self._cfg("quark_cookie", "") or "").strip()
        if not cookie:
            yield event.plain_result(
                "❌ 插件尚未配置夸克 Cookie。\n"
                "请在 AstrBot WebUI 的插件配置中填写 quark_cookie。"
            )
            return

        yield event.plain_result("🔍 已收到夸克分享链接，正在解析并提交下载…")

        try:
            results = await self._process(share_id, password, cookie)
        except QuarkAuthError as exc:
            logger.warning("夸克 Cookie 失效：%s", exc)
            yield event.plain_result(
                f"❌ 夸克 Cookie 已失效，请更新配置。\n原因：{exc}"
            )
            return
        except QuarkPasscodeError as exc:
            yield event.plain_result(
                f"❌ 提取码错误或缺失。\n原因：{exc}\n"
                "请在消息中附上提取码，例如「提取码：1234」。"
            )
            return
        except QuarkError as exc:
            logger.warning("夸克解析失败：%s", exc)
            yield event.plain_result(f"❌ 夸克解析失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，避免插件整体异常
            logger.exception("处理夸克链接时出现未预期错误")
            yield event.plain_result(f"❌ 处理时出现未预期错误：{exc}")
            return

        yield event.plain_result(self._format_reply(results))

    # ------------------------------------------------------------------ #
    # 搜索：搜索指令
    # ------------------------------------------------------------------ #
    def _session_key(self, event: Any) -> str:
        """会话隔离键：群聊按「群 + 人」，私聊按人。"""
        group = _extract_group_id(event)
        sender = _extract_sender_id(event) or "unknown"
        return f"group:{group}:{sender}" if group else f"private:{sender}"

    def _get_search_engine(self) -> SearchEngine | None:
        """构造并缓存搜索引擎。"""
        if self._search_engine is not None:
            return self._search_engine

        names = self._cfg_list("search_sources") or available_sources()
        sources = create_sources(
            names,
            {
                "timeout": self._cfg_float("search_timeout", 20.0),
                "user_agent": str(self._cfg("search_user_agent", "") or ""),
                "base_url": str(self._cfg("search_base_url", "") or ""),
            },
        )
        if not sources:
            logger.error("没有可用的搜索源，search_sources=%r", names)
            return None

        self._search_engine = SearchEngine(
            sources,
            only_quark=self._cfg_bool("search_only_quark", True),
            max_results=self._cfg_int("search_max_results", 10),
        )
        return self._search_engine

    async def _handle_search(self, event: Any, keyword: str):
        """处理「搜索 XX」。"""
        if not self._cfg_bool("search_enabled", True):
            return

        if not self._is_allowed(event):
            logger.info("用户不在白名单，忽略搜索请求")
            return

        if not keyword:
            yield event.plain_result("用法：搜索 <关键词>，例如「搜索 稻香」")
            return

        engine = self._get_search_engine()
        if engine is None:
            yield event.plain_result("❌ 没有可用的搜索源，请检查配置 search_sources。")
            return

        yield event.plain_result(f"🔍 正在搜索「{keyword}」…")

        try:
            outcome = await engine.search(keyword)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免影响插件其他功能
            logger.exception("搜索出错")
            yield event.plain_result(f"❌ 搜索失败：{exc}")
            return

        if not outcome.results:
            lines = [f"😕 没有找到与「{keyword}」相关的夸克资源。"]
            if outcome.total_found:
                lines.append(f"（共搜到 {outcome.total_found} 条，但都没有夸克链接）")
            lines.extend(f"⚠️ {err}" for err in outcome.errors)
            yield event.plain_result("\n".join(lines))
            return

        self._sessions.save(self._session_key(event), keyword, outcome.results)

        ttl = self._cfg_int("search_session_ttl", 300)
        minutes = max(ttl // 60, 1)
        lines = [f"🔍 「{keyword}」找到 {len(outcome.results)} 条夸克资源：", ""]
        for idx, item in enumerate(outcome.results, 1):
            lines.append(item.format_line(idx))
        if outcome.skipped_no_quark:
            lines.append("")
            lines.append(f"（已过滤 {outcome.skipped_no_quark} 条无夸克链接的结果）")
        lines.append("")
        lines.append(f"回复「下载 序号」开始下载，{minutes} 分钟内有效")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 搜索：确认下载
    # ------------------------------------------------------------------ #
    async def _handle_confirm(self, event: Any, index: int):
        """处理「下载 N」。"""
        if not self._cfg_bool("search_enabled", True):
            return
        if not self._is_allowed(event):
            return

        key = self._session_key(event)
        pending = self._sessions.get(key)
        if pending is None:
            # 群里静默忽略，避免把别人的正常聊天（如「下载1」）当成指令；
            # 私聊里给出提示更有帮助。
            if not _extract_group_id(event):
                yield event.plain_result(
                    "没有待确认的搜索结果。请先发「搜索 关键词」。"
                )
            return

        item = pending.pick(index)
        if item is None:
            yield event.plain_result(
                f"⚠️ 序号 {index} 超出范围，当前只有 {len(pending.results)} 条。"
            )
            return

        cookie = str(self._cfg("quark_cookie", "") or "").strip()
        if not cookie:
            yield event.plain_result(
                "❌ 插件尚未配置夸克 Cookie，请先在配置中填写 quark_cookie。"
            )
            return

        engine = self._get_search_engine()
        if engine is None:
            yield event.plain_result("❌ 没有可用的搜索源。")
            return

        yield event.plain_result(f"🔍 正在解析「{item.title}」的下载链接…")

        try:
            links = await engine.get_downloads(item.source, item.detail_id)
        except SearchError as exc:
            yield event.plain_result(f"❌ 获取下载链接失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("获取下载链接出错")
            yield event.plain_result(f"❌ 获取下载链接失败：{exc}")
            return

        quark_links = [link for link in links if link.is_quark]
        if not quark_links:
            yield event.plain_result(
                f"❌ 「{item.title}」没有可用的夸克链接（可能只提供其他网盘）。"
            )
            return

        if self._cfg_bool("search_pick_first_quark", False):
            quark_links = quark_links[:1]

        # 用完即清，避免同一条被反复提交
        self._sessions.clear(key)

        names = "、".join(link.quality or "未知" for link in quark_links)
        yield event.plain_result(f"📥 开始处理「{item.title}」（{names}）…")

        # 用搜索结果里的「歌名 — 歌手」作为兜底文件名。
        # 夸克那边给出的名字可能是空的，也可能只是一串 hash（分享者上传时就那样命名），
        # 这时用它能避免下载目录里出现「一串字符」。
        name_hint = item.title.strip()
        if item.artist:
            name_hint = f"{item.title} - {item.artist}"

        summaries: list[tuple[str, bool, str]] = []
        for link in quark_links:
            parsed = parse_quark_link(link.url)
            if not parsed:
                summaries.append(
                    (f"{item.title} [{link.quality or '未知'}]", False, f"无法识别链接：{link.url}")
                )
                continue

            share_id, password = parsed
            try:
                summaries.extend(
                    await self._process(
                        share_id,
                        password,
                        cookie,
                        name_hint=name_hint,
                        force_name=self._cfg_bool("search_rename_by_result", True),
                    )
                )
            except QuarkAuthError as exc:
                logger.warning("夸克 Cookie 失效：%s", exc)
                yield event.plain_result(f"❌ 夸克 Cookie 已失效，请更新配置。\n原因：{exc}")
                return
            except QuarkPasscodeError as exc:
                summaries.append(
                    (f"{item.title} [{link.quality or '未知'}]", False, f"需要提取码：{exc}")
                )
            except QuarkError as exc:
                summaries.append((f"{item.title} [{link.quality or '未知'}]", False, str(exc)))
            except Exception as exc:  # noqa: BLE001
                logger.exception("提交下载时出错")
                summaries.append(
                    (f"{item.title} [{link.quality or '未知'}]", False, f"未预期错误：{exc}")
                )

        yield event.plain_result(self._format_reply(summaries))

    # ------------------------------------------------------------------ #
    # 业务处理
    # ------------------------------------------------------------------ #
    async def _process(
        self,
        share_id: str,
        password: str | None,
        cookie: str,
        *,
        name_hint: str = "",
        force_name: bool = False,
    ) -> list[tuple[str, bool, str]]:
        """解析夸克直链并逐个提交到 GoPeed。

        Args:
            name_hint: 可读的名字（如「歌名 — 歌手」）。
            force_name: 为 True 时**总是**用 name_hint 命名，而不是只在文件名
                不可读时才替换。搜索下载会开启它 —— 因为网盘分享里的文件名
                常带上传者的无意义数字（如「周杰伦 - (1785291570)七里香(2).mp3」），
                用户期望看到的是自己搜的那个「歌名 — 歌手」。

        Returns:
            ``[(文件名, 是否成功, 提示信息), ...]``
        """
        max_files = max(1, self._cfg_int("max_files", 5))
        recursive = self._cfg_bool("quark_recursive", False)
        request_timeout = self._cfg_float("request_timeout", 30.0)
        task_timeout = self._cfg_float("quark_task_timeout", 90.0)

        async with QuarkClient(
            cookie,
            timeout=request_timeout,
            save_dir_fid=str(self._cfg("quark_save_dir_fid", "0") or "0"),
        ) as quark:
            downloads = await quark.resolve_share(
                share_id,
                password,
                max_files=max_files,
                recursive=recursive,
                task_timeout=task_timeout,
            )
            # ⚠️ 必须在会话结束前导出：夸克在 API 调用中会刷新 __puus 会话令牌，
            # 下载直链必须携带「刷新后」的 Cookie，否则夸克 CDN 返回 412。
            # 这里绝不写日志，避免凭据泄露。
            fresh_cookie_header = quark.export_cookie_header()

        if not downloads:
            raise QuarkError("未能换取到任何下载直链")

        # 传给 GoPeed 的下载请求头。缺了它们夸克 CDN 会返回 412 Precondition Failed。
        download_headers: dict[str, str] = {}
        if self._cfg_bool("gopeed_forward_cookies", True) and fresh_cookie_header:
            download_headers = {
                "Cookie": fresh_cookie_header,
                "Referer": "https://pan.quark.cn/",
                "User-Agent": str(
                    self._cfg("quark_user_agent", DEFAULT_USER_AGENT)
                    or DEFAULT_USER_AGENT
                ),
            }
            logger.info(
                "将随下载任务转发 %d 个请求头（含刷新后的 Cookie）给 GoPeed",
                len(download_headers),
            )
        else:
            logger.warning(
                "未转发 Cookie 给 GoPeed（gopeed_forward_cookies 已关闭或 Cookie 为空）。"
                "若夸克 CDN 返回 412，请开启该选项。"
            )

        results: list[tuple[str, bool, str]] = []
        download_path = str(self._cfg("gopeed_download_path", "") or "")
        async with GopeedClient(
            host=str(self._cfg("gopeed_host", "127.0.0.1")),
            port=self._cfg_int("gopeed_port", 9999),
            token=str(self._cfg("gopeed_token", "") or ""),
            api_path=str(
                self._cfg("gopeed_api_path", DEFAULT_API_PATH) or DEFAULT_API_PATH
            ),
            auth_header=str(
                self._cfg("gopeed_auth_header", DEFAULT_AUTH_HEADER)
                or DEFAULT_AUTH_HEADER
            ),
            auth_scheme=str(self._cfg("gopeed_auth_scheme", DEFAULT_AUTH_SCHEME)),
            payload_template=str(
                self._cfg("gopeed_payload_template", DEFAULT_PAYLOAD_TEMPLATE)
                or DEFAULT_PAYLOAD_TEMPLATE
            ),
            use_ssl=self._cfg_bool("gopeed_use_ssl", False),
            timeout=self._cfg_float("gopeed_timeout", 15.0),
        ) as gopeed:
            for item in downloads:
                file_name = (item.get("file_name") or "").strip()

                # 文件名不可读（空或纯 hash），或调用方要求强制命名时，
                # 用可读名字替代，否则 GoPeed 会拿夸克直链 URL 路径里的 hash
                # 或分享者留下的无意义数字串当文件名。
                if name_hint and (force_name or looks_like_hash_name(file_name)):
                    renamed = build_name_from_hint(name_hint, file_name)
                    logger.info(
                        "文件名 %r -> 改用可读名：%s",
                        file_name or "(空)",
                        renamed,
                    )
                    file_name = renamed

                ok, message = await gopeed.create_task(
                    item["url"],
                    name=file_name,
                    path=download_path,
                    headers=download_headers,
                )
                # 失败必须记进日志：否则只能在微信回复里看到原因，运维时查不到
                if ok:
                    logger.info("GoPeed 已接受任务：%s", file_name or "未命名文件")
                else:
                    logger.warning(
                        "GoPeed 拒绝任务：%s -> %s",
                        file_name or "未命名文件",
                        message,
                    )
                results.append((file_name or "未命名文件", ok, message))

        return results

    @staticmethod
    def _format_reply(results: list[tuple[str, bool, str]]) -> str:
        """把处理结果拼装成给用户看的文本。"""
        if not results:
            return "❌ 没有可提交的文件。"

        succeeded = [r for r in results if r[1]]
        failed = [r for r in results if not r[1]]

        lines: list[str] = []
        if succeeded:
            lines.append(
                f"✅ 已提交 {len(succeeded)} 个下载任务，文件将很快开始下载。"
            )
            for name, _, _ in succeeded:
                lines.append(f"  • {name}")

        if failed:
            lines.append("")
            lines.append(f"❌ 有 {len(failed)} 个文件提交失败：")
            for name, _, message in failed:
                lines.append(f"  • {name}")
                lines.append(f"    原因：{message}")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def terminate(self) -> None:
        """插件卸载/重载时清理内存状态。"""
        self._recent.clear()
        self._sessions.clear_all()
        self._search_engine = None
        logger.info("%s 已卸载", PLUGIN_NAME)
