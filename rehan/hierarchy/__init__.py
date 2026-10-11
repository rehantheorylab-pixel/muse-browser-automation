"""Hierarchical AI agent organization: CEO -> Manager -> Agent -> SubAgent.

Four levels, each talking only to adjacent levels. Peers at the same
level may hand off tasks to each other. Display isolation (virtual
monitor -> virtual desktop) is inherited from
:mod:`rehan.agent_hierarchy`.

Levels: 0=CEO, 1=MANAGER, 2=AGENT, 3=SUBAGENT (max depth).
"""

from rehan.hierarchy.contracts import (
    HandoffEnvelope,
    TaskResult,
    TaskSpec,
    TaskStatus,
    LEVELS,
    CEO,
    MANAGER,
    AGENT,
    SUBAGENT,
    MAX_DEPTH,
)
from rehan.hierarchy.guardrails import ResourceGuard, ScopeGuard
from rehan.hierarchy.handoff import HandoffError, perform_handoff
from rehan.hierarchy.agent import AgentDepthError, OrgAgent, SubAgent
from rehan.hierarchy.manager import Manager, ManagerError
from rehan.hierarchy.ceo import CEO as CEOAgent, CEOError

__all__ = [
    "HandoffEnvelope",
    "TaskResult",
    "TaskSpec",
    "TaskStatus",
    "LEVELS",
    "CEO",
    "MANAGER",
    "AGENT",
    "SUBAGENT",
    "MAX_DEPTH",
    "ResourceGuard",
    "ScopeGuard",
    "HandoffError",
    "perform_handoff",
    "AgentDepthError",
    "OrgAgent",
    "SubAgent",
    "Manager",
    "ManagerError",
    "CEOAgent",
    "CEOError",
]
