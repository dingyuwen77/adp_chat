#!/usr/bin/env bash
# 可用 sh 调用；统一在 Bash 中执行，停止和升级入口复用同一套实现。
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -Eeuo pipefail
umask 077

RELEASE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
CONFIG_FILE="$RELEASE_DIR/config.toml"
CONTAINER_NAME="${ADP_CONTAINER_NAME:-adp_channel_api}"
STATE_FILE="$RELEASE_DIR/.deploy-state.json"
VERSION_PATTERN='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
ATTEMPT="$$-$(date +%s)"
WORK_DIR=""
ROLLBACK_NEEDED=0
RECOVERING=0
OLD_ID="" OLD_IMAGE="" OLD_VERSION="" OLD_BIND="" OLD_PORT=""
VERSION="" IMAGE_ID="" TOOL_IMAGE="" ARCHIVE_SHA="" CONFIG_HASH=""
BASELINE_CID="" BASELINE_SHA="" NEW_ID="" PREVIOUS_VERSION=""
INSPECT_JSON=""
declare -A PLAN=() CURRENT=()

log() {
    # 使用北京时间及真实调用行号记录低频部署阶段，不打印配置内容。
    local level=$1; shift
    printf '[%s %s L%s] [%s] %s\n' \
        "$(TZ=CST-8 date '+%Y-%m-%d %H:%M:%S.%3N')" \
        "${BASH_SOURCE[1]##*/}" "${BASH_LINENO[0]}" "$level" "$*" >&2
}

fail() {
    # 明确返回失败，供主流程退出或恢复流程报告双重失败。
    log ERROR "$*"
    return 1
}

usage() {
    # 三个公开入口分别负责指定版本运行、停止及升级。
    printf '%s\n' '运行：sh run.sh v0.0.1' '停止：sh stop.sh' '升级：sh upgrade.sh [v0.0.2]'
}

read_kv() {
    # 只读固定键和值，不执行 TOML、JSON 或工具输出中的任何 shell 内容。
    local -n output=$1
    local key value
    while IFS='=' read -r key value || [[ -n "$key" ]]; do
        [[ "$key" =~ ^[A-Z_]+$ ]] || { fail '校验工具返回了非法字段。'; return 1; }
        output["$key"]=$value
    done < "$2"
}

file_hash() {
    # 保留 sha256sum 的真实退出码，不让管道掩盖文件读取错误。
    local result
    result=$(sha256sum -- "$1") || return 1
    printf '%s\n' "${result%% *}"
}

get_current() {
    # 名称查找使用精确匹配；所有后续操作绑定真实容器 ID 与部署归属。
    local names
    CURRENT=()
    names=$(docker container ls -a --format '{{.Names}}') || return 1
    if ! grep -Fxq -- "$CONTAINER_NAME" <<< "$names"; then return 0; fi
    docker container inspect "$CONTAINER_NAME" --format 'ID={{.Id}}
MANAGED={{index .Config.Labels "io.nnit.adp.managed"}}
ROOT={{index .Config.Labels "io.nnit.adp.release-dir"}}
VERSION={{index .Config.Labels "io.nnit.adp.version"}}
SESSION_FILE={{index .Config.Labels "io.nnit.adp.session-file"}}
CONFIG_HASH={{index .Config.Labels "io.nnit.adp.config-sha256"}}
ATTEMPT={{index .Config.Labels "io.nnit.adp.attempt"}}
IMAGE_ID={{.Image}}
RUNNING={{.State.Running}}
HEALTH={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}
BIND_IP={{with index .HostConfig.PortBindings "8000/tcp"}}{{(index . 0).HostIp}}{{end}}
HOST_PORT={{with index .HostConfig.PortBindings "8000/tcp"}}{{(index . 0).HostPort}}{{end}}' > "$WORK_DIR/current" || return 1
    read_kv CURRENT "$WORK_DIR/current" || return 1
    [[ "${CURRENT[MANAGED]:-}" == adp_channel_api && "${CURRENT[ROOT]:-}" == "$RELEASE_DIR" ]] ||
        { fail '同名容器不属于本部署目录，拒绝操作。'; return 1; }
    [[ "${CURRENT[VERSION]:-}" =~ $VERSION_PATTERN ]] ||
        { fail '现有容器缺少合法版本标签。'; return 1; }
}

stop_current() {
    # 沿用既有 240 秒宽限；不强制删除正在处理业务的容器。
    get_current || return 1
    if [[ -z "${CURRENT[ID]:-}" ]]; then log INFO '当前没有受管理的容器。'; return 0; fi
    if [[ "${CURRENT[RUNNING]}" == true ]]; then
        log INFO "优雅停止 ${CURRENT[VERSION]}。"
        docker container stop --time 240 "${CURRENT[ID]}" > /dev/null || return 1
    fi
}

