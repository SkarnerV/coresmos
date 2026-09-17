"""Default control, error, and partial-output policies shared by later modules."""

from __future__ import annotations

from collections.abc import Sequence

from agent_runtime.contracts import (
    DEFAULT_ORDINARY_TOOL_ERROR_POLICY,
    DEFAULT_PARTIAL_OUTPUT_POLICY,
    FLOW_PRIORITY,
    Flow,
    FlowDecision,
    OrdinaryToolErrorPolicy,
    PartialOutputPolicy,
    PendingRef,
    ToolResult,
)

__all__ = [
    "DEFAULT_ORDINARY_TOOL_ERROR_POLICY",
    "DEFAULT_PARTIAL_OUTPUT_POLICY",
    "FLOW_PRIORITY",
    "aggregate_flow_decisions",
    "model_visible_tool_error",
]


def aggregate_flow_decisions(decisions: Sequence[FlowDecision]) -> FlowDecision:
    """WAIT outranks FINISH, which outranks CONTINUE. All decisions are considered.

    Side effects must already have been applied in order; this function only
    chooses the control result for the batch.
    """
    by_flow = {decision.flow: decision for decision in decisions}
    for flow in FLOW_PRIORITY:
        if flow in by_flow:
            return by_flow[flow]
    return FlowDecision(flow=Flow.CONTINUE, reason="default")


def model_visible_tool_error(result: ToolResult) -> ToolResult:
    """Ordinary invoker failures stay visible to the model and do not abort the batch."""
    if DEFAULT_ORDINARY_TOOL_ERROR_POLICY is OrdinaryToolErrorPolicy.MODEL_VISIBLE:
        return result
    return result


def wait_decision(pending_ref: PendingRef, reason: str = "wait") -> FlowDecision:
    if not pending_ref.value:
        raise ValueError("WAIT requires a pending_ref.value")
    return FlowDecision(flow=Flow.WAIT, reason=reason, pending_ref=pending_ref)


def isolate_attempts() -> bool:
    return DEFAULT_PARTIAL_OUTPUT_POLICY is PartialOutputPolicy.ISOLATE_ATTEMPTS
