#!/usr/bin/env bash
# 有Docker权限且release可写的账户可直接运行；复用预检、会话保护和失败恢复。
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec bash "$SCRIPT_DIR/run.sh" --upgrade "$@"
