"""OrgAgent (level 2) and SubAgent (level 3).

OrgAgent extends :class:`rehan.agent_hierarchy.Agent` (virtual monitor ->
virtual desktop display isolation, optional WSL2) with org behavior:
task execution, sub-agent spawning (max depth enforced), and peer
handoff. SubAgent is an OrgAgent pinned at level 3 that cannot spawn.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from rehan.agent_hierarchy import Agent as DisplayAgent
from rehan.hierarchy.contracts import (
    AGENT, MAX_DEPTH, SUBAGENT, HandoffEnvelope,
    TaskResult, TaskSpec, TaskStatus,
)
from rehan.hierarchy.guardrails import ResourceGuard, ScopeGuard
from rehan.hierarchy.handoff import perform_handoff


class OrgAgent(DisplayAgent):
    """An executing agent in the org tree (level 2 by default)."""

    LEVEL = AGENT

    def __init__(self, name: Optional[str] = None,
                 parent: Optional["OrgAgent"] = None,
                 specialist: Optional[str] = None,
                 executor: Optional[Callable[[TaskSpec], TaskResult]] = None,
                 **kwargs):
        super().__init__(name=name, parent=parent, **kwargs)
        self.level = self.LEVEL
        self.specialist = specialist
        # executor: TaskSpec -> TaskResult. Injected; mocked in tests
        # (proxy offline). Production wires the real run loop here.
        self.executor = executor
        self.guard = ScopeGuard(self.name)
        self.resources = ResourceGuard(self.name)
        self.tasks: Dict[str, TaskSpec] = {}
        self.results: Dict[str, TaskResult] = {}
        self._task_lock = threading.Lock()
        self.peers: List["OrgAgent"] = []

    # -- org tree ------------------------------------------------------
    @property
    def org_parent(self) -> Optional["OrgAgent"]:
        p = self.parent
        return p if isinstance(p, OrgAgent) else None

    def org_children(self) -> List["OrgAgent"]:
        return [c for c in self.children if isinstance(c, OrgAgent)]

    # -- tasks ----------------------------------------------------------
    def accept(self, task: TaskSpec) -> TaskSpec:
        """Take ownership of a delegated task."""
        self.guard.check("accept_task", task.id, task.title)
        self.resources.note_task()
        task.assignee = self.name
        task.touch(TaskStatus.RUNNING)
        with self._task_lock:
            self.tasks[task.id] = task
        return task

    def execute(self, task: TaskSpec) -> TaskResult:
        """Run a task through the injected executor (mock in tests)."""
        task = self.accept(task)
        if self.executor is None:
            result = TaskResult(task_id=task.id, ok=True,
                                summary=f"(no executor) accepted: {task.title}",
                                by=self.name)
        else:
            try:
                result = self.executor(task)
                result.by = self.name
            except Exception as exc:  # never lose the task silently
                result = TaskResult(task_id=task.id, ok=False,
                                    summary=f"executor raised: {exc}",
                                    error=str(exc), by=self.name)
        with self._task_lock:
            self.results[task.id] = result
        task.touch(TaskStatus.DONE if result.ok else TaskStatus.FAILED)
        self.guard.check("complete_task", task.id,
                         f"ok={result.ok} {result.summary[:80]}")
        return result

    # -- spawning (Agent -> SubAgent only) -------------------------------
    def spawn_subagent(self, name: Optional[str] = None,
                       specialist: Optional[str] = None,
                       **kwargs) -> "SubAgent":
        """Create a level-3 sub-agent on its own display.

        Raises AgentDepthError past MAX_DEPTH. The child is a SubAgent
        (org-aware), not a plain display Agent: it is constructed
        directly so exactly one display is claimed per child.
        """
        if self.level >= MAX_DEPTH:
            raise AgentDepthError(
                f"{self.name}: cannot spawn below level {MAX_DEPTH}")
        self.resources.note_child()
        # Inherit display plumbing (pool preference, wsl factory) so the
        # child lands on its own virtual monitor / desktop + optional Linux.
        kwargs.setdefault("monitor_pool", self._monitor_pool)
        kwargs.setdefault("prefer_monitor", self._prefer_monitor)
        kwargs.setdefault("wsl_factory", self._wsl_factory)
        child = SubAgent(name=name, parent=self, specialist=specialist,
                         executor=self.executor, **kwargs)
        child.start()  # claims its own display (monitor -> desktop -> shim)
        return child

    # -- peer handoff ----------------------------------------------------
    def add_peer(self, peer: "OrgAgent") -> None:
        if peer is not self and peer not in self.peers:
            self.peers.append(peer)

    def handoff(self, task: TaskSpec, peer: "OrgAgent",
                reason: str = "") -> HandoffEnvelope:
        """Hand a task to a same-level peer."""
        if peer.level != self.level:
            from rehan.hierarchy.handoff import HandoffError
            raise HandoffError(
                f"peer level mismatch: {self.level} vs {peer.level}")
        self.guard.check("handoff", task.id, f"to={peer.name} {reason}")
        envelope = perform_handoff(
            task, self.name, peer.name, self.level, peer.level,
            reason=reason, context={"specialist": task.specialist})
        with self._task_lock:
            self.tasks.pop(task.id, None)
        peer.accept(task)
        return envelope

    def status(self) -> Dict[str, Any]:
        with self._task_lock:
            tasks = {tid: t.status for tid, t in self.tasks.items()}
        return {"name": self.name, "level": self.level,
                "specialist": self.specialist,
                "display": self.display_backend,
                "children": [c.name for c in self.org_children()],
                "tasks": tasks,
                "guard": self.guard.summary()}


class AgentDepthError(Exception):
    """Raised when spawning would exceed MAX_DEPTH."""


class SubAgent(OrgAgent):
    """Level-3 leaf: executes, never spawns."""

    LEVEL = SUBAGENT

    def spawn_subagent(self, *args, **kwargs) -> "SubAgent":  # noqa: D102
        raise AgentDepthError(
            f"{self.name}: SubAgent (level {SUBAGENT}) cannot spawn children")
