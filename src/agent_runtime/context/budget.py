"""Token budget policy. Default estimator is documented character/4; unknown types fail closed."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

from agent_runtime.contracts import ConsumedBudget, ExecutionLimits, Message, ToolSpec
from agent_runtime.exceptions import BudgetExhaustedError, ContextBudgetError
from agent_runtime.jsonutil import JsonValue
from agent_runtime.ports import TokenEstimator


def _estimable_texts(message: Message) -> Iterator[str]:
    """Every part of a message that costs tokens, so none of them can silently count as zero."""
    if message.content:
        yield message.content
    if message.reasoning:
        yield message.reasoning
    for call in message.tool_calls:
        yield call.name
        yield str(dict(call.arguments))
    if message.tool_call_id:
        yield message.tool_call_id


def estimate_messages(
    messages: Sequence[Message],
    estimator: TokenEstimator,
    *,
    reserved_tokens: int = 0,
) -> int:
    """Fail closed: content the estimator cannot measure aborts instead of counting as zero."""
    total = reserved_tokens
    for message in messages:
        for text in _estimable_texts(message):
            if not estimator.can_estimate(text):
                raise ContextBudgetError("unestimable message content")
            total += estimator.estimate_text(text)
    return total


class CharQuarterEstimator:
    """Approximate tokens as max(1, ceil(chars/4)) for text; schemas use JSON-ish length.

    This is a scheduling heuristic, not a guarantee that a provider will accept the request.
    """

    def can_estimate(self, text: str) -> bool:
        return True

    def estimate_text(self, text: str) -> int:
        if not self.can_estimate(text):
            raise ContextBudgetError("content type cannot be estimated")
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    def estimate_schema(self, schema: Mapping[str, JsonValue]) -> int:
        encoded = str(dict(schema))
        return self.estimate_text(encoded)


class TokenBudgetPolicy:
    def __init__(
        self,
        *,
        max_prompt_tokens: int,
        reserved_output_tokens: int = 256,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self.max_prompt_tokens = max_prompt_tokens
        self.reserved_output_tokens = reserved_output_tokens
        self.estimator = estimator or CharQuarterEstimator()

    def estimate_messages(self, messages: Sequence[Message]) -> int:
        return estimate_messages(messages, self.estimator)

    def estimate_tools(self, tools: Sequence[ToolSpec]) -> int:
        total = 0
        for spec in tools:
            for text in (spec.name, spec.description):
                if not self.estimator.can_estimate(text):
                    raise ContextBudgetError("unestimable tool description")
                total += self.estimator.estimate_text(text)
            total += self.estimator.estimate_schema(spec.parameters)
        return total

    def estimate_model_attempt(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
        """Count prompt, tool schemas, call arguments, and reserved output for one model attempt.

        Provider TokenUsage is recorded separately and must not be added here.
        """
        return self.estimate_messages(messages) + self.estimate_tools(tools) + self.reserved_output_tokens

    def available_for_prompt(self, tools: Sequence[ToolSpec]) -> int:
        reserved = self.reserved_output_tokens + self.estimate_tools(tools)
        available = self.max_prompt_tokens - reserved
        if available <= 0:
            raise ContextBudgetError("tool schemas and reserved output exceed the token budget")
        return available


class ExecutionBudgetLedger:
    """Run-level cumulative estimate. Every model attempt reserves before its request runs.

    This is the execution budget, not the single-prompt budget of TokenBudgetPolicy: a retry
    reserves again even though it does not consume another model round. Provider TokenUsage is
    reported separately and is never added here.
    """

    def __init__(self, *, limit: int | None = None, consumed: int = 0) -> None:
        self.limit = limit
        self._reserved = consumed

    @property
    def reserved(self) -> int:
        return self._reserved

    def raise_if_exhausted(self) -> None:
        """Reject a restore entry that already sits at or above its cumulative limit."""
        if self.limit is not None and self._reserved >= self.limit:
            raise BudgetExhaustedError("max_estimated_tokens exhausted")

    def reserve(self, estimated_tokens: int) -> None:
        if self.limit is not None and self._reserved + estimated_tokens > self.limit:
            raise BudgetExhaustedError("max_estimated_tokens exhausted")
        self._reserved += estimated_tokens


def execution_budget(limits: ExecutionLimits, consumed: ConsumedBudget) -> ExecutionBudgetLedger:
    return ExecutionBudgetLedger(limit=limits.max_estimated_tokens, consumed=consumed.estimated_tokens)
