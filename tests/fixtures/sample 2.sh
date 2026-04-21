#!/bin/bash
# Sample shell script exercising the bash parser.

set -euo pipefail

source ./sample_lib.sh
. ./sample_config.sh

readonly DATA_DIR="/tmp/crg-example"

log_info() {
    local msg="$1"
    echo "[INFO] $msg"
}

log_error() {
    local msg="$1"
    echo "[ERROR] $msg" >&2
}

ensure_dir() {
    local dir="$1"
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        log_info "created $dir"
    fi
}

cleanup() {
    rm -rf "$DATA_DIR"
    log_info "cleaned up $DATA_DIR"
}

main() {
    log_info "starting"
    ensure_dir "$DATA_DIR"
    # Simulate some work
    echo "processing" > "$DATA_DIR/status"
    cleanup
    log_info "done"
}

main "$@"
