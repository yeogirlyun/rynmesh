#!/bin/sh
# Use the active toolchain consistently for desktop packaging and runtime staging.
# An explicit RUSTC wins; otherwise prefer PATH over installation fallbacks.
set -eu

if [ -n "${RUSTC:-}" ]; then
  compiler="$RUSTC"
elif command -v rustc >/dev/null 2>&1; then
  compiler="$(command -v rustc)"
elif [ -n "${CARGO_HOME:-}" ] && [ -x "$CARGO_HOME/bin/rustc" ]; then
  compiler="$CARGO_HOME/bin/rustc"
elif [ -n "${HOME:-}" ] && [ -x "$HOME/.cargo/bin/rustc" ]; then
  compiler="$HOME/.cargo/bin/rustc"
else
  echo "rustc not found; install Rust, add it to PATH, or set RUSTC" >&2
  exit 1
fi

# Check rustc itself before parsing, so a failed compiler cannot be masked by sed.
if ! metadata="$("$compiler" -Vv)"; then
  echo "could not query rustc; check RUSTC and the selected toolchain" >&2
  exit 1
fi
triple="$(printf '%s\n' "$metadata" | sed -n 's/^host: //p')"
case "$triple" in
  ''|*[!A-Za-z0-9_.-]*)
    echo "could not resolve rust host triple" >&2
    exit 1
    ;;
esac
printf '%s\n' "$triple"
