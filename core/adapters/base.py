"""core/adapters/base.py — Abstract Base Adapter for all execution backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Imported only for annotations (breaks a core.adapters.base <->
    # core.fetch.engine import cycle at runtime).
    from core.fetch.normalizer import FetchResult


class BaseToolAdapter(ABC):
    """Abstract interface for all fetch, browser, and utility execution adapters."""

    name: str

    @abstractmethod
    async def fetch(self, url: str, options: Optional[Dict[str, Any]] = None) -> FetchResult:
        """Fetch URL content and return normalized FetchResult."""
        pass

    async def execute_task(self, task_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Optional task execution handler for specialized actions (downloads, etc.)."""
        raise NotImplementedError(f"Task '{task_type}' not implemented on {self.name}")
