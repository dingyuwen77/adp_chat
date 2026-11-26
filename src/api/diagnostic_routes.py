"""运维诊断入口，仅允许鉴权后的固定探测目标。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from application.diagnostics import DiagnosticReport


router = APIRouter()
DIAGNOSTIC_CHECK_PATH = "/api/internal/diagnostics/check"


class DiagnosticRequest(BaseModel):
    """请求只选择目标，不接收待发文本、收件人或 URL。"""

    model_config = ConfigDict(extra="forbid")
    target: Literal["adp", "wecom"]


@router.post(DIAGNOSTIC_CHECK_PATH, include_in_schema=False)
async def check_diagnostics(body: DiagnosticRequest, request: Request) -> DiagnosticReport:
    """每进程只运行一轮探测，拒绝并发调用以免重复发送。"""
    diagnostics = request.app.state.diagnostics
    if not diagnostics.token:
        raise HTTPException(404, "诊断接口未启用。")
    if not diagnostics.authorized(request.headers.getlist("authorization")):
        raise HTTPException(401, "诊断令牌无效。", headers={"WWW-Authenticate": "Bearer"})
    if diagnostics.lock.locked():
        raise HTTPException(409, "另一轮诊断正在执行。")
    async with diagnostics.lock:
        return await diagnostics.check(body.target)
