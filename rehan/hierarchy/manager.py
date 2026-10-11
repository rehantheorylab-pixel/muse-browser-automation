"""Manager (level 1): decomposes a CEO goal-slice into agent tasks."""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from rehan.hierarchy.agent import OrgAgent
from rehan.hierarchy.contracts import (
    AGENT, MANAGER, TaskResult, TaskSpec, TaskStatus, new_id,
)
from rehan.hierarchy.guardrails import ResourceGuard, ScopeGuard


class ManagerError(Exception):
    """Manager-level failure."""


class Manager:
    """Level-1 coordinator. Owns a team of OrgAgents.

    Receives a TaskSpec from the CEO, decomposes it into level-2 agent
    tasks (via an injected ``decompose`` callable — mocked in tests),
    assigns them to agents, and synthesizes their results.
    """

    LEVEL = MANAGER

    def __init__(self, name: Optional[str] = None,
                 ceo: Optional[Any] = None,
                 decompose: Optional[Callable[[TaskSpec], List[TaskSpec]]] = None,
                 synthesize: Optional[Callable[[TaskSpec, List[TaskResult]], TaskResult]] = None):
        self.name = name or f"manager-{new_id('m')[-6:]}"
        self.level = self.LEVEL
        self.ceo = ceo
        self.decompose = decompose
        self.synthesize = synthesize
        self.guard = ScopeGuard(self.name)
        self.resources = ResourceGuard(self.name)
        self.agents: List[OrgAgent] = []
        self.tasks: Dict[str, TaskSpec] = {}
        self.results: Dict[str, TaskResult] = {}
        self._lock = threading.Lock()
        self.peers: List["Manager"] = []

    # -- team ----------------------------------------------------------
    def hire(self, agent: OrgAgent) -> OrgAgent:
        """Attach an agent to this manager's team."""
        self.guard.check("hire_agent", agent.name,
                         f"specialist={agent.specialist}")
        self.resources.note_child()
        with self._lock:
            if agent not in self.agents:
                self.agents.append(agent)
        return agent

    def spawn_agent(self, name: Optional[str] = None,
                    specialist: Optional[str] = None,
                    **kwargs) -> OrgAgent:
        """Create + start a fresh OrgAgent on its own display."""
        agent = OrgAgent(name=name, specialist=specialist, **kwargs)
        agent.start()
        return self.hire(agent)

    # -- work -----------------------------------------------------------
    def accept(self, task: TaskSpec) -> TaskSpec:
        self.guard.check("accept_task", task.id, task.title)
        self.resources.note_task()
        task.assignee = self.name
        task.touch(TaskStatus.RUNNING)
        with self._lock:
            self.tasks[task.id] = task
        return task

    def _pick_agent(self, task: TaskSpec) -> OrgAgent:
        """Specialist match first, else least-loaded agent."""
        with self._lock:
            agents = list(self.agents)
        if not agents:
            raise ManagerError(f"{self.name}: no agents on team")
        if task.specialist:
            for a in agents:
                if a.specialist == task.specialist:
                    return a
        return min(agents, key=lambda a: len(a.tasks))

    def run(self, task: TaskSpec) -> TaskResult:
        """Decompose -> delegate -> synthesize. The manager loop."""
        task = self.accept(task)
        if self.decompose is None:
            # Default: single agent task mirroring the manager task.
            subtasks = [TaskSpec(title=task.title,
                                 description=task.description,
                                 level=AGENT, parent_id=task.id,
                                 specialist=task.specialist,
                                 payload=dict(task.payload))]
        else:
            subtasks = self.decompose(task) or []
        for st in subtasks:
            st.parent_id = task.id
            task.add_child(st.id)

        results: List[TaskResult] = []
        for st in subtasks:
            agent = self._pick_agent(st)
            self.guard.check("delegate", st.id,
                             f"to={agent.name} specialist={st.specialist}")
            results.append(agent.execute(st))

        if self.synthesize is not None:
            final = self.synthesize(task, results)
        else:
            ok = all(r.ok for r in results)
            final = TaskResult(
                task_id=task.id, ok=ok,
                summary=f"{sum(r.ok for r in results)}/{len(results)} agent tasks ok",
                artifacts={"subtasks": [r.to_dict() for r in results]},
                by=self.name)
        with self._lock:
            self.results[task.id] = final
        task.touch(TaskStatus.DONE if final.ok else TaskStatus.FAILED)
        return final

    # -- peer handoff ----------------------------------------------------
    def add_peer(self, peer: "Manager") -> None:
        if peer is not self and peer not in self.peers:
            self.peers.append(peer)

    def handoff(self, task: TaskSpec, peer: "Manager",
                reason: str = ""):
        """Hand a task to a same-level peer manager."""
        from rehan.hierarchy.handoff import HandoffError, perform_handoff
        if peer.level != self.level:
            raise HandoffError(
                f"peer level mismatch: {self.level} vs {peer.level}")
        self.guard.check("handoff", task.id, f"to={peer.name} {reason}")
        envelope = perform_handoff(task, self.name, peer.name,
                                   self.level, peer.level, reason=reason)
        with self._lock:
            self.tasks.pop(task.id, None)
        return peer, envelope

    def status(self) -> Dict[str, Any]:
        with self._lock:
            tasks = {tid: t.status for tid, t in self.tasks.items()}
            team = [a.name for a in self.agents]
        return {"name": self.name, "level": self.level, "team": team,
                "tasks": tasks, "guard": self.guard.summary()}
