"""Bind acceptance artifacts to the uncommitted source changes under review."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(__file__).resolve().parent


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    wheel = EVIDENCE / "dist" / "agent_runtime-0.1.0-py3-none-any.whl"
    sdist = EVIDENCE / "dist" / "agent_runtime-0.1.0.tar.gz"
    files = sorted((ROOT / "src" / "agent_runtime").rglob("*.py")) + [ROOT / "src" / "agent_runtime" / "py.typed"]
    source = {path.relative_to(ROOT / "src").as_posix(): sha(path.read_bytes()) for path in files}
    with zipfile.ZipFile(wheel) as archive:
        assert {name: sha(archive.read(name)) for name in source} == source
    installed = {}
    for env in ("base-env", "openai-env", "otel-env", "combined-env"):
        site = ROOT / "acceptance" / "round2" / env / "Lib" / "site-packages"
        assert {name: sha((site / name).read_bytes()) for name in source} == source, env
        installed[env] = {"matched_source_files": len(source)}
    current_patch = subprocess.check_output(["git", "diff", "--", "src/agent_runtime"], cwd=ROOT)
    reviewed_patch = (EVIDENCE / "source-changes.patch").read_bytes()
    assert current_patch == reviewed_patch, "Source changes moved during acceptance"
    print(
        json.dumps(
            {
                "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "uncommitted_source_changes": True,
                "source_patch_sha256": sha(reviewed_patch),
                "artifacts": {path.name: sha(path.read_bytes()) for path in (wheel, sdist)},
                "source_files": source,
                "wheel_matches_source": True,
                "installed": installed,
                "ci_for_this_working_tree": "not_run",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