append_bind_groups() {
    # 仅补充实际绑定文件/目录的属组，使10001应用可访问调用者的受限数据目录。
    local -n command_arguments=$1
    local path group
    local -A seen=([10001]=1)
    for path in "$CONFIG_FILE" "$STATE_FILE" "${PLAN[SESSION_HOST_DIR]:-}" "${PLAN[LOG_HOST_DIR]:-}"; do
        [[ -n "$path" && -e "$path" ]] || continue
        group=$(stat -c '%g' -- "$path") || return 1
        [[ "$group" =~ ^[0-9]+$ ]] || { fail '无法确定挂载路径的有效属组。'; return 1; }
        if [[ -z "${seen[$group]:-}" ]]; then
            command_arguments+=(--group-add "$group")
            seen[$group]=1
        fi
    done
}

python_tool() {
    # 使用已加载镜像的 Python 解析机器数据；宿主不依赖 Python，也不挂载 Docker socket。
    local mode=$1
    local -a arguments
    local key
    local -a environment=(
        "DEPLOY_ROOT=$RELEASE_DIR" "DEPLOY_VERSION=$VERSION" "DEPLOY_IMAGE_ID=$IMAGE_ID"
        "DEPLOY_PLATFORM=$PLATFORM" "DEPLOY_ARCHIVE_SHA=$ARCHIVE_SHA"
        "DEPLOY_CONFIG_HASH=$CONFIG_HASH" "DEPLOY_BASELINE_CID=$BASELINE_CID"
        "DEPLOY_BASELINE_SHA=$BASELINE_SHA" "DEPLOY_PREVIOUS_VERSION=$PREVIOUS_VERSION"
        "DEPLOY_HAS_CONTAINER=${CURRENT[ID]:-}" "DEPLOY_INSPECT=$INSPECT_JSON"
        "ADP_BIND_IP=${ADP_BIND_IP:-}" "ADP_HOST_PORT=${ADP_HOST_PORT:-}"
    )
    for key in BIND_IP HOST_PORT SESSION_HOST_FILE; do
        environment+=("DEPLOY_$key=${PLAN[$key]:-}")
    done
    if [[ "$mode" == attest ]]; then
        arguments=(container exec -i)
        for key in "${environment[@]}"; do arguments+=(--env "$key"); done
        arguments+=("$NEW_ID" python - "$mode")
    else
        # 只读元数据模式可读私有上传文件；不挂载可写数据，不要求宿主root。
        local helper_user=10001:10001
        if [[ "$mode" == manifest || "$mode" == cache || "$mode" == plan ]]; then helper_user=0:0; fi
        arguments=(run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges
            --user "$helper_user" --entrypoint python
            --mount "type=bind,src=$CONFIG_FILE,dst=/app/release/config.toml,readonly")
        if [[ "$helper_user" == 0:0 ]]; then
            arguments+=(--cap-add DAC_OVERRIDE)
        else
            append_bind_groups arguments || return 1
        fi
        if [[ "$mode" == manifest || "$mode" == cache ]]; then
            arguments+=(--mount "type=bind,src=$RELEASE_DIR,dst=/deploy/release,readonly")
        fi
        if [[ -f "$STATE_FILE" ]]; then
            arguments+=(--mount "type=bind,src=$STATE_FILE,dst=/deploy/state.json,readonly")
        fi
        if [[ "$helper_user" != 0:0 && -n "${PLAN[SESSION_HOST_DIR]:-}" ]]; then
            arguments+=(--mount "type=bind,src=${PLAN[SESSION_HOST_DIR]},dst=${PLAN[SESSION_CONTAINER_DIR]}")
            if [[ "${PLAN[LOG_CONTAINER_DIR]}" != "${PLAN[SESSION_CONTAINER_DIR]}" ]]; then
                arguments+=(--mount "type=bind,src=${PLAN[LOG_HOST_DIR]},dst=${PLAN[LOG_CONTAINER_DIR]}")
            fi
        fi
        for key in "${environment[@]}"; do arguments+=(--env "$key"); done
        arguments+=("$TOOL_IMAGE" - "$mode")
    fi
    # Python 正文是本文件的一部分，不要求用户额外上传帮助脚本。
    docker "${arguments[@]}" <<'PY'
# BEGIN_DEPLOY_HELPER
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import stat
import sys
import tarfile
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.config import ChatConfig, load_config
from core.logging import diagnostic_logger
from core.models import ChatError
from storage.session import load_session

ROOT = os.environ["DEPLOY_ROOT"]
VERSION = os.environ["DEPLOY_VERSION"]
CONFIG = Path("/app/release/config.toml")
STATE = Path("/deploy/state.json")
PATTERN = r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


class DeploymentError(Exception):
    """表示可向操作者公开的部署校验错误，不携带身份秘密。"""


def emit(key, value):
    """输出不可执行的单行键值，拒绝破坏 shell 读取边界的控制字符。"""
    value = str(value)
    if any(c in value for c in "\r\n\0"):
        raise DeploymentError("路径或清单字段不能包含换行或空字符。")
    print(key + "=" + value)


def digest(path):
    """流式计算文件哈希，不在诊断中输出文件正文。"""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_state():
    """读取最近成功部署，损坏的状态不能被当作首次部署忽略。"""
    try:
        metadata = STATE.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(metadata.st_mode):
        raise DeploymentError("部署状态必须是普通文件，拒绝符号链接或其他类型。")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
            or state["schema_version"] != 1 or state.get("release_dir") != ROOT
            or not re.fullmatch(PATTERN, state.get("version", ""))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", state.get("image_id", ""))
            or not state.get("session_file") or not state.get("conversation_id")
            or not re.fullmatch(r"[0-9a-f]{64}", state.get("session_sha256", ""))):
        raise DeploymentError("部署状态文件损坏，拒绝按首次部署重建会话。")
    return state


