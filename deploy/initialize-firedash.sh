#!/usr/bin/env bash
# Application initialization.
# Usage: initialize-firedash.sh {secrets|admin}
set -Eeuo pipefail

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(CDPATH= cd -- "$SELF_DIR/.." && pwd)

# shellcheck source=lib/common.sh
source "$SELF_DIR/lib/common.sh"
# shellcheck source=lib/secrets.sh
source "$SELF_DIR/lib/secrets.sh"
# shellcheck source=lib/admin.sh
source "$SELF_DIR/lib/admin.sh"

FIREDASH_REPO_ROOT=${FIREDASH_REPO_ROOT:-$ROOT}
export FIREDASH_REPO_ROOT

require_root

cmd=${1:-}
case "$cmd" in
    secrets) secrets_ensure ;;
    admin) bootstrap_admin ;;
    *) die "usage: initialize-firedash.sh {secrets|admin}" ;;
esac
