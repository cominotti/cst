#!/usr/bin/env bash
#
# swap-setup.sh  (the `swap` module of CST: run it as `cst swap ...`)
#
# Thin shim forwarding to swap/cli.py on the system Python (/usr/bin/python3 -I).

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
DIR="$(dirname "${SELF}")"
export CST_PROG="${CST_PROG:-$0}"
exec /usr/bin/python3 -I -B "${DIR}/swap/cli.py" "$@"