def read_manifest(version):
    """验证版本清单的身份、平台和归档名称，禁止把目录名当作全部证据。"""
    directory = Path("/deploy/release") / version
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    platform = os.environ["DEPLOY_PLATFORM"]
    archive = "adp_channel_api-" + version + "-" + platform.replace("/", "-") + ".tar"
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("version") != version
            or manifest.get("image") != "adp_channel_api:" + version
            or manifest.get("platform") != platform or manifest.get("archive") != archive
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("image_id", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("archive_sha256", ""))):
        raise DeploymentError("版本目录、清单、镜像或目标平台不一致。")
    return manifest


def verify_image_identity(manifest):
    """从已校验归档核对配置摘要，兼容两种 Docker 存储后端的 ID 表示。"""
    actual = os.environ["DEPLOY_IMAGE_ID"]
    allowed = {manifest["image_id"]}
    config_id = manifest.get("image_config_id")
    if config_id is not None:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_id):
            raise DeploymentError("清单中的镜像配置摘要无效。")
        archive = Path("/deploy/release") / VERSION / manifest["archive"]
        with tarfile.open(archive) as contents:
            entries = json.load(contents.extractfile("manifest.json"))
            if len(entries) != 1:
                raise DeploymentError("归档必须恰好包含一个目标镜像。")
            member = entries[0]["Config"]
            if not re.fullmatch(r"(?:blobs/sha256/)?[0-9a-f]{64}(?:\.json)?", member):
                raise DeploymentError("归档配置路径无效。")
            with contents.extractfile(member) as source:
                archived_id = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
            if archived_id != config_id:
                raise DeploymentError("归档配置内容与清单摘要不一致。")
        allowed.add(config_id)
    if actual not in allowed:
        raise DeploymentError("实际加载的镜像 ID 与归档身份不一致。")


def deployment_binding(raw, state):
    """仅部署入口校验监听配置，按环境、TOML、成功记录、默认值解析。"""
    settings = raw.get("deployment", {})
    if not isinstance(settings, dict) or set(settings) - {"bind_ip", "host_port"}:
        raise DeploymentError("[deployment] 必须是仅含 bind_ip、host_port 的配置表。")
    if "bind_ip" in settings:
        if not isinstance(settings["bind_ip"], str):
            raise DeploymentError("[deployment].bind_ip 必须是 IPv4 地址字符串。")
        try:
            ipaddress.IPv4Address(settings["bind_ip"])
        except ValueError:
            raise DeploymentError("[deployment].bind_ip 必须是有效的 IPv4 地址。") from None
    if "host_port" in settings and (type(settings["host_port"]) is not int
                                     or not 1 <= settings["host_port"] <= 65535):
        raise DeploymentError("[deployment].host_port 必须是 1–65535 的整数。")
    # 显式非法 TOML 不能被环境变量遮盖；旧文件缺项时才沿用历史记录。
    address = os.environ.get("ADP_BIND_IP") or settings.get("bind_ip", state.get("bind_ip", "10.102.70.82"))
    port = os.environ.get("ADP_HOST_PORT") or settings.get("host_port", state.get("host_port", 4560))
    try:
        if not isinstance(address, str):
            raise ValueError
        address = str(ipaddress.IPv4Address(address))
        if type(port) is not int and (not isinstance(port, str) or not re.fullmatch(r"[0-9]+", port)):
            raise ValueError
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise DeploymentError("部署监听地址必须是 IPv4，宿主端口必须是 1–65535 的整数。") from None
    return address, port


