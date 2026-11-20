"""Run the same unit tests as CI with concurrency scaled to host load."""

import os
import subprocess
import sys


def load_aware_workers(cpus: int, load: float) -> int:
    """Keep full concurrency on an idle host and scale down on a busy host."""
    return min(cpus, max(2, int(cpus * cpus / max(cpus, load))))


def main() -> int:
    if sys.platform != "linux":
        raise RuntimeError("Linux unit attestations require Linux")
    cpus = len(os.sched_getaffinity(0))
    load = os.getloadavg()[0]
    workers = str(load_aware_workers(cpus, load))
    print(
        f"Unit tests: {workers} workers ({cpus} CPUs, 1-minute load {load:.1f})",
        flush=True,
    )
    env = {**os.environ, "RUST_TEST_THREADS": workers, "CARGO_BUILD_JOBS": workers}
    return subprocess.run(["./test"], env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
