"""Peer handoff: transfer a task + context between same-level nodes.

Handoffs are lateral (Agent -> Agent, Manager -> Manager). Vertical
movement is delegation (parent -> child) or escalation (child -> parent),
handled by the node classes, not here.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from rehan.hierarchy.contracts import (
    HandoffEnvelope, TaskSpec, TaskStatus,
)


class HandoffError(Exception):
    """Raised when a handoff is invalid."""


def validate_handoff(from_level: int, to_level: int) -> None:
    """Peers only: levels must match."""
    if from_level != to_level:
        raise HandoffError(
            f"handoff requires same level, got {from_level} -> {to_level}")


def perform_handoff(task: TaskSpec, from_node: str, to_node: str,
                    from_level: int, to_level: int,
                    reason: str = "",
                    context: Optional[Dict[str, Any]] = None,
                    on_transfer: Optional[Callable[[HandoffEnvelope], None]] = None,
                    ) -> HandoffEnvelope:
    """Move ``task`` from one peer node to another.

    Mutates the task: status -> HANDED_OFF, assignee -> to_node.
    Returns the envelope; optionally notifies via ``on_transfer``.
    """
    validate_handoff(from_level, to_level)
    if not task.id:
        raise HandoffError("task has no id")
    if from_node == to_node:
        raise HandoffError("cannot hand off to self")

    envelope = HandoffEnvelope(
        task_id=task.id, from_node=from_node, to_node=to_node,
        reason=reason, context=dict(context or {}))
    task.assignee = to_node
    task.touch(TaskStatus.HANDED_OFF)
    if on_transfer is not None:
        on_transfer(envelope)
    return envelope
