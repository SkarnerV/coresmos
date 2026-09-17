"""Public exception types. Protocol and recording failures abort; ordinary tool errors do not."""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base error for the public runtime."""


class ProtocolError(AgentRuntimeError):
    """A step stream or loop invariant was violated."""


class CompletionProtocolError(ProtocolError):
    """A step stream did not end with exactly one completion event."""


class RecordingError(AgentRuntimeError):
    """A required transcript or state commit failed."""


class IdempotencyError(RecordingError):
    """A logical operation id was reused with a conflicting payload."""


class MessageNotFoundError(RecordingError):
    """An update referenced a message id that does not exist."""


class ReceiptMismatchError(RecordingError):
    """previous_receipt did not match the committed operation."""


class BudgetExhaustedError(AgentRuntimeError):
    """A run or request budget was exhausted."""


class AdapterError(AgentRuntimeError):
    """A low-level model or tool adapter failed."""


class CapabilityError(AgentRuntimeError):
    """Capability selection or binding failed in a non-NoMatch way."""


class SchemaValidationError(AgentRuntimeError):
    """A tool schema or call arguments failed validation."""


class ContextBudgetError(AgentRuntimeError):
    """Required context exceeded the token budget or could not be estimated."""


class VersionConflictError(AgentRuntimeError):
    """Snapshot versions could not be reconciled without replaying work."""


class CleanupTimeoutError(AgentRuntimeError):
    """Run cleanup exceeded its bounded wait; resources are not reported released."""


class CancelledRunError(AgentRuntimeError):
    """The run stopped because of user stop, host cancel, consumer close, or deadline."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
