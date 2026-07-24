#!/usr/bin/env bash
# kindle-butch-gen install.sh alias wrapper for deploy.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/deploy.sh" "$@"
