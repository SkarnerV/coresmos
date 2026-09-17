"""Model-view compression. Originals stay in the transcript; only the projection shrinks."""

from __future__ import annotations

from collections.abc import Sequence

from agent_runtime.context.budget import estimate_messages
from agent_runtime.contracts import (
    Message,
    ModelCompleted,
    ModelConfig,
    ModelRequest,
    Role,
    RunControl,
    TextDeltaEvent,
)
from agent_runtime.exceptions import ContextBudgetError
from agent_runtime.lifecycle import await_despite_cancellation
from agent_runtime.ports import LowLevelModel, Summarizer, TokenEstimator

DEFAULT_SUMMARY_INSTRUCTION = (
    "Summarize the conversation below. Keep decisions, facts, and open questions. "
    "Do not invent details and do not answer the user's request."
)


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
    return estimate_messages(messages, estimator, reserved_tokens=reserved_tokens)


def group_messages(messages: Sequence[Message]) -> tuple[tuple[Message, ...], ...]:
    """Split a paired view into indivisible units: one tool-call group or one plain message."""
    groups: list[tuple[Message, ...]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role is Role.ASSISTANT and message.tool_calls:
            call_ids = {call.call_id for call in message.tool_calls}
            end = index + 1
            while end < len(messages) and messages[end].role is Role.TOOL and messages[end].tool_call_id in call_ids:
                end += 1
            groups.append(tuple(messages[index:end]))
            index = end
            continue
        groups.append((message,))
        index += 1
    return tuple(groups)


class ToolGroupCompressor:
    """Deterministic trimming. Tool-call groups are dropped whole, oldest first.

    Once no tool group is left, the oldest plain turn is dropped instead, so a long text-only
    history compresses rather than failing the run. System messages carry the injected
    contributions and the final message carries the current turn, so neither is ever dropped.
    """

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
            return estimate_messages(items, estimator, reserved_tokens=reserved_tokens)

        while estimate(paired) > budget_tokens:
            span = _oldest_tool_group(paired) or _oldest_plain_turn(paired)
            if span is None:
                raise ContextBudgetError("required context exceeds the token budget")
            start, end = span
            del paired[start:end]
            compressed = True
        return tuple(paired), compressed


class SummarizingCompressor:
    """Replaces the oldest history with one injected summary, then trims what still overflows.

    The summarizer arrives through the model interface, so this class owns only the
    deterministic choice of what to summarize: whole units, oldest first, never a partial
    tool-call group.
    """

    def __init__(
        self,
        summarizer: Summarizer,
        *,
        keep_recent_units: int = 4,
        source: str = "history_summary",
    ) -> None:
        self._summarizer = summarizer
        self._keep_recent_units = max(1, keep_recent_units)
        self._source = source
        self._trimmer = ToolGroupCompressor()

    async def compress(
        self,
        messages: Sequence[Message],
        *,
        budget_tokens: int,
        estimator: TokenEstimator,
        reserved_tokens: int,
    ) -> tuple[tuple[Message, ...], bool]:
        paired = pair_tool_messages(messages)
        if estimate_messages(paired, estimator, reserved_tokens=reserved_tokens) <= budget_tokens:
            return paired, False
        systems = tuple(item for item in paired if item.role is Role.SYSTEM)
        units = group_messages(tuple(item for item in paired if item.role is not Role.SYSTEM))
        cut = max(0, len(units) - self._keep_recent_units)
        dropped = tuple(item for unit in units[:cut] for item in unit)
        if not dropped:
            return await self._trimmer.compress(
                paired,
                budget_tokens=budget_tokens,
                estimator=estimator,
                reserved_tokens=reserved_tokens,
            )
        text = await self._summarizer.summarize(dropped, budget_tokens=max(1, budget_tokens // 4))
        summary = Message(role=Role.SYSTEM, content=text, name=self._source)
        kept = tuple(item for unit in units[cut:] for item in unit)
        view = systems + (summary,) + kept
        if estimate_messages(view, estimator, reserved_tokens=reserved_tokens) > budget_tokens:
            trimmed, _ = await self._trimmer.compress(
                view,
                budget_tokens=budget_tokens,
                estimator=estimator,
                reserved_tokens=reserved_tokens,
            )
            return trimmed, True
        return view, True


class ModelSummarizer:
    """Summarizer backed by the LowLevelModel port.

    Pass the run's RunControl to make the summarization call stop with the run. Without one a
    fresh control is used, and the call is only bounded by the caller's own wait.
    """

    def __init__(
        self,
        model: LowLevelModel,
        *,
        config: ModelConfig | None = None,
        instruction: str = DEFAULT_SUMMARY_INSTRUCTION,
        control: RunControl | None = None,
    ) -> None:
        self._model = model
        self._config = config or ModelConfig()
        self._instruction = instruction
        self._control = control

    async def summarize(self, messages: Sequence[Message], *, budget_tokens: int) -> str:
        del budget_tokens
        request = ModelRequest(
            messages=(
                Message(role=Role.SYSTEM, content=self._instruction),
                Message(role=Role.USER, content=render_transcript(messages)),
            ),
            tools=(),
            model=self._config,
        )
        stream = self._model.stream(request, self._control or RunControl())
        parts: list[str] = []
        final: str | None = None
        try:
            async for event in stream:
                if isinstance(event, ModelCompleted):
                    final = event.result.text
                elif isinstance(event, TextDeltaEvent):
                    parts.append(event.text)
        finally:
            closer = getattr(stream, "aclose", None)
            if closer is not None:
                await await_despite_cancellation(closer())
        text = (final or "".join(parts)).strip()
        if not text:
            raise ContextBudgetError("summarizer returned no text")
        return text


def render_transcript(messages: Sequence[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        if message.content:
            lines.append(f"{message.role.value}: {message.content}")
        for call in message.tool_calls:
            lines.append(f"{message.role.value}: called {call.name}({dict(call.arguments)})")
    return "\n".join(lines)


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


def _oldest_plain_turn(messages: list[Message]) -> tuple[int, int] | None:
    """Oldest non-system turn, excluding the most recent message."""
    for index, message in enumerate(messages[:-1]):
        if message.role is not Role.SYSTEM:
            return index, index + 1
    return None
