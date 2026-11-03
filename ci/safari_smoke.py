"""Real Safari smoke test for a compiled FastLED sketch (macOS only).

Serves the compiled output with the shipped binary's headless server (which
sends the COOP/COEP headers the pthread build needs), drives Safari through
safaridriver's W3C WebDriver endpoint, and requires the page to render frames
without a JavaScript error. A WebDriver screenshot is kept as an artifact.

This is the Safari invariant from CLAUDE.md: Chrome-only or Node-only checks
do not prove Safari compatibility (for example, a build that depends on JSPI
fails here).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FRAMES_REQUIRED = 30

PROBE_SCRIPT = """
const state = window.__fastledSafariSmoke || (window.__fastledSafariSmoke = {
  frames: 0, errors: [], errorsHooked: false, framesHooked: false
});
if (!state.errorsHooked) {
  state.errorsHooked = true;
  window.addEventListener('error', (e) => state.errors.push(String(e.message || e.error)));
  window.addEventListener('unhandledrejection', (e) => state.errors.push(String(e.reason)));
}
if (!state.framesHooked && window.fastLEDEvents && typeof window.fastLEDEvents.on === 'function') {
  state.framesHooked = true;
  window.fastLEDEvents.on('frame:rendered', () => { state.frames += 1; });
}
const canvas = document.getElementById('myCanvas');
const rect = canvas ? canvas.getBoundingClientRect() : null;
return {
  frames: state.frames,
  framesHooked: state.framesHooked,
  errors: state.errors,
  crossOriginIsolated: self.crossOriginIsolated === true,
  viewportWidth: window.innerWidth,
  userAgent: navigator.userAgent,
  canvas: rect ? {x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                  dpr: window.devicePixelRatio || 1} : null,
  text: document.body ? document.body.innerText.slice(0, 400) : ''
};
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def webdriver(
    method: str, url: str, body: dict | None = None, timeout: float = 60
) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"{method} {url} -> {error.code}: {error.read().decode(errors='replace')}"
        )


def start_server(
    fastled: str, serve_dir: str, log_path: Path
) -> tuple[subprocess.Popen, str]:
    log = log_path.open("w")
    process = subprocess.Popen(
        [fastled, "--internal-serve-dir-headless", serve_dir],
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
    )
    deadline = time.monotonic() + 30
    assert process.stdout is not None
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                raise RuntimeError(f"fastled server exited with {process.returncode}")
            continue
        log.write(line)
        log.flush()
        match = re.search(r"Serving .* at (http://\S+)", line)
        if match:
            return process, match.group(1)
    raise RuntimeError("fastled server did not report its URL within 30 s")


def start_safaridriver() -> tuple[subprocess.Popen, str]:
    port = free_port()
    process = subprocess.Popen(["safaridriver", "-p", str(port)])
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if (
                webdriver("GET", f"{base}/status", timeout=5)
                .get("value", {})
                .get("ready")
            ):
                return process, base
        except (OSError, RuntimeError):
            pass
        time.sleep(0.5)
    raise RuntimeError("safaridriver did not become ready within 30 s")


def lit_pixels(png: bytes, state: dict) -> tuple[int, int]:
    from io import BytesIO

    # Pillow is installed ad hoc by ci/smoke_installed_wheel.sh (`--with pillow`),
    # not by the dev dependency group, so it is not resolvable at type-check time.
    from PIL import Image  # pyright: ignore[reportMissingImports]

    image = Image.open(BytesIO(png)).convert("RGB")
    rect = state.get("canvas")
    if rect and rect["width"] > 0 and rect["height"] > 0:
        scale = image.width / max(1.0, float(state.get("viewportWidth") or image.width))
        dpr = rect["dpr"] if abs(scale - 1.0) < 0.01 else scale
        box = (
            int(rect["x"] * dpr),
            int(rect["y"] * dpr),
            int((rect["x"] + rect["width"]) * dpr),
            int((rect["y"] + rect["height"]) * dpr),
        )
        image = image.crop(box)
    pixels = list(image.getdata())
    return sum(1 for pixel in pixels if max(pixel) > 60), len(pixels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fastled", required=True)
    parser.add_argument("--serve-dir", required=True)
    parser.add_argument("--artifacts", required=True, type=Path)
    # Hosted macOS ARM runners render ~2 FPS here, so 30 frames took the whole
    # 120 s and failed at 29. The loop exits as soon as FRAMES_REQUIRED is met.
    parser.add_argument("--ready-timeout", type=float, default=300)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)

    server, page_url = start_server(
        args.fastled, args.serve_dir, args.artifacts / "safari-server.log"
    )
    driver, base = start_safaridriver()
    session = None
    state: dict = {}
    try:
        created = webdriver(
            "POST",
            f"{base}/session",
            {"capabilities": {"alwaysMatch": {"browserName": "safari"}}},
        )
        session = f"{base}/session/{created['value']['sessionId']}"
        webdriver("POST", f"{session}/url", {"url": page_url}, timeout=120)
        deadline = time.monotonic() + args.ready_timeout
        while time.monotonic() < deadline:
            state = webdriver(
                "POST", f"{session}/execute/sync", {"script": PROBE_SCRIPT, "args": []}
            )["value"]
            if state["errors"] or state["frames"] >= FRAMES_REQUIRED:
                break
            time.sleep(0.5)
        png = base64.b64decode(webdriver("GET", f"{session}/screenshot")["value"])
        (args.artifacts / "safari.png").write_bytes(png)
        lit, total = lit_pixels(png, state)
        state["canvasLitPixels"] = f"{lit}/{total}"
        (args.artifacts / "safari-state.json").write_text(json.dumps(state, indent=2))
        print(f"Safari: {state['userAgent']}")
        print(
            f"Safari: frames={state['frames']} crossOriginIsolated={state['crossOriginIsolated']} "
            f"canvas lit pixels={lit}/{total}"
        )
        if state["errors"]:
            print(
                "Safari page errors:\n  " + "\n  ".join(state["errors"]),
                file=sys.stderr,
            )
            return 1
        if not state["crossOriginIsolated"]:
            print(
                "Safari page is not cross-origin isolated (SharedArrayBuffer unavailable)",
                file=sys.stderr,
            )
            return 1
        if state["frames"] < FRAMES_REQUIRED:
            print(
                f"Safari rendered {state['frames']} frames in {args.ready_timeout:.0f} s "
                f"(need {FRAMES_REQUIRED}); page text: {state.get('text', '')!r}",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        if session:
            try:
                webdriver("DELETE", session, timeout=30)
            except (OSError, RuntimeError):
                pass
        driver.terminate()
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
