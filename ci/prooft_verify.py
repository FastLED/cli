"""Plan PR skips using the verifier and policy checked out from the base."""

import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOFT_SOURCE = (
    "git+https://github.com/forge-ci/prooft@cf897d4a49735c880c22c5994f5c95ac63a2d352"
)


def require_attesting_host() -> None:
    """Refuse to attest anywhere but the host the policy's linux-x64 gates name."""
    host = (platform.system(), platform.machine(), platform.libc_ver()[0])
    if host != ("Linux", "x86_64", "glibc"):
        raise SystemExit(
            f"./push: the attestation lanes require Linux x86_64 with glibc, not {host}"
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
