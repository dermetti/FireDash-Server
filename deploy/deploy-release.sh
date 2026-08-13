#!/usr/bin/env bash
# Immutable release construction and mutable activation.
# Usage: deploy-release.sh {build|activate}
set -Eeuo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(CDPATH= cd -- "$SELF_DIR/.." && pwd)

# shellcheck source=lib/common.sh
source "$SELF_DIR/lib/common.sh"
# shellcheck source=lib/release.sh
source "$SELF_DIR/lib/release.sh"

FIREDASH_REPO_ROOT=${FIREDASH_REPO_ROOT:-$ROOT}
export FIREDASH_REPO_ROOT

require_root

cmd=${1:-}
case "$cmd" in
    build) build_release ;;
    activate) activate_release ;;
    *) die "usage: deploy-release.sh {build|activate}" ;;
esac
