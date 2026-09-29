"""「全盘搜」搜索源适配器。

站点：https://so.liumingye.cn/ —— 免费网盘资源搜索引擎

实测接口（2026-09-27 在真实站点验证）::

    搜索  GET /api/music/search?q=<关键词>&pageSize=<n>
          -> {"data":[{"id","title","artist","album","cover","quality":[...]}],
              "total":..,"page":..,"pageSize":..,"totalPages":..,"tokens":[..]}

    详情  GET /api/music/<id>
          -> {"id","title","artist","album","cover","lyrics","playUrl",
              "downloads":[{"url":"https://pan.quark.cn/s/xxx","quality":"夸克FLAC"}]}

实测注意事项：
* 该站搜索是**模糊匹配**，输入毫不相关的词也会返回一堆结果，
  因此不能以「有返回」判断命中，需要用户自己看标题；
* ``pageSize`` 有效（上限未测），``page`` 实测无效（始终返回首屏）；
* ``q`` 为空或缺失时返回空数组，不是报错；
* 返回的 ``downloads[].url`` 就是夸克**分享链接**，可直接交给本插件的下载链路。
"""

from __future__ import annotations

import asyncio

import httpx

try:  # AstrBot 环境优先使用宿主 logger
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_quark_gopeed.search")

try:
    from .search_base import (
        DownloadLink,
        SearchError,
        SearchResult,
        SearchSource,
    )
except ImportError:  # pragma: no cover
    from search_base import (  # type: ignore[no-redef]
        DownloadLink,
        SearchError,
        SearchResult,
        SearchSource,
    )


DEFAULT_BASE_URL = "https://so.liumingye.cn"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
SEARCH_PATH = "/api/music/search"
DETAIL_PATH = "/api/music/{detail_id}"


class QuanPanSouSource(SearchSource):
    """「全盘搜」适配器。"""

    name = "quanpansou"
    display_name = "全盘搜"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._base = str(self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = float(self.config.get("timeout") or 20.0)
        self._ua = str(self.config.get("user_agent") or DEFAULT_UA)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @property
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{self._base}/",
        }

    async def _get_json(
        self, path: str, params: dict | None = None, *, retries: int = 2
    ) -> dict:
        """请求接口并返回 JSON。

        带重试：实测该站偶发超时（详情接口平时约 1.3 秒，
        但短时间内连续请求时会挂起），因此超时与服务端 5xx 都重试。
        """
        url = f"{self._base}{path}"
        last_error = ""

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout), follow_redirects=True
                ) as client:
                    resp = await client.get(url, params=params, headers=self._headers)
            except httpx.TimeoutException:
                last_error = f"请求 {self.display_name} 超时"
            except httpx.HTTPError as exc:
                last_error = f"请求 {self.display_name} 失败：{exc}"
            else:
                # 4xx 属于请求本身的问题，重试没有意义
                if 400 <= resp.status_code < 500:
                    raise SearchError(
                        f"{self.display_name} 返回 HTTP {resp.status_code}：{resp.text[:150]}"
                    )
                if resp.status_code != 200:
                    last_error = (
                        f"{self.display_name} 返回 HTTP {resp.status_code}：{resp.text[:150]}"
                    )
                else:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise SearchError(
                            f"{self.display_name} 返回的不是 JSON（站点可能已改版）："
                            f"{resp.text[:150]}"
                        ) from exc
                    if not isinstance(data, dict):
                        raise SearchError(f"{self.display_name} 返回格式异常")
                    return data

            if attempt < retries:
                wait = 1.0 * (attempt + 1)
                logger.warning(
                    "%s 第 %d 次请求失败，%.1fs 后重试：%s",
                    self.display_name,
                    attempt + 1,
                    wait,
                    last_error,
                )
                await asyncio.sleep(wait)

        raise SearchError(f"{last_error}（已重试 {retries} 次）")

    # ------------------------------------------------------------------ #
    # 接口实现
    # ------------------------------------------------------------------ #
    async def search(self, keyword: str, limit: int = 10) -> list[SearchResult]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        # 该站 pageSize 有效；多取一些以便过滤掉非夸克结果后仍够用
        want = max(1, min(int(limit) * 3, 60))
        data = await self._get_json(SEARCH_PATH, {"q": keyword, "pageSize": want})

        raw_items = data.get("data")
        if not isinstance(raw_items, list):
            raise SearchError(f"{self.display_name} 返回结构里没有 data 列表")

        results: list[SearchResult] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            detail_id = str(item.get("id") or "").strip()
            if not detail_id:
                continue
            quality = item.get("quality")
            results.append(
                SearchResult(
                    detail_id=detail_id,
                    title=str(item.get("title") or "").strip() or "(无标题)",
                    artist=str(item.get("artist") or "").strip(),
                    album=str(item.get("album") or "").strip(),
                    cover=str(item.get("cover") or "").strip(),
                    quality=[str(q) for q in quality] if isinstance(quality, list) else [],
                    source=self.name,
                )
            )

        logger.info(
            "%s 搜索「%s」：原始 %d 条（total=%s）",
            self.display_name,
            keyword,
            len(results),
            data.get("total"),
        )
        return results

    async def get_downloads(self, detail_id: str) -> list[DownloadLink]:
        detail_id = (detail_id or "").strip()
        if not detail_id:
            raise SearchError("缺少详情 ID")

        data = await self._get_json(DETAIL_PATH.format(detail_id=detail_id))
        raw = data.get("downloads")
        if not isinstance(raw, list):
            return []

        links: list[DownloadLink] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            links.append(
                DownloadLink(url=url, quality=str(item.get("quality") or "").strip())
            )
        return links