def paths_and_config():
    """复用正式配置校验，仅补充宿主路径与容器挂载对应关系。"""
    raw = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    state = read_state()
    binding = deployment_binding(raw, state)
    try:
        config = load_config(CONFIG)
    except ChatError as error:
        if "deployment" in raw and error.stage == "config" and error.code == "unknown_section":
            raise DeploymentError(
                "镜像与配置分组不兼容；旧镜像可能不识别 [deployment]。"
                "请先保持不含该分组的原配置完成镜像升级，再添加部署配置并对同版本执行 upgrade.sh。"
            ) from None
        raise
    result = {}
    mounts = {}
    for section, field, prefix in (
        ("session", "session_file", "SESSION"),
        ("logging", "log_file", "LOG"),
    ):
        original = raw.get(section, {}).get(field, ChatConfig.__dataclass_fields__[field].default)
        if any(c in original for c in "\r\n\0,:\\"):
            raise DeploymentError("Linux 挂载路径不能包含换行、逗号、冒号或反斜线。")
        host = posixpath.normpath(original if original.startswith("/") else posixpath.join(ROOT, original))
        target = posixpath.normpath(getattr(config, field))
        host_dir, target_dir = posixpath.dirname(host), posixpath.dirname(target)
        if host_dir in ("/", ROOT) or target_dir in ("/", "/app", "/app/release", "/deploy", "/deploy/release"):
            raise DeploymentError("日志和会话必须使用独立持久目录，不能覆盖配置或容器根目录。")
        if target_dir in mounts and mounts[target_dir] != host_dir:
            raise DeploymentError("两个宿主目录不能绑定到同一个容器目录。")
        mounts[target_dir] = host_dir
        result[prefix + "_HOST_DIR"] = host_dir
        result[prefix + "_CONTAINER_DIR"] = target_dir
        result[prefix + "_HOST_FILE"] = host
        result[prefix + "_CONTAINER_FILE"] = target
    if state and state["session_file"] != result["SESSION_HOST_FILE"]:
        raise DeploymentError("会话路径与最近成功部署不同；请先按迁移流程处理，不能静默创建新会话。")
    return config, result, state, binding


def check_session(config, state, required):
    """已有会话只读复用；升级缺失、身份不符或字节变化均失败关闭。"""
    path = Path(config.session_file)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise DeploymentError("原会话文件缺失或不是普通文件，拒绝创建新 ConversationId。")
        return "", ""
    if not stat.S_ISREG(metadata.st_mode):
        raise DeploymentError("会话必须是普通文件，拒绝符号链接或其他类型。")
    cid = load_session(config)
    sha = digest(path)
    expected_cid = os.environ.get("DEPLOY_BASELINE_CID") or state.get("conversation_id")
    expected_sha = os.environ.get("DEPLOY_BASELINE_SHA") or state.get("session_sha256")
    if (expected_cid and cid != expected_cid) or (expected_sha and sha != expected_sha):
        raise DeploymentError("会话 ID 或文件内容与升级前不一致，拒绝继续。")
    return cid, sha


def main():
    """按单一校验模式处理清单、路径、会话、运行容器与成功状态。"""
    mode = sys.argv[1]
    if mode == "cache":
        for directory in sorted(Path("/deploy/release").iterdir()):
            if (directory.is_symlink() or not directory.is_dir()
                    or not re.fullmatch(PATTERN, directory.name)
                    or tuple(map(int, directory.name[1:].split("."))) >= tuple(map(int, VERSION[1:].split(".")))
                    or directory.name in (VERSION, os.environ["DEPLOY_PREVIOUS_VERSION"])):
                continue
            try:
                manifest = read_manifest(directory.name)
                sums = (directory / "SHA256SUMS").read_text(encoding="ascii").splitlines()
                expected = [manifest["archive"], "manifest.json"]
                if len(sums) != 2:
                    continue
                for row, filename in zip(sums, expected):
                    if row != digest(directory / filename) + "  " + filename:
                        raise DeploymentError("旧版本校验失败。")
                legacy = manifest.get("image_config_id", manifest["image_id"])
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", legacy):
                    continue
                print(directory.name + "|" + manifest["image_id"] + "|" + legacy)
            except Exception:
                # 不完整/损坏的旧包不能成为删除缓存的依据。
                continue
        return
    if mode == "manifest":
        manifest = read_manifest(VERSION)
        if manifest["archive_sha256"] != os.environ["DEPLOY_ARCHIVE_SHA"]:
            raise DeploymentError("归档哈希与清单不一致。")
        verify_image_identity(manifest)
        emit("IMAGE_ID", os.environ["DEPLOY_IMAGE_ID"])
        return
    if mode == "state":
        result = {
            "schema_version": 1, "release_dir": ROOT, "version": VERSION,
            "image": "adp_channel_api:" + VERSION, "image_id": os.environ["DEPLOY_IMAGE_ID"],
            "previous_version": os.environ["DEPLOY_PREVIOUS_VERSION"],
            "session_file": os.environ["DEPLOY_SESSION_HOST_FILE"],
            "conversation_id": os.environ["DEPLOY_BASELINE_CID"],
            "session_sha256": os.environ["DEPLOY_BASELINE_SHA"],
            "config_sha256": os.environ["DEPLOY_CONFIG_HASH"],
            "bind_ip": os.environ["DEPLOY_BIND_IP"], "host_port": int(os.environ["DEPLOY_HOST_PORT"]),
            "deployed_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config, paths, state, binding = paths_and_config()
    if mode == "plan":
        for key, value in paths.items():
            emit(key, value)
        address, port = binding
        emit("BIND_IP", address)
        emit("HOST_PORT", port)
        emit("STATE_VERSION", state.get("version", ""))
        emit("STATE_PREVIOUS_VERSION", state.get("previous_version", ""))
        emit("REQUIRE_SESSION", "yes" if state or os.environ["DEPLOY_HAS_CONTAINER"] else "no")
        return
    required = bool(state or os.environ["DEPLOY_HAS_CONTAINER"] or mode == "attest")
    cid, sha = check_session(config, state, required)
    if mode == "preflight":
        directories = {Path(config.session_file).parent}
        if config.log_enabled:
            directories.add(Path(config.log_file).parent)
        for directory in directories:
            with tempfile.TemporaryFile(dir=directory):
                pass
        with diagnostic_logger(config):
            pass
    elif mode == "attest":
        inspection = json.loads(os.environ["DEPLOY_INSPECT"])
        expected = {
            "/app/release/config.toml": (ROOT + "/config.toml", False),
            paths["SESSION_CONTAINER_DIR"]: (paths["SESSION_HOST_DIR"], True),
            paths["LOG_CONTAINER_DIR"]: (paths["LOG_HOST_DIR"], True),
        }
        actual = {m["Destination"]: (m["Source"], m["RW"]) for m in inspection["Mounts"]}
        bindings = inspection["HostConfig"]["PortBindings"].get("8000/tcp", [])
        if (inspection["Image"] != os.environ["DEPLOY_IMAGE_ID"]
                or any(actual.get(target) != source for target, source in expected.items())
                or len(bindings) != 1
                or bindings[0]["HostIp"] != os.environ["DEPLOY_BIND_IP"]
                or bindings[0]["HostPort"] != os.environ["DEPLOY_HOST_PORT"]
                or os.getuid() != 10001 or os.getgid() != 10001):
            raise DeploymentError("运行镜像、真实挂载、监听参数或非 root 身份与预期不一致。")
    else:
        raise DeploymentError("未知校验模式。")
    emit("SESSION_CID", cid)
    emit("SESSION_SHA", sha)


try:
    main()
except ChatError as error:
    print("应用校验失败 [" + error.stage + "/" + error.code + "]，请检查配置、身份和会话权限。", file=sys.stderr)
    sys.exit(1)
except DeploymentError as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
except Exception as error:
    print("部署校验失败：" + type(error).__name__ + "；请检查清单、配置和权限。", file=sys.stderr)
    sys.exit(1)
# END_DEPLOY_HELPER
PY
}

