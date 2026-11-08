"""生成独立本机测试TOML；沿现有配置加载器，不覆盖根配置或已存在文件。"""

import argparse
import base64
import json
import os
import re
import secrets
import tomllib
from pathlib import Path

from core.config import ADP_CHAT_URL, load_config
from core.models import ChatError


def create_local_config(output, *, scope, real_adp_source=None, project_root=None):
    """只写项目忽略的runtime子目录；复制真实ADP须显式选择源，企微始终模拟。"""
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    output = Path(output).resolve()
    if (not output.is_relative_to((root / "runtime").resolve())
            or not re.fullmatch(r"wecom-local-[A-Za-z0-9_-]{1,80}", scope)):
        raise ValueError("测试配置必须在项目runtime内，scope须以wecom-local-开头")
    if output.exists():
        raise FileExistsError("已有配置不覆盖；复用它或明确选择新的独立测试目录")
    data = tomllib.loads((root / "config.example.toml").read_text(encoding="utf-8"))
    if real_adp_source is not None:
        source_path = Path(real_adp_source)
        load_config(source_path)
        source = tomllib.loads(source_path.read_text(encoding="utf-8"))
        data["adp"].update(source["adp"])
        data["chat"].update(source.get("chat", {}))
    else:
        data["adp"].update(app_key="fake-local-adp-only", user_id="legacy-local-fixture", visitor_id="", user_name="")
    data["session"]["session_file"] = "runtime/session.json"
    data["wecom"].update(corp_id="wwf483023b734c570a", agent_id=1000053, adp_application=scope,
        gateway_enabled=True, callback_token=secrets.token_hex(12),
        encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:-1], messages_dir="runtime/messages",
        adp_url=ADP_CHAT_URL if real_adp_source is not None else "http://127.0.0.1:8765/adp/v2/chat",
        notification_mode="simulated", gateway_environment="test", wecom_app_secret="")
    data["logging"].update(log_enabled=True, log_level="INFO", log_file="logs/adp_chat.log",
                           log_question=False, log_answer_chars=0)
    lines = []
    for section, values in data.items():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {json.dumps(value, ensure_ascii=False, allow_nan=False)}" for key, value in values.items())
        lines.append("")
    text = "\n".join(lines)
    tomllib.loads(text)
    output.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建，既有配置/密钥/作用域绝不自动轮换；相对路径仍由同load_config解释。
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
    load_config(output)
    return output


def main(argv=None):
    """仅生成配置，不发请求；控制台只输出路径、模式和非云端身份声明。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", required=True, help="独立测试应用逻辑名，后续两轮/重启保持不变")
    parser.add_argument("--real-adp-source", type=Path, help="显式复制本机已发布测试ADP配置；不复制企微Secret")
    args = parser.parse_args(argv)
    try:
        path = create_local_config(args.output, scope=args.scope, real_adp_source=args.real_adp_source)
        print(json.dumps({"config": str(path), "adp": "real" if args.real_adp_source else "fake",
                          "wecom": "simulated", "cloud_identity_verified": False}, ensure_ascii=False))
        return 0
    except ChatError as exc:
        print(json.dumps({"stage": exc.stage, "code": exc.code}))
    except (OSError, ValueError):
        print(json.dumps({"code": "local_setup_failed", "secrets_displayed": False}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
