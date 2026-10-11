"""Task/result/handoff contracts for the agent hierarchy.

Every node in the CEO -> Manager -> Agent -> SubAgent tree speaks these
contracts. Plain dataclasses + dicts so they serialize cleanly onto the
SQLite blackboard (WAL mode) without new infrastructure.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# Hierarchy levels. Depth 0 is the CEO; depth 3 (SubAgent) is the max.
CEO = 0
MANAGER = 1
AGENT = 2
SUBAGENT = 3
MAX_DEPTH = SUBAGENT

LEVELS = {CEO: "ceo", MANAGER: "manager", AGENT: "agent", SUBAGENT: "subagent"}


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    HANDED_OFF = "handed_off"
    CANCELLED = "cancelled"


# Specialist taxonomy (chosen as the best-fit default; override per task).
#   browser          -> web automation via browser engines
#   pc_app           -> native Windows app automation (UIA)
#   linux_shell      -> WSL2 / Linux tasks
#   file_ops         -> file creation / manipulation
#   vision_grounding -> screenshot -> coordinates
SPECIALISTS = ("browser", "pc_app", "linux_shell", "file_ops", "vision_grounding")


def new_id(prefix: str = "task") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class TaskSpec:
    """One unit of work in the hierarchy."""

    id: str = field(default_factory=lambda: new_id("task"))
    title: str = ""
    description: str = ""
    level: int = AGENT          # level that should execute it
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    status: str = TaskStatus.PENDING
    specialist: Optional[str] = None
    assignee: Optional[str] = None   # node name currently owning it
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self, status: Optional[str] = None) -> "TaskSpec":
        self.updated_at = time.time()
        if status is not None:
            self.status = status
        return self

    def add_child(self, child_id: str) -> "TaskSpec":
        if child_id not in self.children_ids:
            self.children_ids.append(child_id)
        return self.touch()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TaskResult:
    """Completion record for a TaskSpec."""

    task_id: str = ""
    ok: bool = True
    summary: str = ""
    artifacts: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    by: Optional[str] = None        # node name that produced it
    finished_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class HandoffEnvelope:
    """Peer-to-peer task transfer at the same hierarchy level."""

    task_id: str = ""
    from_node: str = ""
    to_node: str = ""
    reason: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HandoffEnvelope":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