check_release() {
    # 两条受限校验记录先验证文件完整性，再允许 Docker load。
    local directory="$RELEASE_DIR/$VERSION" digest filename count=0 seen_archive=0 seen_manifest=0
    local archive_name="adp_channel_api-$VERSION-${PLATFORM//\//-}.tar"
    [[ -d "$directory" && ! -L "$directory" ]] || { fail '版本目录不存在或是符号链接。'; return 1; }
    for filename in "$archive_name" manifest.json SHA256SUMS; do
        [[ -f "$directory/$filename" && ! -L "$directory/$filename" ]] ||
            { fail "版本文件缺失或不是普通文件：$filename"; return 1; }
    done
    while IFS=' ' read -r digest filename || [[ -n "$digest" ]]; do
        [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || { fail '校验和格式错误。'; return 1; }
        case "$filename" in
            "$archive_name") ((seen_archive += 1)); ARCHIVE_SHA=$digest ;;
            manifest.json) ((seen_manifest += 1)) ;;
            *) fail '校验清单包含不属于该版本的路径。'; return 1 ;;
        esac
        ((count += 1))
    done < "$directory/SHA256SUMS"
    [[ $count == 2 && $seen_archive == 1 && $seen_manifest == 1 ]] ||
        { fail '校验清单必须恰好包含镜像与 manifest。'; return 1; }
    (cd -- "$directory" && sha256sum --check --strict SHA256SUMS) || return 1
    local reference="adp_channel_api:$VERSION"
    if ! IMAGE_ID=$(docker image inspect "$reference" --format '{{.Id}}' 2>/dev/null); then
        log INFO "加载已校验的镜像 $reference。"
        docker image load --input "$directory/$archive_name" || return 1
        IMAGE_ID=$(docker image inspect "$reference" --format '{{.Id}}') || return 1
    fi
    TOOL_IMAGE=$IMAGE_ID
    python_tool manifest > "$WORK_DIR/manifest" || return 1
}

