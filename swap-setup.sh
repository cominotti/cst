#!/usr/bin/env bash
#
# swap-setup.sh  (the `swap` module of CST: run it as `cst swap ...`)
#
# Thin shim forwarding to swap/cli.py on the system Python (/usr/bin/python3 -I).

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
DIR="$(dirname "${SELF}")"
export CST_PROG="${CST_PROG:-$0}"
# CST_LC_CTYPE: the caller's LC_CTYPE ("" when unset, "=VALUE" when set),
# which Python changes at start under a C locale; swap/cli.py puts it back
# for the commands it runs.
CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" exec /usr/bin/python3 -I -B "${DIR}/swap/cli.py" "$@"
