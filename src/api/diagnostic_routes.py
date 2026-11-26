"""由可信内网或管理代理访问的固定目标诊断入口。"""

from typing import Literal, Self

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, model_validator

from application.diagnostics import DiagnosticReport
from wecom.models import is_employee_id


router = APIRouter()
DIAGNOSTIC_CHECK_PATH = "/api/internal/diagnostics/check"


class DiagnosticRequest(BaseModel):
    """选择诊断渠道，企微检查同时需要一个成员账号。"""

    model_config = ConfigDict(extra="forbid")
    target: Literal["adp", "wecom"]
    user_id: str | None = None

    @model_validator(mode="after")
    def validate_recipient(self) -> Self:
        """将企微请求限制为单个成员账号，ADP 使用配置中的独立测试身份。"""
        if self.target == "wecom" and not is_employee_id(self.user_id):
            raise ValueError("企微检查需要一个有效的成员 user_id。")
        if self.target == "adp" and "user_id" in self.model_fields_set:
            raise ValueError("ADP 检查只接收 target。")
        return self


@router.post(DIAGNOSTIC_CHECK_PATH, include_in_schema=False)
async def check_diagnostics(body: DiagnosticRequest, request: Request) -> DiagnosticReport:
    """每进程只运行一轮探测，拒绝并发调用以免重复发送。"""
    diagnostics = request.app.state.diagnostics
    if not diagnostics.enabled:
        raise HTTPException(404, "诊断接口未启用。")
    if diagnostics.lock.locked():
        raise HTTPException(409, "另一轮诊断正在执行。")
    async with diagnostics.lock:
        return await diagnostics.check(body.target, body.user_id)
