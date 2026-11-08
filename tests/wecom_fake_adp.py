"""本机确定性 Fake ADP：仅测试服务，不挂到正式网关，不接真实 AppKey。"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse


def event(kind, **fields):
    """生成腾讯V2同形事件，不伪造未返回的Provider Code。"""
    return (f"event: {kind}\ndata: " + json.dumps({"Type": kind, **fields}, ensure_ascii=False) + "\n\n").encode()


def create_fake_app(*, quota=0, hold=False, calls_file=None, mode="success", code=460031):
    """0名额表示不设平台业务额度；只存脱敏测试关联，不保存AppKey或正文。"""
    app = FastAPI(title="本机 Fake ADP，非生产接口")
    release = asyncio.Event()
    if not hold:
        release.set()
    state = {"active": 0, "peak": 0, "calls": [], "rejected": []}

    @app.get("/_test/status")
    async def status():
        """控制面仅属于独立Fake服务，用于有界观察与测试屏障。"""
        return state

    @app.post("/_test/release")
    async def unlock():
        """释放所有已获准请求，配额判断已在平台入口完成。"""
        release.set()
        return {"released": True}

    @app.post("/_test/hold")
    async def hold_future():
        """只给后续测试重新设屏障，不重启网络Owner或重放历史调用。"""
        release.clear()
        return {"holding_future_requests": True}

    @app.post("/adp/v2/chat")
    async def chat(request: Request):
        """只接明显测试凭据，避免本机配置切换时误向Fake提交真实密钥。"""
        payload = await request.json()
        if payload.get("AppKey") != "fake-local-adp-only":
            raise HTTPException(403, "Fake只接受文档指定的独立测试AppKey")
        call = {key: payload[key] for key in ("RequestId", "ConversationId", "UserId", "VisitorId")}
        state["calls"].append(call)
        if calls_file:
            with Path(calls_file).open("a", encoding="utf-8") as output:
                output.write(json.dumps(call) + "\n")
                output.flush()
                os.fsync(output.fileno())
        denied = quota > 0 and state["active"] >= quota
        if denied or mode == "reject":
            state["rejected"].append(call["RequestId"])

            async def reject():
                """明确应用入口QPS拒绝，模型执行期错误用其他mode另测。"""
                yield event("error", Error={"Code": code})

            return StreamingResponse(reject(), media_type="text/event-stream")
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])

        async def response():
            """保留名额直到完成/取消，支持不完整流、超时和执行后限额。"""
            try:
                yield event("request_ack", RequestAck={"Status": "success", "ConversationId": call["ConversationId"]})
                await release.wait()
                if mode == "disconnect":
                    return
                if mode == "timeout":
                    await asyncio.sleep(60)
                    return
                if mode == "model-error":
                    yield event("error", Error={"Code": code})
                    return
                yield event("response.completed", Response={"Status": "success", "ConversationId": call["ConversationId"],
                    "ExtraInfo": {"ReplyMethod": 7 if mode == "reply7" else 18}, "Messages": [{"Type": "reply", "Contents": [
                        {"Type": "text", "Text": f"Fake 回复 {call['UserId']}"}]}]})
            finally:
                state["active"] -= 1

        return StreamingResponse(response(), media_type="text/event-stream")

    return app


def main(argv=None):
    """独立本机HTTP服务，可由PowerShell/PyCharm启动；不控制正式回调鉴权。"""
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--quota", type=int, default=0)
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--calls-file", type=Path)
    parser.add_argument("--mode", choices=["success", "reject", "disconnect", "timeout", "model-error", "reply7"], default="success")
    parser.add_argument("--code", type=int, default=460031)
    args = parser.parse_args(argv)
    uvicorn.run(create_fake_app(quota=args.quota, hold=args.hold, calls_file=args.calls_file,
                               mode=args.mode, code=args.code), host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
