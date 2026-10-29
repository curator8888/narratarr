"""A small time-limited cache that can't grow without bound (the app runs for months)."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, TypeVar

T = TypeVar("T")


class Cache(Generic[T]):
    def __init__(self, seconds: float, size: int = 500):
        self.seconds, self.size = seconds, size
        self._items: OrderedDict[object, tuple[float, T]] = OrderedDict()

    def get(self, key) -> T | None:
        hit = self._items.get(key)
        if hit is None:
            return None
        if time.monotonic() - hit[0] >= self.seconds:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return hit[1]

    def set(self, key, value: T) -> None:
        self._items[key] = (time.monotonic(), value)
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
