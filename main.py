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
except ImportError:  # pragma: no cover
    from gopeed_client import (  # type: ignore[no-redef]
        DEFAULT_API_PATH,
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


PLUGIN_NAME = "astrbot_plugin_quark_gopeed"
PLUGIN_VERSION = "1.2.0"

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
    "lee",
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
        logger.info("%s v%s 已加载", PLUGIN_NAME, PLUGIN_VERSION)

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
        """监听全部消息，命中夸克链接时自动处理。"""
        text = _extract_text(event)
        if not text:
            return

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
    # 业务处理
    # ------------------------------------------------------------------ #
    async def _process(
        self, share_id: str, password: str | None, cookie: str
    ) -> list[tuple[str, bool, str]]:
        """解析夸克直链并逐个提交到 GoPeed。

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
                self._cfg("gopeed_auth_header", "Authorization") or "Authorization"
            ),
            auth_scheme=str(self._cfg("gopeed_auth_scheme", "Bearer")),
            payload_template=str(
                self._cfg("gopeed_payload_template", DEFAULT_PAYLOAD_TEMPLATE)
                or DEFAULT_PAYLOAD_TEMPLATE
            ),
            use_ssl=self._cfg_bool("gopeed_use_ssl", False),
            timeout=self._cfg_float("gopeed_timeout", 15.0),
        ) as gopeed:
            for item in downloads:
                file_name = item.get("file_name") or ""
                ok, message = await gopeed.create_task(
                    item["url"],
                    name=file_name,
                    path=download_path,
                    headers=download_headers,
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
        logger.info("%s 已卸载", PLUGIN_NAME)
