"""搜索层的数据结构与抽象接口。

设计目标：把「搜索源」抽象成可替换的适配器，主流程只依赖这里的接口。
以后换站点或加站点，只需新增一个适配器文件，main.py 一行不用改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class DownloadLink:
    """一条搜索结果里可用的下载链接。"""

    url: str
    quality: str = ""

    @property
    def is_quark(self) -> bool:
        return "quark.cn" in self.url


@dataclass
class SearchResult:
    """一条搜索结果（列表页层面的信息，尚未取得真实链接）。"""

    detail_id: str
    title: str
    artist: str = ""
    album: str = ""
    cover: str = ""
    quality: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def has_quark(self) -> bool:
        """列表页信息里是否标了夸克网盘资源。"""
        if any("夸克" in q for q in self.quality):
            return True
        # 有些站点不标 quality，此时无法提前判断，保守地当作「可能有」
        return not self.quality

    @property
    def quality_text(self) -> str:
        return " / ".join(self.quality) if self.quality else "未知"

    def format_line(self, index: int) -> str:
        """格式化成给用户看的一行，例如：
        3. 稻香 — 周杰伦  [夸克MP3 / 夸克FLAC]
        """
        line = f"{index}. {self.title}"
        if self.artist:
            line += f" — {self.artist}"
        if self.quality:
            line += f"  [{self.quality_text}]"
        return line


class SearchError(Exception):
    """搜索或取链接失败。"""


class SearchSource(ABC):
    """搜索源适配器接口。

    子类只需实现两个方法，并在 ``search_engine.SOURCE_REGISTRY`` 里注册。
    """

    #: 内部标识，配置里用它来选择搜索源
    name: str = "unknown"
    #: 展示给用户的名字
    display_name: str = "未知来源"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    async def search(self, keyword: str, limit: int = 10) -> list[SearchResult]:
        """按关键词搜索，返回结果列表。"""

    @abstractmethod
    async def get_downloads(self, detail_id: str) -> list[DownloadLink]:
        """取某条结果对应的下载链接列表。"""
