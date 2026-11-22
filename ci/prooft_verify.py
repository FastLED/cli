"""Plan PR skips using the verifier and policy checked out from the base."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOFT_SOURCE = (
    "git+https://github.com/forge-ci/prooft@cf897d4a49735c880c22c5994f5c95ac63a2d352"
)


def prooft(*args: str) -> list[str]:
    return ["uvx", "--from", PROOFT_SOURCE, "prooft", "-C", str(ROOT), *args]


def main(base: str, head: str, title: str) -> int:
    subprocess.run(["git", "fetch", "--no-tags", "origin", head], cwd=ROOT, check=True)
    return subprocess.run(
        prooft(
            "plan",
            "--base",
            base,
            "--head",
            head,
            f"--title={title}",
            "--github-output",
        ),
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