prepare_paths() {
    # 仅创建缺失目录；已有目录不递归 chown/chmod，不覆盖原配置和会话。
    python_tool plan > "$WORK_DIR/plan" || return 1
    read_kv PLAN "$WORK_DIR/plan" || return 1
    log INFO "计划发布 ${PLAN[BIND_IP]}:${PLAN[HOST_PORT]}，容器端口8000。"
    if [[ -n "${CURRENT[SESSION_FILE]:-}" && "${CURRENT[SESSION_FILE]}" != "${PLAN[SESSION_HOST_FILE]}" ]]; then
        fail '当前容器与目标配置的会话路径不同，拒绝切换。'; return 1
    fi
    local directory
    for directory in "${PLAN[SESSION_HOST_DIR]}" "${PLAN[LOG_HOST_DIR]}"; do
        if [[ -e "$directory" ]]; then
            [[ -d "$directory" && ! -L "$directory" ]] || { fail '持久路径不是普通目录。'; return 1; }
        else
            if ((EUID == 0)); then
                install -d -o 10001 -g 10001 -m 0750 -- "$directory" || return 1
            else
                # setgid让新文件继承调用者属组；不改已有目录或会话权限。
                install -d -g "$(id -g)" -m 2770 -- "$directory" || return 1
            fi
        fi
    done
    python_tool preflight > "$WORK_DIR/preflight" || return 1
    local -A checked=()
    read_kv checked "$WORK_DIR/preflight" || return 1
    BASELINE_CID="${checked[SESSION_CID]}"
    BASELINE_SHA="${checked[SESSION_SHA]}"
}

start_container() {
    # 所有入口共用这一套参数，保留现有单 worker、重启、健康和停止策略。
    local -a arguments=(run -d --pull never --name "$CONTAINER_NAME" --init --restart unless-stopped
        --stop-timeout 240
        --label io.nnit.adp.managed=adp_channel_api
        --label "io.nnit.adp.release-dir=$RELEASE_DIR"
        --label "io.nnit.adp.version=$VERSION"
        --label "io.nnit.adp.session-file=${PLAN[SESSION_HOST_FILE]}"
        --label "io.nnit.adp.config-sha256=$CONFIG_HASH"
        --label "io.nnit.adp.attempt=$ATTEMPT"
        --env ADP_CONFIG_FILE=/app/release/config.toml
        --publish "${PLAN[BIND_IP]}:${PLAN[HOST_PORT]}:8000"
        --mount "type=bind,src=$CONFIG_FILE,dst=/app/release/config.toml,readonly"
        --mount "type=bind,src=${PLAN[SESSION_HOST_DIR]},dst=${PLAN[SESSION_CONTAINER_DIR]}"
        --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()\""
        --health-interval 30s --health-timeout 5s --health-retries 3 --health-start-period 10s)
    if [[ "${PLAN[LOG_CONTAINER_DIR]}" != "${PLAN[SESSION_CONTAINER_DIR]}" ]]; then
        arguments+=(--mount "type=bind,src=${PLAN[LOG_HOST_DIR]},dst=${PLAN[LOG_CONTAINER_DIR]}")
    fi
    append_bind_groups arguments || return 1
    # 使用清单验证过的不可变 ID 启动，避免标签随后变化影响实际运行版本。
    NEW_ID=$(docker "${arguments[@]}" "$IMAGE_ID") || return 1
    log INFO "已创建 $CONTAINER_NAME，版本 $VERSION。"
}

wait_healthy() {
    # 等待真实 Docker 健康状态；提前退出和 unhealthy 立即失败。
    local deadline=$((SECONDS + 75)) status
    while ((SECONDS < deadline)); do
        status=$(docker container inspect "$NEW_ID" --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}') || return 1
        case "$status" in
            'true healthy') return 0 ;;
            'false '*|'true unhealthy'|'true none')
                docker container logs --tail 20 "$NEW_ID" >&2 || true
                fail "容器未健康运行：$status"; return 1 ;;
        esac
        sleep 1
    done
    fail '容器在启动验收窗口内未健康。'
}

attest_container() {
    # 从实际运行容器复核原会话、真实挂载和镜像身份，防止仅验证旁路 helper。
    INSPECT_JSON=$(docker container inspect "$NEW_ID" --format '{{json .}}') || return 1
    python_tool attest > "$WORK_DIR/attest" || return 1
    local -A checked=()
    read_kv checked "$WORK_DIR/attest" || return 1
    BASELINE_CID="${checked[SESSION_CID]}"
    BASELINE_SHA="${checked[SESSION_SHA]}"
}

record_success() {
    # 仅在健康和会话检查通过后，原子记录成功部署；文件不是新的应用配置。
    python_tool state > "$WORK_DIR/state.json" || return 1
    chmod 0640 "$WORK_DIR/state.json" || return 1
    mv -f -- "$WORK_DIR/state.json" "$STATE_FILE" || return 1
}

cleanup_old_cache() {
    # 成功后仅移除已验证的更早版本标签；当前、上一版本和任意容器引用均保留。
    local version source_id config_id actual references
    python_tool cache > "$WORK_DIR/cache" || { log WARNING '旧镜像缓存检查失败，保留缓存。'; return 0; }
    while IFS='|' read -r version source_id config_id; do
        actual=$(docker image inspect "adp_channel_api:$version" --format '{{.Id}}' 2>/dev/null) || continue
        [[ "$actual" == "$source_id" || "$actual" == "$config_id" ]] || continue
        references=$(docker container ls -a --quiet --filter "ancestor=$actual") || continue
        [[ -z "$references" ]] || continue
        if docker image rm "adp_channel_api:$version" > /dev/null; then
            log INFO "已移除未被容器引用的旧镜像缓存 $version；离线归档保留。"
        else
            log WARNING "旧镜像缓存 $version 未删除，不影响本次部署。"
        fi
    done < "$WORK_DIR/cache"
}

