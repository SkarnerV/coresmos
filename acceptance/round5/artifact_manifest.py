"""Verify source, wheel and the four installed acceptance copies have identical package files."""

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
        packaged = {name: sha(archive.read(name)) for name in source}
    assert packaged == source
    installed = {}
    for env in ("base-env", "openai-env", "otel-env", "combined-env"):
        site = ROOT / "acceptance" / "round2" / env / "Lib" / "site-packages"
        actual = {name: sha((site / name).read_bytes()) for name in source}
        assert actual == source, env
        installed[env] = {"matched_source_files": len(actual)}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    ci = json.loads((EVIDENCE / "ci-results.json").read_bytes())
    assert ci["headSha"] == commit
    print(
        json.dumps(
            {
                "commit": commit,
                "artifacts": {path.name: sha(path.read_bytes()) for path in (wheel, sdist)},
                "source_files": source,
                "wheel_matches_source": True,
                "installed": installed,
                "ci": [{"name": job["name"], "conclusion": job["conclusion"], "url": job["url"]} for job in ci["jobs"]],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
