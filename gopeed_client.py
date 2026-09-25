"""GoPeed 下载器异步客户端。

⚠️ 关于接口的不确定性
--------------------
需求里描述的是「TCP 协议」，但同时给出的又是 ``http://host:port`` 形式的地址，
这两者是矛盾的 —— 这里按 **HTTP REST** 实现（GoPeed 确实在 9999 端口提供 HTTP 服务）。

另外据我所知，GoPeed 官方 REST API 创建任务的端点更可能是 ``/api/v1/tasks``，
请求体形如 ``{"req": {"url": ...}, "opt": {...}}``；而认证头也未必是
``Authorization: Bearer``。**我无法在当前环境联网核实。**

因此本客户端把「端点路径 / 认证头 / 请求体结构」全部做成了可配置项：

* ``api_path``           —— 请求路径，默认 ``/api/v1/download``（按需求文档）
* ``auth_header``        —— 认证头名称，默认 ``Authorization``
* ``auth_scheme``        —— 认证方案前缀，默认 ``Bearer``（留空则只发 token）
* ``payload_template``   —— 请求体 JSON 模板，用 ``{url}`` 占位

只要改配置就能适配不同的 GoPeed 版本，不需要改代码。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

try:  # AstrBot 环境优先使用宿主 logger
    from astrbot.api import logger
except Exception:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_quark_gopeed.gopeed")


#: 默认形态
#: ⚠️ 以下默认值已按 GoPeed 1.9.3 的**实测结果**校准：
#:     POST /api/v1/tasks   body: {"req": {"url": ...}, "opt": {...}}
#: 早期版本曾按需求文档使用 /api/v1/download + {"url": ...}：
#: 实测 /api/v1/download 返回 404，裸 {"url": ...} 被 GoPeed 以 code=1002 拒绝。
DEFAULT_API_PATH = "/api/v1/tasks"
DEFAULT_AUTH_HEADER = "Authorization"
DEFAULT_AUTH_SCHEME = "Bearer"
DEFAULT_PAYLOAD_TEMPLATE = (
    '{"req": {"url": "{url}", "extra": {"header": {headers}}},'
    ' "opt": {"name": "{name}", "path": "{path}"}}'
)

#: GoPeed 用 HTTP 200 承载业务错误码，这里为常见错误码给出针对性提示
CODE_HINTS = {
    1002: (
        "（GoPeed 要求请求体形如 {\"req\": {\"url\": ...}, \"opt\": {}}，"
        "请检查 gopeed_payload_template 配置）"
    ),
}


class GopeedError(Exception):
    """GoPeed 调用失败的通用异常。"""


class GopeedClient:
    """GoPeed HTTP API 异步客户端。

    Args:
        host: GoPeed 服务地址，如 ``127.0.0.1`` 或 NAS 的局域网 IP。
        port: GoPeed HTTP 端口，默认 ``9999``。
        token: 接口令牌。留空表示 GoPeed 未开启鉴权。
        api_path: 创建任务的路径。
        auth_header: 认证头名称。
        auth_scheme: 认证方案前缀。
        payload_template: 请求体模板，``{url}`` 会被替换成下载直链。
        use_ssl: 是否使用 https。
        timeout: 请求超时秒数。
    """

    def __init__(
        self,
        host: str,
        port: int | str = 9999,
        token: str = "",
        *,
        api_path: str = DEFAULT_API_PATH,
        auth_header: str = DEFAULT_AUTH_HEADER,
        auth_scheme: str = DEFAULT_AUTH_SCHEME,
        payload_template: str = DEFAULT_PAYLOAD_TEMPLATE,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self._host = str(host or "").strip() or "127.0.0.1"
        try:
            self._port = int(port)
        except (TypeError, ValueError):
            self._port = 9999
        self._token = (token or "").strip()
        self._api_path = "/" + str(api_path or DEFAULT_API_PATH).strip().lstrip("/")
        self._auth_header = str(auth_header or DEFAULT_AUTH_HEADER).strip()
        self._auth_scheme = str(auth_scheme or "").strip()
        self._payload_template = (
            str(payload_template or "").strip() or DEFAULT_PAYLOAD_TEMPLATE
        )
        self._scheme = "https" if use_ssl else "http"
        self._timeout = float(timeout)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "GopeedClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # 组装请求
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    @property
    def url(self) -> str:
        return f"{self.base_url}{self._api_path}"

    def build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        }
        if self._token:
            value = (
                f"{self._auth_scheme} {self._token}".strip()
                if self._auth_scheme
                else self._token
            )
            headers[self._auth_header] = value
        return headers

    def build_payload(
        self,
        download_url: str,
        name: str = "",
        path: str = "",
        headers: dict[str, str] | None = None,
    ) -> str:
        """按模板生成请求体。

        模板占位符：
        * ``{url}``     —— 下载直链（作为 JSON 字符串值，自动转义）
        * ``{name}``    —— 文件名（同上）
        * ``{path}``    —— 下载目录（同上）
        * ``{headers}`` —— 自定义请求头，替换成**完整的 JSON 对象**（不是字符串）

        ``{headers}`` 用于向 GoPeed 传递下载夸克直链所需的 Cookie / Referer / UA，
        实测缺少它们时夸克 CDN 会返回 412。
        """
        # json.dumps 会加上首尾引号并转义内部特殊字符，去掉引号即得到安全的内嵌值
        def esc(value: str) -> str:
            return json.dumps(value, ensure_ascii=False)[1:-1]

        rendered = (
            self._payload_template.replace("{url}", esc(download_url))
            .replace("{name}", esc(name))
            .replace("{path}", esc(path))
            .replace("{headers}", json.dumps(headers or {}, ensure_ascii=False))
        )
        try:
            json.loads(rendered)
        except ValueError as exc:
            raise GopeedError(
                f"gopeed_payload_template 不是合法 JSON 模板：{exc}"
            ) from exc
        return rendered

    # ------------------------------------------------------------------ #
    # 创建任务
    # ------------------------------------------------------------------ #
    async def create_task(
        self,
        download_url: str,
        name: str = "",
        path: str = "",
        headers: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        """创建下载任务。

        Args:
            download_url: 下载直链。
            name: 文件名，留空则由 GoPeed 自行推断。
            path: 下载目录（GoPeed 容器内路径），留空则用 GoPeed 默认目录。
            headers: 交给 GoPeed 在下载时使用的自定义请求头。

        Returns:
            ``(是否成功, 提示信息)`` —— 不抛异常，便于批量处理时逐个反馈。
        """
        if self._client is None:
            raise GopeedError("GopeedClient 未初始化，请使用 async with 语法")

        try:
            payload = self.build_payload(
                download_url, name=name, path=path, headers=headers
            )
        except GopeedError as exc:
            return False, str(exc)

        try:
            resp = await self._client.post(
                self.url,
                content=payload.encode("utf-8"),
                headers=self.build_headers(),
            )
        except httpx.ConnectError as exc:
            return False, (
                f"无法连接 GoPeed（{self.base_url}）：{exc}。"
                "请确认 GoPeed 已启动、端口正确，且 AstrBot 能访问到该地址"
            )
        except httpx.TimeoutException:
            return False, f"连接 GoPeed 超时（{self.base_url}）"
        except httpx.HTTPError as exc:
            return False, f"请求 GoPeed 失败：{exc}"

        body_preview = (resp.text or "").strip()[:300]

        if resp.status_code in (401, 403):
            return False, (
                f"GoPeed 认证失败（HTTP {resp.status_code}）。"
                "请检查 gopeed_token、gopeed_auth_header 与 gopeed_auth_scheme 配置"
            )
        if resp.status_code == 404:
            return False, (
                f"GoPeed 返回 404，接口路径可能不对（{self._api_path}）。"
                "请在配置中调整 gopeed_api_path"
            )

        if resp.status_code in (200, 201, 202, 204):
            # ⚠️ 关键：GoPeed 用 HTTP 200 承载业务错误码。
            # 实测 {"url": ...} 会返回 HTTP 200 + {"code":1002,"msg":"param invalid: rid or req"}。
            # 因此必须解析响应体，否则会把失败误报成成功。
            if not body_preview:
                logger.info("GoPeed 未返回响应体，按成功处理：HTTP %s", resp.status_code)
                return True, f"HTTP {resp.status_code}"

            try:
                payload_obj = json.loads(body_preview)
            except ValueError:
                logger.warning(
                    "GoPeed 返回非 JSON 响应（HTTP %s）：%s",
                    resp.status_code,
                    body_preview,
                )
                return True, body_preview

            if isinstance(payload_obj, dict) and "code" in payload_obj:
                code = payload_obj.get("code")
                if code == 0:
                    task_id = self._extract_task_id(payload_obj.get("data"))
                    logger.info("GoPeed 任务创建成功：task_id=%s", task_id or "未知")
                    return True, f"task_id={task_id}" if task_id else "ok"

                msg = (
                    payload_obj.get("msg")
                    or payload_obj.get("message")
                    or "未知错误"
                )
                hint = CODE_HINTS.get(code, "")
                logger.warning("GoPeed 拒绝任务：code=%s msg=%s", code, msg)
                return False, f"GoPeed 拒绝任务：code={code}, msg={msg}{hint}"

            logger.info("GoPeed 任务创建成功：HTTP %s", resp.status_code)
            return True, body_preview

        return False, f"GoPeed 返回 HTTP {resp.status_code}：{body_preview}"

    @staticmethod
    def _extract_task_id(data: Any) -> str | None:
        """从 GoPeed 的 data 字段里取出任务 ID。"""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            task_id = data.get("id")
            return str(task_id) if task_id else None
        return None

    async def ping(self) -> tuple[bool, str]:
        """连通性探测，用于排障。"""
        if self._client is None:
            raise GopeedError("GopeedClient 未初始化，请使用 async with 语法")
        try:
            resp = await self._client.get(self.base_url, headers=self.build_headers())
        except httpx.HTTPError as exc:
            return False, f"无法访问 {self.base_url}：{exc}"
        return True, f"HTTP {resp.status_code}"
