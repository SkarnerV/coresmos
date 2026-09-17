"""JSON Schema checks with an in-memory referencing registry. No network or file retrieval."""

from __future__ import annotations

from collections.abc import Mapping

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from agent_runtime.contracts import ToolCall, ToolSpec
from agent_runtime.exceptions import SchemaValidationError
from agent_runtime.jsonutil import JsonValue, thaw_json

SUPPORTED_DIALECTS = frozenset(
    {
        "https://json-schema.org/draft/2020-12/schema",
        "http://json-schema.org/draft/2020-12/schema",
    }
)
DEFAULT_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _as_dict(schema: Mapping[str, JsonValue] | Mapping[str, object]) -> dict[str, object]:
    thawed = thaw_json(schema)  # type: ignore[arg-type]
    if not isinstance(thawed, dict):
        raise SchemaValidationError("schema must be a JSON object")
    return thawed


def dialect_of(schema: Mapping[str, object]) -> str:
    value = schema.get("$schema")
    if value is None:
        return DEFAULT_DIALECT
    if not isinstance(value, str):
        raise SchemaValidationError("$schema must be a string")
    return value


def assert_supported_schema(schema: Mapping[str, JsonValue] | Mapping[str, object]) -> None:
    material = _as_dict(schema)
    dialect = dialect_of(material)
    if dialect not in SUPPORTED_DIALECTS:
        raise SchemaValidationError(f"unsupported JSON Schema dialect: {dialect}")
    try:
        Draft202012Validator.check_schema(material)
    except SchemaError as exc:
        raise SchemaValidationError(str(exc)) from exc


def memory_registry(schemas: Mapping[str, Mapping[str, object]]) -> Registry[str]:
    registry: Registry[str] = Registry()
    for uri, schema in schemas.items():
        resource = Resource.from_contents(_as_dict(schema), default_specification=DRAFT202012)
        registry = registry.with_resource(uri, resource)
    return registry


def validate_instance(
    schema: Mapping[str, JsonValue] | Mapping[str, object],
    instance: object,
    *,
    registry: Registry[str] | None = None,
) -> None:
    material = _as_dict(schema)
    assert_supported_schema(material)
    resolver_registry = registry if registry is not None else memory_registry({})
    validator = Draft202012Validator(material, registry=resolver_registry)
    try:
        validator.validate(instance)
    except Unresolvable as exc:
        raise SchemaValidationError(f"schema $ref is not available in memory: {exc}") from exc
    except ValidationError as exc:
        raise SchemaValidationError(str(exc)) from exc


def check_tool_spec(spec: ToolSpec) -> None:
    assert_supported_schema(spec.parameters)


def validate_tool_call(spec: ToolSpec, call: ToolCall, *, registry: Registry[str] | None = None) -> None:
    check_tool_spec(spec)
    validate_instance(spec.parameters, dict(call.arguments), registry=registry)
