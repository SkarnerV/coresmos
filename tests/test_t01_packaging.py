from __future__ import annotations

from pathlib import Path

from agent_runtime import assemble_default


def test_public_import_does_not_require_extras() -> None:
    assert callable(assemble_default)


def test_package_is_typed() -> None:
    marker = Path(__file__).resolve().parents[1] / "src" / "agent_runtime" / "py.typed"
    assert marker.is_file()
