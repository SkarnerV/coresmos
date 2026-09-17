"""Token budget policy. Default estimator is documented character/4; unknown types fail closed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent_runtime.contracts import Message, ToolSpec
from agent_runtime.exceptions import ContextBudgetError
from agent_runtime.jsonutil import JsonValue
from agent_runtime.ports import TokenEstimator


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
        total = 0
        for message in messages:
            if message.content:
                if not self.estimator.can_estimate(message.content):
                    raise ContextBudgetError("unestimable message content")
                total += self.estimator.estimate_text(message.content)
            if message.reasoning:
                total += self.estimator.estimate_text(message.reasoning)
            for call in message.tool_calls:
                total += self.estimator.estimate_text(call.name)
                total += self.estimator.estimate_text(str(dict(call.arguments)))
            if message.tool_call_id:
                total += self.estimator.estimate_text(message.tool_call_id)
        return total

    def estimate_tools(self, tools: Sequence[ToolSpec]) -> int:
        return sum(
            self.estimator.estimate_text(spec.name)
            + self.estimator.estimate_text(spec.description)
            + self.estimator.estimate_schema(spec.parameters)
            for spec in tools
        )

    def available_for_prompt(self, tools: Sequence[ToolSpec]) -> int:
        reserved = self.reserved_output_tokens + self.estimate_tools(tools)
        available = self.max_prompt_tokens - reserved
        if available <= 0:
            raise ContextBudgetError("tool schemas and reserved output exceed the token budget")
        return available
