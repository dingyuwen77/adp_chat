# 从 Windows PowerShell 调用内网 adp_chat FastAPI；本示例不自动重试。
param(
    [string]$Server = "http://10.102.70.82:4560",
    [string]$Message = "你好"
)

$ErrorActionPreference = "Stop"
$url = $Server.TrimEnd("/") + "/api/v1/chat"
$json = @{ message = $Message } | ConvertTo-Json -Compress

try {
    $result = Invoke-RestMethod -Uri $url -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 240
    Write-Output "ConversationId: $($result.conversation_id)"
    Write-Output "RequestId: $($result.request_id)"
    Write-Output $result.text
} catch {
    Write-Error "请求失败：$($_.Exception.Message)。如已发送到 ADP，请勿盲目重试。"
    exit 1
}
