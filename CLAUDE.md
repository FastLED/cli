# Agent Instructions

This file is read by Claude Code, Codex, and other coding agents that work on this repo. Codex agents should also see `CODEX.md`, which points back here.

## Layout

- Python package shim: `src/fastled/`
- Rust workspace: `crates/fastled-cli`
- Tests: `tests/unit/` (thin Python API smoke tests), `soldr cargo test --workspace` (Rust)
- CI workflows: `.github/workflows/_*.yml`
- Dev scripts: `./install`, `./lint`, `./test`, `./build-wheel`, `./clean`

## Build Cache

Use `soldr cargo ...`, `soldr rustfmt ...`, and related soldr-wrapped commands for Rust work. Plain `cargo`, `rustc`, `rustfmt`, and `rustup` bypass soldr and are blocked by `ci/hooks/tool_guard.py`.

If `soldr` is not on PATH, install it with `uv tool install soldr`.

## Tests

Ordinary PR/main CI is minimal. The literal `ci-test` PR label adds Linux x86
unit and integration tests; `ci-full` runs the platform matrix. Full validation
must pass on the exact release SHA before tagging or publishing. A version bump
on `main` or a pushed VS Code tag no longer starts a release. Both CLI and
VS Code releases require manual exact-SHA dispatch and artifact preflight.
See [fractional CI and release gates](docs/FRACTIONAL_CI.md).

`bash test` runs the Python API smoke tests plus the Rust workspace tests. End-to-end WASM compiles should be run only when the change touches the native build backend.

For iterative work:

```bash
uv run pytest tests/unit/test_python_api.py -v
soldr cargo test --workspace
```

Do not pipe pytest through `| tail -N` if you want streaming output; it buffers until pytest exits.

## Architecture Notes

- The user-facing CLI is the Rust binary `fastled` built from `crates/fastled-cli`.
- Python `cli.py` and `app.py` are tiny shims that call `fastled._rust_cli.invoke_rust_fastled_cli`.
- The Python package intentionally does not ship a PyO3 extension; it bundles and launches the native Rust CLI.
- The compile path is Rust-owned through `crates/fastled-cli/src/build.rs` and `crates/fastled-cli/src/wasm_build.rs`.
- Python no longer owns build, toolchain, sketch selection, project init, install, server, debug-symbol, or frontend-bundling orchestration.

## Browser Compatibility Invariants

- Safari is a required production target. Production compiler defaults and generated artifacts must not depend on JavaScript Promise Integration (JSPI), because Safari does not support the JSPI path required by Emscripten.
- Do not add or preserve `-sJSPI`, `-sJSPI_EXPORTS`, `WebAssembly.Suspending`, or `WebAssembly.promising` as part of an Emscripten upgrade or build-performance change. Newer Emscripten support for JSPI is not an upgrade benefit for this project.
- Existing JSPI flags or code are compatibility debt, not precedent. Removing or replacing them requires a dedicated Safari-compatible implementation and browser validation; do not silently propagate them into a new toolchain configuration.
- Every Emscripten upgrade must inspect the effective linker flags and generated JavaScript for newly enabled JSPI behavior, then run a real Safari smoke test before the toolchain can become the release default. Chrome- or Node-only tests are insufficient.
- Async and coroutine behavior must retain a non-JSPI Safari-compatible implementation. Do not accept an Emscripten default change that narrows browser support without an explicit project decision.

## Conventions

- Lint command: `bash lint`.
- Test command: `bash test` or targeted subsets above.
- Prefer native Rust implementations; fail-loud behavior is Design principle 6 below.
- Do not add backwards-compat hacks for code paths that no longer exist.
- For deterministic in-product render checks, use `fastled <sketch> --test`
  with the `--test-*` screenshot, interval, log, and timeout flags. This uses
  the shipped Tauri viewer and exits automatically; no browser harness is needed.

## Design principles (fleet directives, zackees/ci.yml#163)

Agent and review guidance from zackees/ci.yml `docs/policy-rust.md` ("Design principles, narrowed"). Not enforced by ci-lint; no rule ID.

1. **Search before you add.** Before adding a resolver, detector, predicate, guard or path helper, `rg` for an existing one. Reuse it, or say in the PR why it does not fit.
2. **Conflicting implementations get a research issue, not an inline winner.** If the same concept is implemented more than once with observably different behaviour and fixing it needs a design decision, file `research: N conflicting implementations of X -- which is canonical?` listing every copy (`file:line`) and what the difference does to real input; cross-reference it and carry on with your task. Copies that behave identically are a refactor, not an issue.
3. **Keep behaviour changes and refactors apart.** Separate commits, preferably separate PRs, never one mixed diff.
4. **Refactor first only when you would grow a giant function.** If a fix would add more than 50 lines to a function already over 200 lines, extract first in a separate behaviour-preserving change with characterization tests written first (RED -> GREEN). Otherwise fix it where it belongs; no unrequested restructures.
5. **Function and file size follow the complexity ratchet (`RUST-018`).** Until it is wired into this workspace's `Cargo.toml`, new functions stay under clippy's `too_many_lines` (100) and `cognitive_complexity` (25). A function carrying an `#[expect]` must not grow; delete the `expect` once it is back under. Never split a file with `include!` fragments to dodge a length ceiling. Before splitting a hot file, check `git branch -a --contains` for in-flight branches -- a split is a rename and conflicts destructively.
6. **Fail fast, and delete before you abstract.** Reject invalid input at the boundary with a typed error; never fall back silently to a degraded path (that includes silent Python fallbacks for Rust-owned paths). Prefer deleting code to adding a layer. No new trait until there are two real implementations (a test double does not count).
7. **Existing boundaries beat generic DRY.** Platform modules, Dylint-enforced layering (`dylints/ban_std_pathbuf`), crate boundaries and documented deliberate copies win over removing duplication. Do not copy code across a crate boundary to get around it -- say the boundary hurts. An undocumented copy is a finding under 2; a documented one is not.

## Common Gotchas

- The installed `fastled` CLI on PATH may be a Python compatibility shim or stale script; the bundled native binary is `fastled[.exe]`.
- Error messages from the Rust CLI go to stderr.
- On Windows, stale generated binaries under `src/fastled/bin/` are build artifacts and should not be committed.
- `src/fastled/frontend/build_flags.toml` currently contains legacy JSPI flags. Their presence does not make JSPI an approved compatibility target; see Browser Compatibility Invariants above.

## Filing Issues / PRs

- `gh issue create --repo zackees/fastled-wasm ...`
- `gh pr create ...`
- Reference the originating issue (`Refs #72`) in commits/PRs when applicable.
