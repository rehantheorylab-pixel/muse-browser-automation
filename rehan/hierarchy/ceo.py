"""CEO (level 0): takes a high-level goal, fans out to managers."""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from rehan.hierarchy.contracts import (
    CEO, MANAGER, TaskResult, TaskSpec, TaskStatus, new_id,
)
from rehan.hierarchy.guardrails import ResourceGuard, ScopeGuard
from rehan.hierarchy.manager import Manager


class CEOError(Exception):
    """CEO-level failure."""


class CEO:
    """Level-0 root. Decomposes a goal into manager slices, monitors."""

    LEVEL = CEO

    def __init__(self, name: Optional[str] = None,
                 decompose: Optional[Callable[[TaskSpec], List[TaskSpec]]] = None,
                 synthesize: Optional[Callable[[TaskSpec, List[TaskResult]], TaskResult]] = None):
        self.name = name or f"ceo-{new_id('c')[-6:]}"
        self.level = self.LEVEL
        self.decompose = decompose
        self.synthesize = synthesize
        self.guard = ScopeGuard(self.name)
        self.resources = ResourceGuard(self.name)
        self.managers: List[Manager] = []
        self.goals: Dict[str, TaskSpec] = {}
        self.results: Dict[str, TaskResult] = {}
        self._lock = threading.Lock()

    # -- org -------------------------------------------------------------
    def hire(self, manager: Manager) -> Manager:
        """Attach a manager. Sets the back-link ceo -> manager."""
        self.guard.check("hire_manager", manager.name)
        self.resources.note_child()
        manager.ceo = self
        with self._lock:
            if manager not in self.managers:
                self.managers.append(manager)
        return manager

    def spawn_manager(self, name: Optional[str] = None,
                      **kwargs) -> Manager:
        manager = Manager(name=name, ceo=self, **kwargs)
        return self.hire(manager)

    def _pick_manager(self, task: TaskSpec) -> Manager:
        with self._lock:
            managers = list(self.managers)
        if not managers:
            raise CEOError(f"{self.name}: no managers hired")
        return min(managers, key=lambda m: len(m.tasks))

    # -- the CEO loop ------------------------------------------------------
    def submit_goal(self, title: str, description: str = "",
                    payload: Optional[Dict[str, Any]] = None) -> TaskResult:
        """Decompose a goal -> delegate to managers -> synthesize."""
        goal = TaskSpec(title=title, description=description, level=CEO,
                        payload=dict(payload or {}))
        goal.touch(TaskStatus.RUNNING)
        self.guard.check("submit_goal", goal.id, title)
        with self._lock:
            self.goals[goal.id] = goal

        if self.decompose is None:
            slices = [TaskSpec(title=title, description=description,
                               level=MANAGER, parent_id=goal.id,
                               payload=dict(payload or {}))]
        else:
            slices = self.decompose(goal) or []
        for s in slices:
            s.parent_id = goal.id
            goal.add_child(s.id)

        results: List[TaskResult] = []
        for s in slices:
            manager = self._pick_manager(s)
            self.guard.check("delegate", s.id, f"to={manager.name}")
            results.append(manager.run(s))

        if self.synthesize is not None:
            final = self.synthesize(goal, results)
        else:
            ok = all(r.ok for r in results)
            final = TaskResult(
                task_id=goal.id, ok=ok,
                summary=f"{sum(r.ok for r in results)}/{len(results)} manager slices ok",
                artifacts={"slices": [r.to_dict() for r in results]},
                by=self.name)
        with self._lock:
            self.results[goal.id] = final
        goal.touch(TaskStatus.DONE if final.ok else TaskStatus.FAILED)
        return final

    def status(self) -> Dict[str, Any]:
        with self._lock:
            goals = {gid: g.status for gid, g in self.goals.items()}
            team = [m.name for m in self.managers]
        tree = {}
        for m in self.managers:
            tree[m.name] = {
                "agents": [a.name for a in m.agents],
                "subagents": {a.name: [c.name for c in a.org_children()]
                              for a in m.agents},
            }
        return {"name": self.name, "level": self.level,
                "managers": team, "goals": goals, "tree": tree,
                "guard": self.guard.summary()}
