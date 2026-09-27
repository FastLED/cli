# ban_std_pathbuf

A dylint that bans `std::path::PathBuf` outside an explicit per-file
allowlist (`src/allowlist.txt`). Migrated call sites should use
`fastled_cli::path::NormalizedPath` instead, which strips Windows
long-path prefixes (`\\?\`) and applies case-insensitive comparison on
case-insensitive filesystems.

## Why

Issue #114 hit a Windows-only crash where `fs::canonicalize` produced
`\\?\C:\...` paths that meson and Python's `open()` couldn't handle.
Wrapping every boundary path in `NormalizedPath` prevents that class of
regression from coming back into a different module.

## Layout

- `src/lib.rs` — the lint pass itself (ported from zccache).
- `src/allowlist.txt` — file suffixes that may still use raw `PathBuf`.

## Toolchain

Builds against `nightly-2026-05-28` (see `rust-toolchain.toml`). The main
workspace stays on stable; this sub-crate is intentionally not in the
workspace. The local `[workspace]` table in `Cargo.toml` keeps Cargo from
implicitly attaching this crate to the parent stable workspace.

## Running

The repository `./lint` script runs this lint after `cargo clippy`. Soldr
prepares the published Dylint tools and driver for the pinned nightly;
`.cargo/config.toml` directs the lint crate through `dylint-link` to emit
the toolchain-suffixed library Dylint expects:

```bash
./lint
```

The root `rust-toolchain.toml` remains stable. The Dylint build/check clears
the ambient stable `RUSTUP_TOOLCHAIN` so Soldr selects the pinned nightly.

## Reference

Ported from <https://github.com/zackees/zccache/tree/main/dylints/ban_std_pathbuf>.
