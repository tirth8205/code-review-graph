#!/bin/sh
# code-review-graph one-line installer (macOS / Linux).
#
# What this does, in order:
#   1. Ensures `uv` (https://docs.astral.sh/uv/) is available, installing it via
#      the official Astral installer if missing.
#   2. Installs the `code-review-graph` CLI as a uv tool (falling back to pipx,
#      then `pip --user`).
#   3. Prints the next steps.
#
# NOTE: This installs `uv`, a single static Python toolchain manager. It is NOT
# a bundled/standalone runtime — uv manages Python for you, so you do not have
# to set Python up yourself, but a Python interpreter is still downloaded/used
# under the hood by uv.
#
# Idempotent: safe to re-run. POSIX sh, no bashisms.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/tirth8205/code-review-graph/main/install.sh | sh
#   # or, from a checkout:
#   sh install.sh

set -eu

# --- helpers ---------------------------------------------------------------

# Pinned to the official Astral uv installer. We echo this before running it so
# the user can see exactly what is being executed.
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

info() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

die() {
    err "$*"
    exit 1
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# --- 1. ensure uv ----------------------------------------------------------

ensure_uv() {
    if have uv; then
        info "uv already installed: $(uv --version 2>/dev/null || echo 'uv')"
        return 0
    fi

    info "uv not found. Installing uv via the official Astral installer."
    info "  This runs: curl -LsSf ${UV_INSTALLER_URL} | sh"
    info "  (uv is a single static binary that manages Python for you;"
    info "   this is not a bundled runtime.)"

    if have curl; then
        curl -LsSf "$UV_INSTALLER_URL" | sh || die "uv installation failed (curl)."
    elif have wget; then
        wget -qO- "$UV_INSTALLER_URL" | sh || die "uv installation failed (wget)."
    else
        die "Neither curl nor wget is available. Install one, or install uv \
manually: https://docs.astral.sh/uv/getting-started/installation/"
    fi

    # uv installs to ~/.local/bin (or $XDG_BIN_HOME). Make it visible for the
    # rest of this script even if the user has not restarted their shell.
    if ! have uv; then
        for dir in "$HOME/.local/bin" "$HOME/.cargo/bin" "${XDG_BIN_HOME:-}"; do
            if [ -n "$dir" ] && [ -x "$dir/uv" ]; then
                PATH="$dir:$PATH"
                export PATH
                break
            fi
        done
    fi

    have uv || die "uv was installed but is not on PATH. Open a new shell (or \
add ~/.local/bin to PATH) and re-run this script."
    info "uv installed: $(uv --version 2>/dev/null || echo 'uv')"
}

# --- 2. install the CLI ----------------------------------------------------

install_crg() {
    # Preferred path: uv tool install (isolated, fast, no venv juggling).
    info "Installing code-review-graph with: uv tool install code-review-graph"
    if uv tool install code-review-graph; then
        return 0
    fi
    warn "uv tool install failed; trying pipx."

    if have pipx; then
        info "Installing with: pipx install code-review-graph"
        if pipx install code-review-graph; then
            return 0
        fi
        warn "pipx install failed; trying pip --user."
    fi

    # Last-resort fallback: pip --user.
    PIP=""
    if have pip3; then
        PIP="pip3"
    elif have pip; then
        PIP="pip"
    fi
    if [ -n "$PIP" ]; then
        info "Installing with: $PIP install --user code-review-graph"
        if "$PIP" install --user code-review-graph; then
            return 0
        fi
    fi

    die "All install methods failed (uv tool / pipx / pip --user). \
See https://github.com/tirth8205/code-review-graph for manual instructions."
}

# --- 3. next steps ---------------------------------------------------------

print_next_steps() {
    info ""
    info "code-review-graph installed. Next steps:"
    info ""
    info "  1. Configure your AI coding tools:"
    info "       code-review-graph install"
    info ""
    info "  2. Build the graph for your project (run inside a repo):"
    info "       code-review-graph build"
    info ""
    info "  3. Verify the graph (health / stats check):"
    info "       code-review-graph status"
    info ""
    if ! have code-review-graph; then
        warn "'code-review-graph' is not on your PATH yet. If you installed via \
uv, run 'uv tool update-shell' or open a new terminal. If you used 'pip \
--user', add your user scripts dir to PATH."
    fi
}

# --- main ------------------------------------------------------------------

main() {
    info "code-review-graph installer"
    ensure_uv
    install_crg
    print_next_steps
}

main "$@"
