"""夸克网盘异步客户端。

⚠️ 重要免责说明
----------------
夸克网盘**没有公开的官方 API**。本文件中的所有接口路径、参数名、错误码语义，
均来自社区逆向实践的整理，**未经官方文档确认，且可能随夸克前端更新而失效**。

如果某天插件突然解析失败，请优先怀疑这里。所有端点都集中在下方 ENDPOINTS 区，
便于集中修正，不需要改动 main.py。

核心流程（分享链接 -> 下载直链）::

    1. share/sharepage/token   用 pwd_id + 提取码换取 stoken
    2. share/sharepage/detail  用 stoken 列出分享内的文件
    3. share/sharepage/save    把文件转存到自己的网盘（必须，否则拿不到直链）
    4. task                    轮询转存任务，取得转存后的 fid
    5. file/download           用 fid 换取带签名的下载直链

注意第 3 步：夸克分享链接**无法直接下载**，必须先转存到自己账号。
直链带有签名且**有效期很短**，拿到后应尽快提交给下载器。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

try:  # AstrBot 环境优先使用宿主 logger；独立测试时回退到标准 logging
    from astrbot.api import logger
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_quark_gopeed.quark")


# --------------------------------------------------------------------------- #
# 端点定义（接口如有变更，改这里）
# --------------------------------------------------------------------------- #
PAN_HOME = "https://pan.quark.cn"
DRIVE_HOST = "https://drive-pc.quark.cn"
API_PREFIX = f"{DRIVE_HOST}/1/clouddrive"

EP_SHARE_TOKEN = "/share/sharepage/token"
EP_SHARE_DETAIL = "/share/sharepage/detail"
EP_SHARE_SAVE = "/share/sharepage/save"
EP_TASK = "/task"
EP_FILE_SORT = "/file/sort"
EP_FILE_DOWNLOAD = "/file/download"

#: 转存任务状态码（社区约定，未经官方确认）
TASK_STATUS_DONE = 2
TASK_STATUS_FAILED = 3

PAGE_SIZE = 50
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class QuarkError(Exception):
    """夸克接口调用失败的通用异常。"""


class QuarkAuthError(QuarkError):
    """Cookie 失效 / 未登录。"""


class QuarkPasscodeError(QuarkError):
    """提取码错误或缺失。"""


class QuarkClient:
    """夸克网盘异步客户端。

    Args:
        cookie: 夸克账号的完整 Cookie 字符串（必填）。
        timeout: 单次 HTTP 请求超时秒数。
        save_dir_fid: 转存目标目录的 fid，``"0"`` 表示网盘根目录。
        user_agent: 自定义 User-Agent，默认使用桌面版 Chrome。
    """

    def __init__(
        self,
        cookie: str,
        *,
        timeout: float = 30.0,
        save_dir_fid: str = "0",
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._cookie = (cookie or "").strip()
        if not self._cookie:
            raise QuarkError("夸克 Cookie 为空，请在插件配置中填写 quark_cookie")
        self._timeout = float(timeout)
        self._save_fid = str(save_dir_fid or "0")
        self._ua = user_agent
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "QuarkClient":
        # ⚠️ 关键：Cookie 必须交给 httpx 的 cookie jar 管理，**不能**写死在请求头里。
        #
        # 实测（2026-09-25，夸克 CDN 返回 412 Precondition Failed）：
        # 夸克在每次 API 调用时会通过 Set-Cookie 下发新的会话令牌 __puus。
        # 下载直链时必须使用「刷新后」的 __puus，否则 CDN 一律返回 412。
        # 若把 Cookie 固定写在 headers 里，httpx 虽然收到了 Set-Cookie，
        # 却永远不会把它用于后续请求 —— 插件就会一直拿过期会话去下载。
        #
        # 用 cookies= 传入后，httpx 会自动完成「接收新值 -> 后续请求使用新值」。
        self._client = httpx.AsyncClient(
            cookies=self._parse_cookie(self._cookie),
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
            headers={
                "User-Agent": self._ua,
                "Referer": f"{PAN_HOME}/",
                "Origin": PAN_HOME,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # Cookie 管理
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_cookie(cookie: str) -> httpx.Cookies:
        """把浏览器复制来的 Cookie 字符串转成 httpx 的 cookie jar。"""
        jar = httpx.Cookies()
        for part in (cookie or "").split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                jar.set(name, value.strip(), domain=".quark.cn")
        return jar

    def export_cookies(self) -> dict[str, str]:
        """导出当前会话的 Cookie，**包含夸克服务端刷新过的字段**。

        下载直链时必须使用这份 Cookie，否则 CDN 会返回 412。
        导出结果仅用于提交给下载器，请勿写入日志。
        """
        if self._client is None:
            return {}
        return {c.name: c.value for c in self._client.cookies.jar}

    def export_cookie_header(self) -> str:
        """把当前会话 Cookie 拼成可直接放进 HTTP 头的字符串。"""
        return "; ".join(f"{k}={v}" for k, v in self.export_cookies().items())

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _params(**extra: Any) -> dict[str, Any]:
        """夸克接口的公共查询参数。"""
        params: dict[str, Any] = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "__dt": int(time.time() * 1000),
            "__t": int(time.time() * 1000),
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    @staticmethod
    def _raise_for_code(code: Any, message: Any) -> None:
        """把夸克返回的业务错误码翻译成带语义的异常。

        夸克的错误码没有公开文档，因此这里以 ``message`` 的中文关键字判定为主，
        ``code`` 仅用于日志与提示，避免因码值变动而误判。
        """
        msg = str(message or "")
        if any(k in msg for k in ("登录", "未登录", "失效", "重新登录", "身份")):
            raise QuarkAuthError(f"Cookie 可能已失效（code={code}, message={msg}）")
        if any(k in msg for k in ("提取码", "密码", "passcode")):
            raise QuarkPasscodeError(f"提取码错误或缺失（code={code}, message={msg}）")
        raise QuarkError(f"夸克接口返回错误：code={code}, message={msg}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """发起一次夸克 API 请求，返回 ``data`` 字段（可能是 dict 或 list）。"""
        if self._client is None:
            raise QuarkError("QuarkClient 未初始化，请使用 async with 语法")

        url = f"{API_PREFIX}{path}"
        try:
            resp = await self._client.request(
                method, url, params=params, json=json_body
            )
        except httpx.TimeoutException as exc:
            raise QuarkError(f"请求夸克接口超时（{path}）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise QuarkError(f"请求夸克接口失败（{path}）：{exc}") from exc

        if resp.status_code != 200:
            raise QuarkError(
                f"夸克接口 HTTP {resp.status_code}：{resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise QuarkError(
                f"夸克接口返回非 JSON 内容：{resp.text[:200]}"
            ) from exc

        if not isinstance(payload, dict):
            raise QuarkError(f"夸克接口返回格式异常：{str(payload)[:200]}")

        code = payload.get("code", 0)
        if code != 0:
            self._raise_for_code(code, payload.get("message"))
        return payload.get("data")

    # ------------------------------------------------------------------ #
    # 步骤 1：换取 stoken
    # ------------------------------------------------------------------ #
    async def get_stoken(self, pwd_id: str, passcode: str | None = None) -> str:
        """用分享 ID 与提取码换取访问令牌 stoken。"""
        body = {"pwd_id": pwd_id, "passcode": passcode or ""}
        data = await self._request(
            "POST", EP_SHARE_TOKEN, params=self._params(), json_body=body
        )
        if not isinstance(data, dict) or not data.get("stoken"):
            raise QuarkError("未能获取 stoken，分享可能已失效或需要提取码")
        return str(data["stoken"])

    # ------------------------------------------------------------------ #
    # 步骤 2：列出分享内容
    # ------------------------------------------------------------------ #
    async def list_share(
        self,
        pwd_id: str,
        stoken: str,
        *,
        pdir_fid: str = "0",
        page: int = 1,
        size: int = PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """列出分享链接某一层目录下的条目（文件和文件夹）。"""
        params = self._params(
            pwd_id=pwd_id,
            stoken=stoken,
            pdir_fid=pdir_fid,
            force="0",
            _page=page,
            _size=size,
            _sort="file_type:asc,updated_at:desc",
        )
        data = await self._request("GET", EP_SHARE_DETAIL, params=params)
        if isinstance(data, dict):
            items = data.get("list") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [it for it in items if isinstance(it, dict)]

    async def list_share_all(
        self, pwd_id: str, stoken: str, *, max_items: int = 50
    ) -> list[dict[str, Any]]:
        """分页列出一层目录，直到取满 ``max_items`` 或没有更多数据。"""
        collected: list[dict[str, Any]] = []
        page = 1
        while len(collected) < max_items:
            batch = await self.list_share(pwd_id, stoken, page=page)
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        return collected[:max_items]

    # ------------------------------------------------------------------ #
    # 步骤 3：转存到自己的网盘
    # ------------------------------------------------------------------ #
    async def save_share(
        self,
        pwd_id: str,
        stoken: str,
        fid_list: list[str],
        fid_token_list: list[str],
        *,
        to_pdir_fid: str | None = None,
    ) -> str:
        """把分享中的文件转存到自己的网盘，返回转存任务 ID。"""
        body = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": str(to_pdir_fid or self._save_fid),
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        data = await self._request(
            "POST", EP_SHARE_SAVE, params=self._params(), json_body=body
        )
        if not isinstance(data, dict) or not data.get("task_id"):
            raise QuarkError("转存请求未返回 task_id")
        return str(data["task_id"])

    # ------------------------------------------------------------------ #
    # 步骤 4：轮询任务
    # ------------------------------------------------------------------ #
    async def wait_task(
        self, task_id: str, *, timeout: float = 90.0, interval: float = 1.0
    ) -> dict[str, Any]:
        """轮询转存任务直到完成，返回任务详情。"""
        deadline = time.monotonic() + max(timeout, 1.0)
        retry_index = 0
        while time.monotonic() < deadline:
            params = self._params(task_id=task_id, retry_index=retry_index)
            data = await self._request("GET", EP_TASK, params=params)
            if not isinstance(data, dict):
                raise QuarkError("任务查询返回格式异常")

            status = data.get("status")
            if status == TASK_STATUS_DONE:
                return data
            if status == TASK_STATUS_FAILED:
                raise QuarkError(
                    f"转存任务失败：{data.get('message') or data}"
                )
            retry_index += 1
            await asyncio.sleep(interval)
        raise QuarkError(f"转存任务超时（task_id={task_id}）")

    @staticmethod
    def _extract_saved_fids(task: dict[str, Any]) -> list[str]:
        """从转存任务结果里取出转存后产生的顶层 fid。"""
        save_as = task.get("save_as")
        if isinstance(save_as, dict):
            fids = save_as.get("save_as_top_fids")
            if isinstance(fids, list):
                return [str(f) for f in fids if f]
        return []

    # ------------------------------------------------------------------ #
    # 自己网盘的目录浏览（仅当需要递归文件夹时使用）
    # ------------------------------------------------------------------ #
    async def list_my_files(
        self, *, pdir_fid: str = "0", page: int = 1, size: int = PAGE_SIZE
    ) -> list[dict[str, Any]]:
        """列出自己网盘某个目录下的内容。"""
        params = self._params(
            pdir_fid=pdir_fid,
            force="0",
            _page=page,
            _size=size,
            _sort="file_type:asc,updated_at:desc",
        )
        data = await self._request("GET", EP_FILE_SORT, params=params)
        if isinstance(data, dict):
            items = data.get("list") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [it for it in items if isinstance(it, dict)]

    async def _walk_dir(
        self, pdir_fid: str, *, depth: int = 0, max_depth: int = 3
    ) -> list[str]:
        """递归收集某个目录下所有文件的 fid。"""
        if depth > max_depth:
            logger.warning("目录递归超过最大深度 %s，已停止：fid=%s", max_depth, pdir_fid)
            return []
        collected: list[str] = []
        try:
            entries = await self.list_my_files(pdir_fid=pdir_fid)
        except QuarkError as exc:
            logger.warning("列出目录失败（fid=%s）：%s", pdir_fid, exc)
            return []
        for entry in entries:
            fid = entry.get("fid")
            if not fid:
                continue
            if entry.get("dir"):
                collected.extend(
                    await self._walk_dir(str(fid), depth=depth + 1, max_depth=max_depth)
                )
            else:
                collected.append(str(fid))
        return collected

    async def _expand_top_fids(
        self, top_fids: list[str], *, recursive: bool
    ) -> list[str]:
        """把转存得到的顶层 fid 展开成「可直接下载的文件 fid」列表。"""
        try:
            entries = await self.list_my_files(pdir_fid=self._save_fid)
        except QuarkError as exc:
            logger.warning("无法列出转存目标目录，将直接使用顶层 fid：%s", exc)
            return top_fids

        by_fid = {str(e.get("fid")): e for e in entries if e.get("fid")}
        result: list[str] = []
        for fid in top_fids:
            entry = by_fid.get(fid)
            if entry is None:
                # 目录列表里没找到（可能转存到了别处），仍然尝试直接下载
                result.append(fid)
                continue
            if entry.get("dir"):
                if recursive:
                    result.extend(await self._walk_dir(fid, max_depth=3))
                else:
                    logger.info("跳过文件夹（未开启 recursive）：%s", entry.get("file_name"))
            else:
                result.append(fid)
        return result

    # ------------------------------------------------------------------ #
    # 步骤 5：换取下载直链
    # ------------------------------------------------------------------ #
    async def get_download_urls(self, fids: list[str]) -> list[dict[str, str]]:
        """用 fid 列表换取带签名的下载直链。

        Returns:
            ``[{"url": ..., "file_name": ...}, ...]``
        """
        if not fids:
            return []
        body = {"fids": fids}
        data = await self._request(
            "POST", EP_FILE_DOWNLOAD, params=self._params(), json_body=body
        )

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("list") or []
        else:
            items = []

        results: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("download_url")
            if url:
                results.append(
                    {
                        "url": str(url),
                        "file_name": str(item.get("file_name") or ""),
                    }
                )
        return results

    # ------------------------------------------------------------------ #
    # 编排：一步到位
    # ------------------------------------------------------------------ #
    async def resolve_share(
        self,
        pwd_id: str,
        passcode: str | None = None,
        *,
        max_files: int = 5,
        recursive: bool = False,
        task_timeout: float = 90.0,
    ) -> list[dict[str, str]]:
        """完整流程：分享链接 -> 下载直链列表。

        Args:
            pwd_id: 分享 ID（链接里 ``/s/`` 之后的部分）。
            passcode: 提取码，无则传 ``None``。
            max_files: 最多处理多少个文件，防止一次提交过量任务。
            recursive: 分享里含文件夹时，是否递归收集文件夹内的文件。
            task_timeout: 等待转存任务完成的最长秒数。

        Returns:
            ``[{"url": 直链, "file_name": 文件名}, ...]``
        """
        stoken = await self.get_stoken(pwd_id, passcode)
        entries = await self.list_share_all(pwd_id, stoken, max_items=max_files * 4)
        if not entries:
            raise QuarkError("分享内容为空，或分享链接已失效")

        files = [e for e in entries if not e.get("dir") and e.get("fid")]
        dirs = [e for e in entries if e.get("dir")]

        if not files and not dirs:
            raise QuarkError("分享中没有可下载的内容")
        if not files and not recursive:
            raise QuarkError(
                f"该分享包含 {len(dirs)} 个文件夹但没有可下载的文件。"
                "若需下载文件夹内容，请在插件配置中开启 quark_recursive"
            )

        targets = files[:max_files]
        dir_targets = dirs[:max_files] if recursive else []

        fid_list = [str(e["fid"]) for e in targets + dir_targets]
        # 转存必须同时提供 share_fid_token，缺失会导致转存被拒
        fid_token_list = [str(e.get("share_fid_token") or "") for e in targets + dir_targets]
        if not any(fid_token_list):
            logger.warning(
                "分享条目缺少 share_fid_token 字段，转存可能失败。"
                "如遇失败请检查夸克接口是否变更。"
            )

        logger.info(
            "开始转存：pwd_id=%s，文件=%s，文件夹=%s，目标目录=%s",
            pwd_id, len(targets), len(dir_targets), self._save_fid,
        )
        task_id = await self.save_share(pwd_id, stoken, fid_list, fid_token_list)
        task = await self.wait_task(task_id, timeout=task_timeout)

        top_fids = self._extract_saved_fids(task)
        if not top_fids:
            raise QuarkError(
                "转存完成但未能解析出文件 ID（save_as_top_fids 为空）。"
                "可能是夸克接口有变更，请查看插件日志。"
            )

        file_fids = await self._expand_top_fids(top_fids, recursive=recursive)
        if not file_fids:
            raise QuarkError("转存后没有找到可下载的文件")

        downloads = await self.get_download_urls(file_fids)
        if not downloads:
            raise QuarkError("未能换取到任何下载直链")
        return downloads
