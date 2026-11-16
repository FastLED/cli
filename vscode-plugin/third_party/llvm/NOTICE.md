# LLVM notice

This extension redistributes `clangd` and the Clang builtin headers from LLVM
21.1.5 (`8e2cd28cd4ba46613a46467b0c91b1cabead26cd`) and LLVM 21.1.6
(`a832a5222e489298337fbb5876f8dcaf072c5cca`).  The six target archives are
distributed by `clang-tool-chain-bins` 0.4.6: Windows x64/ARM64 and Linux
x64/ARM64 are extracted upstream LLVM builds; macOS x64 is built with
forge-ci/forge; macOS ARM64 is extracted upstream LLVM.  See
`clangd-artifacts.json` for the pinned archive provenance and checksums.

LLVM is licensed under the Apache License 2.0 with LLVM Exceptions.  The
complete license text is in `LICENSE.TXT`.
