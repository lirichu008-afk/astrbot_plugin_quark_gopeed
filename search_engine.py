"""搜索编排层：按配置构造搜索源、聚合结果、取出下载链接。

主流程只依赖本模块，不直接接触具体站点，因此新增站点时
只需写一个适配器并在 SOURCE_REGISTRY 里注册。
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:  # AstrBot 环境优先使用宿主 logger
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_quark_gopeed.search")

try:
    from .search_base import DownloadLink, SearchError, SearchResult, SearchSource
    from .search_quanpansou import QuanPanSouSource
except ImportError:  # pragma: no cover
    from search_base import (  # type: ignore[no-redef]
        DownloadLink,
        SearchError,
        SearchResult,
        SearchSource,
    )
    from search_quanpansou import QuanPanSouSource  # type: ignore[no-redef]


#: 搜索源注册表：配置里写的名字 -> 适配器类
SOURCE_REGISTRY: dict[str, type[SearchSource]] = {
    QuanPanSouSource.name: QuanPanSouSource,
}


def available_sources() -> list[str]:
    """返回所有已注册的搜索源名字。"""
    return sorted(SOURCE_REGISTRY)


@dataclass
class SearchOutcome:
    """一次搜索的完整结果，供上层组装回复文案。"""

    keyword: str
    results: list[SearchResult] = field(default_factory=list)
    #: 搜索源返回的原始条数（过滤前）
    total_found: int = 0
    #: 因为没有夸克资源而被过滤掉的条数
    skipped_no_quark: int = 0
    #: 各搜索源的错误信息
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results)


class SearchEngine:
    """负责「搜索」与「取下载链接」两件事。"""

    def __init__(
        self,
        sources: list[SearchSource],
        *,
        only_quark: bool = True,
        max_results: int = 10,
    ) -> None:
        self._sources = list(sources)
        self._only_quark = bool(only_quark)
        self._max_results = max(1, int(max_results))

    @property
    def sources(self) -> list[SearchSource]:
        return list(self._sources)

    def _find_source(self, name: str) -> SearchSource | None:
        for src in self._sources:
            if src.name == name:
                return src
        return None

    async def search(self, keyword: str) -> SearchOutcome:
        """在所有已配置的搜索源上检索关键词。"""
        outcome = SearchOutcome(keyword=keyword)
        collected: list[SearchResult] = []

        for src in self._sources:
            try:
                # 多取一些，过滤掉非夸克结果后才够填满 max_results
                items = await src.search(keyword, limit=self._max_results)
            except SearchError as exc:
                logger.warning("搜索源 %s 出错：%s", src.display_name, exc)
                outcome.errors.append(f"{src.display_name}：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 单源异常不应拖垮整体
                logger.exception("搜索源 %s 出现未预期错误", src.display_name)
                outcome.errors.append(f"{src.display_name}：{exc}")
                continue

            outcome.total_found += len(items)
            collected.extend(items)

        # 去重：同一站点可能返回重复 detail_id
        seen: set[tuple[str, str]] = set()
        unique: list[SearchResult] = []
        for item in collected:
            key = (item.source, item.detail_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        # 按需过滤掉明显没有夸克资源的条目
        if self._only_quark:
            kept = [r for r in unique if r.has_quark]
            outcome.skipped_no_quark = len(unique) - len(kept)
            unique = kept

        outcome.results = unique[: self._max_results]
        return outcome

    async def get_downloads(self, source_name: str, detail_id: str) -> list[DownloadLink]:
        """取某条结果对应的下载链接。"""
        src = self._find_source(source_name)
        if src is None:
            raise SearchError(f"找不到搜索源 {source_name}")
        return await src.get_downloads(detail_id)


def create_sources(
    names: list[str], source_config: dict | None = None
) -> list[SearchSource]:
    """按配置里的名字列表构造搜索源实例。

    Args:
        names: 例如 ``["quanpansou"]``；为空则使用全部已注册源。
        source_config: 传给适配器的配置字典。
    """
    wanted = [str(n).strip().lower() for n in (names or []) if str(n).strip()]
    if not wanted:
        wanted = available_sources()

    sources: list[SearchSource] = []
    for key in wanted:
        cls = SOURCE_REGISTRY.get(key)
        if cls is None:
            logger.warning(
                "未知的搜索源 %r，已跳过（可用：%s）", key, ", ".join(available_sources())
            )
            continue
        sources.append(cls(dict(source_config or {})))
    return sources