remove_attempt() {
    # 清理只针对本次尝试创建的容器，绝不按镜像名称批量停止。
    get_current || return 1
    if [[ -n "${CURRENT[ID]:-}" && "${CURRENT[ATTEMPT]:-}" == "$ATTEMPT" ]]; then
        stop_current || return 1
        docker container rm "${CURRENT[ID]}" > /dev/null || return 1
    fi
}

recover_previous() {
    # 失败恢复保留同一配置和会话；不能恢复时明确失败，不重建状态掩盖问题。
    log WARNING "目标版本未完成，恢复 $OLD_VERSION。"
    get_current || return 1
    if [[ "${CURRENT[ID]:-}" == "$OLD_ID" && "${CURRENT[RUNNING]:-}" == true ]]; then return 0; fi
    remove_attempt || return 1
    get_current || return 1
    if [[ -n "${CURRENT[ID]:-}" ]]; then
        [[ "${CURRENT[ID]}" == "$OLD_ID" ]] || { fail '恢复时发现其他容器占用名称。'; return 1; }
        stop_current || return 1
        docker container rm "$OLD_ID" > /dev/null || return 1
    fi
    VERSION=$OLD_VERSION IMAGE_ID=$OLD_IMAGE TOOL_IMAGE=$OLD_IMAGE
    PLAN[BIND_IP]=$OLD_BIND PLAN[HOST_PORT]=$OLD_PORT
    PREVIOUS_VERSION="${PLAN[STATE_PREVIOUS_VERSION]:-}"
    python_tool preflight > "$WORK_DIR/recovery" || return 1
    start_container || return 1
    wait_healthy || return 1
    attest_container || return 1
    record_success || return 1
    log WARNING '上一成功版本已恢复；本次升级仍返回失败。'
}

finish() {
    # 正常、失败和可捕获信号均释放自有临时文件；恢复失败单独报告。
    local status=$?
    trap - EXIT INT TERM HUP
    set +e
    if ((status != 0 && ROLLBACK_NEEDED && ! RECOVERING)); then
        RECOVERING=1
        recover_previous
        if (($? != 0)); then log ERROR '自动恢复未完成，请保留原会话并人工检查。'; fi
    elif ((status != 0)); then
        remove_attempt
    fi
    if [[ -n "$WORK_DIR" && "$WORK_DIR" == "$RELEASE_DIR"/.deploy-work.* ]]; then
        rm -f -- "$WORK_DIR/"{current,manifest,plan,preflight,attest,recovery,state.json,cache}
        rmdir -- "$WORK_DIR" || log WARNING "请检查自有临时目录：$WORK_DIR"
    fi
    exit "$status"
}

