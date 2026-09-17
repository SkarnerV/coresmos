"""Shared JSON freeze helpers. Frozen dataclasses do not protect nested mutables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | tuple[JsonValue, ...] | Mapping[str, JsonValue]


def freeze_json(value: object) -> JsonValue:
    """Return a deep copy that callers cannot mutate through the original object."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value type: {type(value)!r}")


def thaw_json(value: JsonValue) -> object:
    """Materialize a mutable copy for validators that expect dict/list."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    return [thaw_json(item) for item in value]


def freeze_mapping(value: Mapping[str, object] | None) -> Mapping[str, JsonValue]:
    frozen = freeze_json({} if value is None else dict(value))
    if not isinstance(frozen, Mapping):
        raise TypeError("expected a JSON object")
    return frozen
