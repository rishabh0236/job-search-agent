#!/usr/bin/env bash
# Install the non-Python toolchain into ./.tooling (gitignored, no root required).
#
# Why vendored binaries instead of system packages:
#   * tectonic — this box has no TeX distribution, and a full TeX Live is several
#     GB. tectonic is a single binary that fetches only the packages a document
#     actually uses, then caches them.
#   * node — the system node is 8.10 and ships no npm, so the frontend toolchain
#     has to come from somewhere. A local tarball keeps it out of system dirs.
#
# Usage: scripts/bootstrap.sh [tectonic|node|all]
set -euo pipefail

TECTONIC_VERSION="0.17.0"
# Node 16 is the last line that runs on this host: 18 and 20 official builds need
# GLIBC >= 2.28 and this machine has 2.27. Vite 4 supports Node 16, which is why the
# frontend pins Vite 4 rather than 5. Raise both together on a newer host.
NODE_VERSION="16.20.2"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLING_DIR="$REPO_ROOT/.tooling"
BIN_DIR="$TOOLING_DIR/bin"
mkdir -p "$BIN_DIR"

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }

install_tectonic() {
  if [[ -x "$BIN_DIR/tectonic" ]]; then
    log "tectonic already installed: $("$BIN_DIR/tectonic" --version 2>&1 | head -1)"
    return 0
  fi

  local archive url
  archive="$(mktemp -d)/tectonic.tar.gz"
  # The musl build is statically linked. The gnu build needs GLIBC_2.29, which
  # this host (Ubuntu 18.04, glibc 2.27) does not have.
  url="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz"

  log "downloading tectonic ${TECTONIC_VERSION}"
  curl -fsSL "$url" -o "$archive"
  tar -xzf "$archive" -C "$BIN_DIR"
  chmod +x "$BIN_DIR/tectonic"
  rm -rf "$(dirname "$archive")"

  log "installed $("$BIN_DIR/tectonic" --version 2>&1 | head -1)"
}

install_node() {
  if [[ -x "$BIN_DIR/node" ]]; then
    log "node already installed: $("$BIN_DIR/node" --version)"
    return 0
  fi

  local archive url extracted
  archive="$(mktemp -d)/node.tar.xz"
  url="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"

  log "downloading node ${NODE_VERSION}"
  curl -fsSL "$url" -o "$archive"
  extracted="$TOOLING_DIR/node-v${NODE_VERSION}-linux-x64"
  tar -xJf "$archive" -C "$TOOLING_DIR"
  rm -rf "$(dirname "$archive")"

  # Symlink rather than copy so `npm` finds its own lib/ relative to the binary.
  ln -sf "$extracted/bin/node" "$BIN_DIR/node"
  ln -sf "$extracted/bin/npm" "$BIN_DIR/npm"
  ln -sf "$extracted/bin/npx" "$BIN_DIR/npx"

  log "installed node $("$BIN_DIR/node" --version) with npm $("$BIN_DIR/npm" --version)"
}

verify_tectonic() {
  # A compiler that installs but cannot produce a PDF is worse than no compiler,
  # because the failure would surface later inside the tailoring pipeline.
  local workdir
  workdir="$(mktemp -d)"
  cat >"$workdir/probe.tex" <<'TEX'
\documentclass{article}
\begin{document}
Career Agent LaTeX toolchain probe.
\end{document}
TEX

  log "verifying tectonic can compile (first run downloads support files)"
  if (cd "$workdir" && "$BIN_DIR/tectonic" -X compile probe.tex --outdir . >/dev/null 2>&1) \
    && [[ -f "$workdir/probe.pdf" ]]; then
    log "tectonic produced a PDF ($(stat -c%s "$workdir/probe.pdf") bytes)"
    rm -rf "$workdir"
  else
    warn "tectonic could not compile a minimal document; LaTeX features will be unavailable"
    rm -rf "$workdir"
    return 1
  fi
}

case "${1:-all}" in
  tectonic) install_tectonic && verify_tectonic ;;
  node) install_node ;;
  all)
    install_tectonic && verify_tectonic
    install_node
    ;;
  *)
    echo "usage: $0 [tectonic|node|all]" >&2
    exit 2
    ;;
esac

log "done. Binaries are in .tooling/bin (referenced by CA_LATEX_BIN)."
