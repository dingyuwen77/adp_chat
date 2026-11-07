"""显式验证真实Linux会话权限：应用10001创建，部署用户1000直接读取。"""

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from uuid import uuid4


def inside():
    """只在隔离容器操作fixture，保留真实UID/GID、文件系统和正式存储入口。"""
    loader = (
        "from core.config import ChatConfig;from storage.session import load_session;import sys;"
        "c=ChatConfig('permission-fixture','fixture-user',log_enabled=False,session_file=sys.argv[1]);"
        "print(load_session(c,new='new' in sys.argv[2:]))"
    )

    def as_user(code, *arguments, uid=10001, check=True):
        """以真实Linux进程凭据执行，不用chmod/Mock伪造部署用户读权限。"""
        result = subprocess.run([sys.executable, "-c", code, *map(str, arguments)],
                                user=uid, group=uid, extra_groups=[1000],
                                capture_output=True, timeout=20)
        if check and result.returncode:
            print(result.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
            result.check_returncode()
        return result

    with tempfile.TemporaryDirectory(prefix="adp-session-permission-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        data = root / "runtime"
        data.mkdir()
        os.chown(data, 1000, 1000)
        data.chmod(0o770)  # 故意不设setgid，验证应用显式匹配可加入的目录组。
        session = data / "session.json"

        def assert_readable():
            """核对真实元数据、宿主身份读取及组成员无文件写权限。"""
            metadata = session.stat()
            assert stat.S_IMODE(metadata.st_mode) == 0o640, oct(stat.S_IMODE(metadata.st_mode))
            assert metadata.st_uid == 10001 and metadata.st_gid == 1000
            result = as_user("from pathlib import Path;import sys;sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())",
                             session, uid=1000)
            assert result.stdout == session.read_bytes()
            denial = as_user("import sys;open(sys.argv[1],'a').close()", session, uid=1000, check=False)
            assert denial.returncode != 0 and b"PermissionError" in denial.stderr

        first = as_user(loader, session).stdout.decode().strip()
        original = session.read_bytes()
        assert json.loads(original)["conversation_id"] == first
        assert_readable()
        print("New session: UID10001 owns 640/group1000; UID1000 directly reads, cannot write PASS", flush=True)

        # 原有600且属组为10001的会话，只改元数据，绝不生成新CID或重写JSON。
        os.chown(session, 10001, 10001)
        session.chmod(0o600)
        assert as_user(loader, session).stdout.decode().strip() == first
        assert session.read_bytes() == original
        assert_readable()
        print("Legacy600 session: group/read repaired; original CID and bytes preserved PASS", flush=True)

        # 无效数据/身份先失败，不因读取而先开放权限。
        for code, content, expected in (
            (loader.replace("permission-fixture", "wrong-fixture"), original, b"ChatError"),
            (loader, b"not-json", b"ChatError"),
        ):
            session.write_bytes(content)
            os.chown(session, 10001, 10001)
            session.chmod(0o600)
            result = as_user(code, session, check=False)
            assert result.returncode != 0 and expected in result.stderr
            assert session.read_bytes() == content
            assert stat.S_IMODE(session.stat().st_mode) == 0o600 and session.stat().st_gid == 10001
        session.write_bytes(original)
        as_user(loader, session)

        # 已可读的外部属主文件不夺权，部署用户仍保留原所有权。
        os.chown(session, 1000, 1000)
        before = session.stat()
        assert as_user(loader, session).stdout.decode().strip() == first
        after = session.stat()
        assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
        assert session.read_bytes() == original

        os.chown(session, 10001, 1000)
        # 新文件权限设置失败时不能提交或覆盖旧会话。
        fail_save = loader.replace(
            "print(load_session(c,new='new' in sys.argv[2:]))",
            "from unittest.mock import patch\nwith patch('storage.session.os.chmod',side_effect=PermissionError): load_session(c,new=True)"
        )
        failed = as_user(fail_save, session, check=False)
        assert failed.returncode != 0 and b"ChatError" in failed.stderr
        assert session.read_bytes() == original and list(data.iterdir()) == [session]
        second = as_user(loader, session, "new").stdout.decode().strip()
        assert second != first
        assert_readable()
        print("Invalid/foreign owner and failed atomic save guards; explicit new remains readable PASS", flush=True)


def main():
    """从本机Docker运行正式镜像内的隔离角色验收，结束移除唯一测试容器。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    parser.add_argument("--inside", action="store_true")
    arguments = parser.parse_args()
    if arguments.inside:
        inside()
        return
    if not arguments.image:
        parser.error("需要 --image")
    name = "adp-session-permissions-" + uuid4().hex
    try:
        subprocess.run(["docker", "create", "--name", name, "--network", "none", "--user", "0:0",
                        "--entrypoint", "python", arguments.image, "/tmp/session_permissions_smoke.py", "--inside"],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "cp", str(Path(__file__).resolve()), name + ":/tmp/session_permissions_smoke.py"],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["docker", "start", "--attach", name], check=True, timeout=180)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
    print("Session permissions smoke PASS; fixture/container removed; no ADP request sent.")


if __name__ == "__main__":
    main()
