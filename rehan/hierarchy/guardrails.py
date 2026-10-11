"""Guardrails for the agent hierarchy.

Rehan's decision: ScopeGuard is RELAXED — actions are allowed, everything
is logged. No blocking prompts in the autonomous path. The audit log is
the safety net, not a gate.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AuditEntry:
    ts: float
    node: str
    action: str
    target: str
    allowed: bool
    detail: str = ""


class ScopeGuard:
    """Relaxed scope guard: log every action, allow it.

    Rehan: "no it is allowed always". Destructive patterns (delete,
    overwrite, kill-process) are flagged in the log with ``risky=True``
    so a reviewer can audit later, but they are NOT blocked.
    """

    RISKY_KEYWORDS = ("delete", "remove", "rm ", "del ", "kill",
                      "format", "overwrite", "drop table", "shutdown")

    def __init__(self, node_name: str = "?"):
        self.node_name = node_name
        self._lock = threading.Lock()
        self.audit: List[AuditEntry] = []
        self.risky_count = 0

    def _is_risky(self, action: str, target: str) -> bool:
        blob = f"{action} {target}".lower()
        return any(k in blob for k in self.RISKY_KEYWORDS)

    def check(self, action: str, target: str = "",
              detail: str = "") -> bool:
        """Always returns True (allowed); records the audit entry."""
        risky = self._is_risky(action, target)
        entry = AuditEntry(ts=time.time(), node=self.node_name,
                           action=action, target=target, allowed=True,
                           detail=("RISKY " if risky else "") + detail)
        with self._lock:
            self.audit.append(entry)
            if risky:
                self.risky_count += 1
        return True

    def risky_entries(self) -> List[AuditEntry]:
        with self._lock:
            return [e for e in self.audit if e.detail.startswith("RISKY")]

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {"node": self.node_name, "actions": len(self.audit),
                    "risky": self.risky_count}


class ResourceGuard:
    """Per-node resource budget. Soft caps: warn + record, don't kill.

    Caps are advisory so a single misbehaving node can't silently wedge
    the pool, while the watchdog (workers.py) remains the hard enforcer.
    """

    def __init__(self, node_name: str = "?", max_tasks: int = 32,
                 max_children: int = 8):
        self.node_name = node_name
        self.max_tasks = max_tasks
        self.max_children = max_children
        self._lock = threading.Lock()
        self.tasks_seen = 0
        self.children_spawned = 0
        self.violations: List[str] = []

    def note_task(self) -> bool:
        """True if within budget, False if over (still allowed)."""
        with self._lock:
            self.tasks_seen += 1
            over = self.tasks_seen > self.max_tasks
            if over:
                self.violations.append(
                    f"task budget exceeded: {self.tasks_seen}>{self.max_tasks}")
            return not over

    def note_child(self) -> bool:
        with self._lock:
            self.children_spawned += 1
            over = self.children_spawned > self.max_children
            if over:
                self.violations.append(
                    f"child budget exceeded: {self.children_spawned}>{self.max_children}")
            return not over

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {"node": self.node_name, "tasks_seen": self.tasks_seen,
                    "children_spawned": self.children_spawned,
                    "violations": list(self.violations)}
