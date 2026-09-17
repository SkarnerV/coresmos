"""Deterministic compression that drops oldest complete tool-call groups."""

from __future__ import annotations

from collections.abc import Sequence

from agent_runtime.contracts import Message, Role
from agent_runtime.exceptions import ContextBudgetError
from agent_runtime.ports import TokenEstimator


def pair_tool_messages(messages: Sequence[Message]) -> tuple[Message, ...]:
    """Keep only complete, adjacent, unique tool-call/result groups in the model view."""
    kept: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role is Role.ASSISTANT and message.tool_calls:
            call_ids = [call.call_id for call in message.tool_calls]
            cursor = index + 1
            if len(set(call_ids)) != len(call_ids):
                while cursor < len(messages) and messages[cursor].role is Role.TOOL:
                    cursor += 1
                index = cursor
                continue
            needed = set(call_ids)
            results: list[Message] = []
            seen: set[str] = set()
            while cursor < len(messages) and messages[cursor].role is Role.TOOL:
                tool_id = messages[cursor].tool_call_id
                if tool_id and tool_id in needed and tool_id not in seen:
                    results.append(messages[cursor])
                    seen.add(tool_id)
                    cursor += 1
                    if seen == needed:
                        break
                    continue
                break
            if seen == needed and len(results) == len(call_ids):
                kept.append(message)
                kept.extend(results)
                index = cursor
                continue
            while cursor < len(messages) and messages[cursor].role is Role.TOOL:
                cursor += 1
            index = cursor
            continue
        if message.role is Role.TOOL:
            index += 1
            continue
        kept.append(message)
        index += 1
    return tuple(kept)


def estimate_history_tokens(messages: Sequence[Message], estimator: TokenEstimator, *, reserved_tokens: int = 0) -> int:
    total = reserved_tokens
    for message in messages:
        if message.content:
            if not estimator.can_estimate(message.content):
                raise ContextBudgetError("unestimable content in compression")
            total += estimator.estimate_text(message.content)
        if message.reasoning:
            total += estimator.estimate_text(message.reasoning)
        for call in message.tool_calls:
            total += estimator.estimate_text(call.name)
            total += estimator.estimate_text(str(dict(call.arguments)))
        if message.tool_call_id:
            total += estimator.estimate_text(message.tool_call_id)
    return total


class ToolGroupCompressor:
    async def compress(
        self,
        messages: Sequence[Message],
        *,
        budget_tokens: int,
        estimator: TokenEstimator,
        reserved_tokens: int,
    ) -> tuple[tuple[Message, ...], bool]:
        paired = list(pair_tool_messages(messages))
        compressed = False

        def estimate(items: Sequence[Message]) -> int:
            return estimate_history_tokens(items, estimator, reserved_tokens=reserved_tokens)

        while estimate(paired) > budget_tokens:
            group = _oldest_tool_group(paired)
            if group is None:
                raise ContextBudgetError("required context exceeds the token budget")
            start, end = group
            del paired[start:end]
            compressed = True
        return tuple(paired), compressed


def _oldest_tool_group(messages: list[Message]) -> tuple[int, int] | None:
    for index, message in enumerate(messages):
        if message.role is Role.ASSISTANT and message.tool_calls:
            call_ids = {call.call_id for call in message.tool_calls}
            end = index + 1
            while end < len(messages) and messages[end].role is Role.TOOL:
                if messages[end].tool_call_id in call_ids:
                    end += 1
                    continue
                break
            if end > index + 1:
                return index, end
    return None