main() {
    # 主流程只调度已验证的生命周期，目标版本加载与会话预检始终先于停机。
    local action=run requested="${1:-}" target architecture directory candidate current_hash
    case "$requested" in
        --help|-h) usage; return 0 ;;
        --stop) action=stop; shift; requested="" ;;
        --upgrade) action=upgrade; shift; requested="${1:-}"; if (($#)); then shift; fi ;;
        *) if (($#)); then shift; fi ;;
    esac
    (($# == 0)) || { usage >&2; return 1; }
    if [[ "$action" == run && -z "$requested" ]]; then usage >&2; return 1; fi
    [[ -w "$RELEASE_DIR" ]] || { fail '当前账户需要release目录写权限，以创建部署锁和状态。'; return 1; }
    [[ "$CONTAINER_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ ]] || { fail '容器名称无效。'; return 1; }
    [[ "$RELEASE_DIR" != *[,$'\n\r']* ]] || { fail '部署根目录含有不支持的字符。'; return 1; }
    for target in docker flock sha256sum install sort stat id; do command -v "$target" >/dev/null || { fail "缺少命令：$target"; return 1; }; done
    PLATFORM=$(docker info --format '{{.OSType}}/{{.Architecture}}') ||
        { fail '当前账户无法访问Docker，请先确认docker info可直接执行。'; return 1; }
    case "$PLATFORM" in linux/x86_64|linux/amd64) PLATFORM=linux/amd64 ;; linux/aarch64|linux/arm64) PLATFORM=linux/arm64 ;; *) fail "不支持的 Docker 平台：$PLATFORM"; return 1 ;; esac
    [[ ! -L "$RELEASE_DIR/.deploy.lock" ]] || { fail '部署锁不能是符号链接。'; return 1; }
    exec 9>>"$RELEASE_DIR/.deploy.lock"
    flock -n 9 || { fail '已有运行/停止/升级操作在执行。'; return 1; }
    WORK_DIR=$(mktemp -d "$RELEASE_DIR/.deploy-work.XXXXXXXX") || return 1
    trap finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    get_current || return 1
    if [[ "$action" == stop ]]; then stop_current; return; fi
    [[ -f "$CONFIG_FILE" && ! -L "$CONFIG_FILE" ]] || { fail '请先上传 release/config.toml 普通文件。'; return 1; }
    [[ ! -L "$STATE_FILE" && ( ! -e "$STATE_FILE" || -f "$STATE_FILE" ) ]] ||
        { fail '部署状态必须是普通文件，拒绝符号链接或其他类型。'; return 1; }
    if [[ -n "$requested" ]]; then
        VERSION="v${requested#v}"
        [[ "$VERSION" =~ $VERSION_PATTERN && ${#VERSION} -le 128 ]] || { fail '版本必须是 vX.Y.Z 正式版本。'; return 1; }
    else
        VERSION=$(
            for directory in "$RELEASE_DIR"/v*; do
                candidate="${directory##*/}"
                if [[ -d "$directory" && ! -L "$directory" && "$candidate" =~ $VERSION_PATTERN ]]; then printf '%s\n' "$candidate"; fi
            done | sort -V | tail -n 1
        )
        [[ -n "$VERSION" ]] || { fail '没有可用的正式版本目录。'; return 1; }
        if [[ -n "${CURRENT[VERSION]:-}" ]]; then
            target=$(printf '%s\n' "$VERSION" "${CURRENT[VERSION]}" | sort -V | tail -n 1)
            [[ "$target" == "$VERSION" ]] || { fail '最新目录低于当前运行版本，不自动降级。'; return 1; }
        fi
    fi
    if [[ "$action" == run && "${CURRENT[RUNNING]:-}" == true && "${CURRENT[VERSION]}" != "$VERSION" ]]; then
        fail '其他版本正在运行，请使用 upgrade.sh 或先 stop.sh。'; return 1
    fi
    CONFIG_HASH=$(file_hash "$CONFIG_FILE") || return 1
    check_release || return 1
    prepare_paths || return 1
    PREVIOUS_VERSION="${PLAN[STATE_PREVIOUS_VERSION]}"
    if [[ -n "${PLAN[STATE_VERSION]}" && "${PLAN[STATE_VERSION]}" != "$VERSION" ]]; then PREVIOUS_VERSION="${PLAN[STATE_VERSION]}"; fi
    if [[ -n "${CURRENT[ID]:-}" ]]; then
        OLD_ID="${CURRENT[ID]}" OLD_IMAGE="${CURRENT[IMAGE_ID]}" OLD_VERSION="${CURRENT[VERSION]}"
        OLD_BIND="${CURRENT[BIND_IP]}" OLD_PORT="${CURRENT[HOST_PORT]}"
        if [[ "$OLD_VERSION" != "$VERSION" ]]; then PREVIOUS_VERSION=$OLD_VERSION; fi
        if [[ "${CURRENT[RUNNING]}" == true && "$OLD_VERSION" == "$VERSION" && "$OLD_IMAGE" == "$IMAGE_ID" &&
              "${CURRENT[HEALTH]}" == healthy && "${CURRENT[CONFIG_HASH]}" == "$CONFIG_HASH" &&
              "${CURRENT[BIND_IP]}" == "${PLAN[BIND_IP]}" && "${CURRENT[HOST_PORT]}" == "${PLAN[HOST_PORT]}" ]]; then
            NEW_ID=$OLD_ID
            attest_container || return 1
            record_success || return 1
            log INFO "版本 $VERSION 已健康运行，会话保持不变。"
            return 0
        fi
        if [[ "$action" == run && "${CURRENT[RUNNING]}" == true ]]; then fail '配置或健康状态已变化，请使用升级入口重新验收。'; return 1; fi
        # 切换前验证旧镜像也能读取当前配置与原会话，避免无法回退才发现不兼容。
        if [[ "$action" == upgrade ]]; then
            TOOL_IMAGE=$OLD_IMAGE
            python_tool preflight > "$WORK_DIR/recovery" || return 1
            TOOL_IMAGE=$IMAGE_ID
            ROLLBACK_NEEDED=1
        fi
        current_hash=$(file_hash "$CONFIG_FILE") || return 1
        [[ "$current_hash" == "$CONFIG_HASH" ]] || { fail '配置在预检期间变化，拒绝停机。'; return 1; }
        stop_current || return 1
        docker container rm "$OLD_ID" > /dev/null || return 1
    fi
    start_container || return 1
    wait_healthy || return 1
    attest_container || return 1
    current_hash=$(file_hash "$CONFIG_FILE") || return 1
    [[ "$current_hash" == "$CONFIG_HASH" ]] || { fail '配置在启动期间变化，拒绝记录成功。'; return 1; }
    record_success || return 1
    ROLLBACK_NEEDED=0
    cleanup_old_cache
    log INFO "版本 $VERSION 部署成功；原会话和服务器持久目录已验收。"
    # 默认保留上一成功镜像，归档文件也保留，供失败恢复和手动回退。
}

main "$@"
