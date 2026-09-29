"""会话级状态：记住最近一次搜索结果，等待用户回复确认下载。

设计要点：
* **按会话隔离** —— 私聊按用户、群聊按「群+用户」，避免群里互相串结果；
* **带 TTL** —— 过期自动失效，不会一直留着；
* **纯内存** —— 插件重启即清空，这是可接受的（搜索结果本就不该长期保存）；
* **有上限** —— 防止长时间运行后字典无限膨胀。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PendingSearch:
    """一次等待用户确认的搜索结果。"""

    keyword: str
    results: list = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)

    def age(self) -> float:
        """距创建已过去的秒数。"""
        return time.monotonic() - self.created_at

    def is_expired(self, ttl: float) -> bool:
        return self.age() > ttl

    def pick(self, index: int) -> object | None:
        """按 1 起的序号取结果；越界返回 None。"""
        if index < 1 or index > len(self.results):
            return None
        return self.results[index - 1]


class SessionStore:
    """会话 -> 待确认搜索结果的映射。"""

    def __init__(self, ttl: float = 300.0, max_sessions: int = 500) -> None:
        self._ttl = float(ttl)
        self._max = max(1, int(max_sessions))
        self._data: dict[str, PendingSearch] = {}

    @property
    def ttl(self) -> float:
        return self._ttl

    def save(self, key: str, keyword: str, results: list) -> PendingSearch:
        """记录一次搜索结果，覆盖该会话之前的记录。"""
        self._cleanup()
        item = PendingSearch(keyword=keyword, results=list(results))
        self._data[key] = item
        return item

    def get(self, key: str) -> PendingSearch | None:
        """取出该会话待确认的搜索结果；已过期或不存在返回 None。"""
        item = self._data.get(key)
        if item is None:
            return None
        if item.is_expired(self._ttl):
            self._data.pop(key, None)
            return None
        return item

    def clear(self, key: str) -> None:
        self._data.pop(key, None)

    def clear_all(self) -> None:
        self._data.clear()

    def size(self) -> int:
        return len(self._data)

    def _cleanup(self) -> None:
        """清理过期项；超出上限时丢弃最旧的。"""
        now = time.monotonic()
        expired = [
            k for k, v in self._data.items() if now - v.created_at > self._ttl
        ]
        for k in expired:
            self._data.pop(k, None)

        if len(self._data) >= self._max:
            ordered = sorted(self._data.items(), key=lambda kv: kv[1].created_at)
            for k, _ in ordered[: len(self._data) - self._max + 1]:
                self._data.pop(k, None)
