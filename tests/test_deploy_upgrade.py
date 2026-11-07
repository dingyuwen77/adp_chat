"""执行正式 Bash 主流程；镜像操作使用受控替身，不访问 Docker 或服务器。"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def find_bash():
    """Windows 优先使用 Git 自带 Bash，避免误调用没有发行版的 WSL 入口。"""
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash")


BASH = find_bash()


@unittest.skipUnless(BASH, "本机没有可用的 Bash")
class DeploymentUpgradeTests(unittest.TestCase):
    """保护升级编排、停机前校验与实际失败后的恢复边界。"""

    def invoke_upgrade(self, *, target_valid=True, start_fails=False, old_compatible=False):
        """运行真实 main/finish/recover_previous，以文件观察部署结果和故障恢复。"""
        with tempfile.TemporaryDirectory(prefix="adp-upgrade-test-") as directory:
            root = Path(directory)
            release = root / "release"
            for name in ("release", "runtime", "log"):
                (root / name).mkdir()
            source = (ROOT / "scripts/run.sh").read_text(encoding="utf-8")
            body, invocation = source.rsplit('\nmain "$@"', 1)
            self.assertFalse(invocation.strip())
            (release / "functions.sh").write_text(body, encoding="utf-8", newline="\n")
            (release / "config.toml").write_text("[chat]\nstream=false\n", encoding="utf-8")
            fixture = r'''
source ./functions.sh
fixture_id=old-container
fixture_image=old-image
fixture_version=v0.0.7
fixture_running=true
fixture_attempt=previous
save_current() {
    # 记录替身容器的可观察状态，供宿主断言最终运行版本。
    printf '%s\n%s\n%s\n' "$fixture_version" "$fixture_running" "$fixture_id" > current-state
}
get_current() {
    # 只替换 Docker 查询边界，升级与恢复决策仍执行正式主流程。
    CURRENT=()
    if [[ -n "$fixture_id" ]]; then
        CURRENT=([ID]="$fixture_id" [IMAGE_ID]="$fixture_image" [VERSION]="$fixture_version"
            [RUNNING]="$fixture_running" [BIND_IP]=127.0.0.1 [HOST_PORT]=4560
            [SESSION_FILE]="$RELEASE_DIR/../runtime/session.json" [HEALTH]=healthy
            [CONFIG_HASH]=previous-config [ATTEMPT]="$fixture_attempt")
    fi
}
docker() {
    # 本测试不创建容器；仅提供主流程所需的引擎信息与删除动作。
    case "$1 $2" in
        "info --format") printf 'linux/arm64\n' ;;
        "container rm") fixture_id=""; fixture_running=false; save_current ;;
        *) printf 'unexpected fixture docker operation\n' >&2; return 90 ;;
    esac
}
flock() {
    # 操作锁不属于此编排回归范围；真实锁由既有部署 smoke 覆盖。
    return 0
}
check_release() {
    # 归档校验由既有打包测试覆盖，这里固定目标镜像身份。
    IMAGE_ID=target-image
    TOOL_IMAGE=target-image
}
python_tool() {
    # 仅隔离镜像内校验：旧解析器拒绝新 stream 字段，目标解析器可接受。
    case "$1" in
        plan)
            printf 'BIND_IP=127.0.0.1\nHOST_PORT=4560\nSTATE_VERSION=v0.0.7\nSTATE_PREVIOUS_VERSION=\n'
            printf 'SESSION_HOST_DIR=%s/../runtime\nSESSION_HOST_FILE=%s/../runtime/session.json\n' "$RELEASE_DIR" "$RELEASE_DIR"
            printf 'LOG_HOST_DIR=%s/../log\n' "$RELEASE_DIR"
            ;;
        preflight)
            printf '%s\n' "$TOOL_IMAGE" >> preflights
            if [[ "$TOOL_IMAGE" == old-image && "$OLD_COMPATIBLE" != 1 ]]; then
                printf 'fixture config/unknown_key\n' >&2
                return 42
            fi
            if [[ "$TOOL_IMAGE" == target-image && "$TARGET_VALID" != 1 ]]; then
                printf 'fixture target preflight rejected\n' >&2
                return 43
            fi
            printf 'SESSION_CID=fixture-session\nSESSION_SHA=fixture-sha\n'
            ;;
        *) printf 'unexpected fixture helper mode\n' >&2; return 91 ;;
    esac
}
stop_current() {
    # 记录真实停机阶段是否已经到达。
    printf '%s\n' "$fixture_id" >> stopped
    fixture_running=false
    save_current
}
start_container() {
    # 在启动边界注入失败，确保测试真实走到 finish 的恢复分支。
    if [[ "$IMAGE_ID" == target-image && "$START_FAILS" == 1 ]]; then return 44; fi
    NEW_ID="running-$VERSION"
    fixture_id=$NEW_ID
    fixture_image=$IMAGE_ID
    fixture_version=$VERSION
    fixture_running=true
    fixture_attempt=$ATTEMPT
    save_current
}
wait_healthy() {
    # 健康探针细节由现有容器 smoke 覆盖。
    return 0
}
attest_container() {
    # 挂载及会话校验由既有部署测试覆盖。
    return 0
}
record_success() {
    # 只有正式主流程确认成功后才写入可观察的成功版本。
    printf '%s\n' "$VERSION" > successful-version
}
cleanup_old_cache() {
    # 不操作镜像缓存，避免测试修改用户 Docker 状态。
    return 0
}
save_current
main --upgrade v0.0.8
'''
            (release / "fixture.sh").write_text(fixture, encoding="utf-8", newline="\n")
            result = subprocess.run(
                [BASH, "./fixture.sh"], cwd=release, capture_output=True, encoding="utf-8",
                timeout=20, env={**os.environ, "TARGET_VALID": str(int(target_valid)),
                                 "START_FAILS": str(int(start_fails)),
                                 "OLD_COMPATIBLE": str(int(old_compatible))},
            )
            files = {
                name: (release / name).read_text(encoding="utf-8").splitlines()
                if (release / name).exists() else []
                for name in ("current-state", "successful-version", "preflights", "stopped")
            }
            return result, files

    def test_new_config_can_upgrade_even_when_old_image_rejects_it(self):
        """新版本可读配置时成功切换，旧版本配置兼容性不再成为升级门槛。"""
        result, files = self.invoke_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files["current-state"], ["v0.0.8", "true", "running-v0.0.8"])
        self.assertEqual(files["successful-version"], ["v0.0.8"])
        self.assertEqual(files["preflights"], ["target-image"])

    def test_target_preflight_failure_keeps_old_container_running(self):
        """目标镜像预检失败仍发生在停机之前，不写成功状态。"""
        result, files = self.invoke_upgrade(target_valid=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(files["current-state"], ["v0.0.7", "true", "old-container"])
        self.assertEqual(files["stopped"], [])
        self.assertEqual(files["successful-version"], [])

    def test_start_failure_still_recovers_a_compatible_previous_image(self):
        """保留失败后的回退预检和恢复，升级本身仍返回失败。"""
        result, files = self.invoke_upgrade(start_fails=True, old_compatible=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(files["current-state"], ["v0.0.7", "true", "running-v0.0.7"])
        self.assertEqual(files["successful-version"], ["v0.0.7"])
        self.assertEqual(files["preflights"], ["target-image", "old-image"])
        self.assertIn("上一成功版本已恢复", result.stderr)

    def test_incompatible_recovery_is_reported_after_actual_start_failure(self):
        """旧配置不兼容只影响真实失败后的恢复，不能提前拒绝正常升级。"""
        result, files = self.invoke_upgrade(start_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(files["current-state"], ["v0.0.7", "false", ""])
        self.assertEqual(files["successful-version"], [])
        self.assertEqual(files["preflights"], ["target-image", "old-image"])
        self.assertIn("自动恢复未完成", result.stderr)


if __name__ == "__main__":
    unittest.main()
