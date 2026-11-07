#!/usr/bin/env bash
# 有Docker权限且release可写的账户可直接运行；复用归属校验、部署锁及优雅停止。
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec bash "$SCRIPT_DIR/run.sh" --stop "$@"
