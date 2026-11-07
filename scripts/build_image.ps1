# 本机版本化打包；保留 UTF-8 BOM，兼容 Windows PowerShell 5.1。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')]
    [string]$Version,
    [ValidateSet('linux/amd64', 'linux/arm64')]
    [string]$Platform = 'linux/arm64'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-BuildLog {
    # 输出带北京时间和真实调用行号的构建阶段，便于定位失败。
    param([string]$Level, [string]$Message)
    $stamp = [DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd HH:mm:ss.fff')
    Write-Host "[$stamp build_image.ps1 L$($MyInvocation.ScriptLineNumber)] [$Level] $Message"
}

function Invoke-Docker {
    # 原样传递参数数组；原生命令失败必须阻断成品发布。
    param([string[]]$ArgumentList)
    & docker @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($ArgumentList[0]) 失败，退出码 $LASTEXITCODE。"
    }
}

function Write-Utf8 {
    # 机器清单使用无 BOM 的 UTF-8 和 LF，供 Linux 直接读取。
    param([string]$Path, [string]$Content)
    [IO.File]::WriteAllText($Path, $Content.Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
}

function Get-Sha256 {
    # 流式计算大镜像校验和，避免依赖 PowerShell 5.1 的可选模块。
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

if ($Version.Length -gt 128) { throw '版本号过长。' }
if (-not $Version.StartsWith('v')) { $Version = 'v' + $Version }
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$distRoot = Join-Path $projectRoot 'dist'
$destination = Join-Path $distRoot $Version
$image = 'adp_channel_api:' + $Version
$temporaryImage = 'adp_channel_api:build-' + [Guid]::NewGuid().ToString('N')
$archiveName = 'adp_channel_api-' + $Version + '-' + $Platform.Replace('/', '-') + '.tar'
$stage = Join-Path $distRoot ('.' + $Version + '-' + [Guid]::NewGuid().ToString('N'))
$lockPath = Join-Path $distRoot ('.' + $Version + '.lock')
$lockHandle = $null
$tagCreated = $false
$published = $false
$buildStarted = $false

try {
    Get-Command docker -ErrorAction Stop > $null
    Get-Command tar -ErrorAction Stop > $null
    if ((Invoke-Docker -ArgumentList @('info', '--format', '{{.OSType}}')).Trim() -ne 'linux') {
        throw '请启动 Docker Linux 引擎后重试。'
    }
    if (Test-Path -LiteralPath $destination) { throw "版本目录已存在：$destination；请使用新版本号。" }
    $existing = @(Invoke-Docker -ArgumentList @('image', 'ls', '--quiet', '--filter', "reference=$image"))
    if (($existing -join '').Trim()) { throw "镜像标签已存在：$image；不会覆盖已有版本。" }

    New-Item -ItemType Directory -Path $distRoot -Force > $null
    # 同一工作区的同版本打包只能有一个 writer；失败不会删除别人的锁。
    $lockHandle = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    if (Test-Path -LiteralPath $destination) { throw '版本目录在构建前已被创建。' }
    New-Item -ItemType Directory -Path $stage > $null
    $builtAt = [DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-ddTHH:mm:sszzz')
    $revision = 'unknown'
    $dirty = $null
    if ((Test-Path -LiteralPath (Join-Path $projectRoot '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        $revision = (& git -C $projectRoot rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0) { throw '无法核实源码 revision。' }
        $sourceStatus = @(& git -C $projectRoot status --porcelain)
        if ($LASTEXITCODE -ne 0) { throw '无法核实源码工作区状态。' }
        $dirty = $sourceStatus.Count -gt 0
    }
    Write-BuildLog 'INFO' "构建 $image，目标 $Platform。"
    $buildStarted = $true
    Invoke-Docker -ArgumentList @(
        'build', '--platform', $Platform, '--tag', $temporaryImage,
        '--label', "org.opencontainers.image.version=$Version",
        '--label', "org.opencontainers.image.created=$builtAt",
        '--label', "org.opencontainers.image.revision=$revision", $projectRoot
    )
    $inspection = (Invoke-Docker -ArgumentList @('image', 'inspect', $temporaryImage, '--format', '{{json .}}')) | ConvertFrom-Json
    if (($inspection.Os + '/' + $inspection.Architecture) -ne $Platform) { throw '构建产物平台与指定平台不符。' }
    $imageId = [string]$inspection.Id
    if ($imageId -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Docker 未返回有效镜像 ID。' }
    # 构建期间不占用正式标签；构建成功后再次核实，避免覆盖并发创建的版本。
    $existing = @(Invoke-Docker -ArgumentList @('image', 'ls', '--quiet', '--filter', "reference=$image"))
    if (($existing -join '').Trim()) { throw '正式标签在构建期间已被其他操作创建。' }
    Invoke-Docker -ArgumentList @('image', 'tag', $temporaryImage, $image)
    $tagCreated = $true
    $archivePath = Join-Path $stage $archiveName
    Write-BuildLog 'INFO' '导出镜像并验证 tar 结构。'
    # 不通过 PowerShell 文本管道传输镜像二进制，避免 5.1 编码转换损坏归档。
    Invoke-Docker -ArgumentList @('image', 'save', '--output', $archivePath, $image)
    $members = @(& tar -tf $archivePath)
    if ($LASTEXITCODE -ne 0 -or -not ($members -match '^(manifest|index)\.json$')) { throw '导出的文件不是可识别的 Docker 镜像归档。' }
    # 兼容 classic 与 containerd：前者以配置摘要、后者可能以索引摘要作为 .Id。
    $archiveManifest = @((& tar -xOf $archivePath manifest.json) | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $archiveManifest.Count -ne 1) { throw '镜像归档必须恰好包含一个目标镜像。' }
    $configMember = [string]$archiveManifest[0].Config
    if ($configMember -notmatch '^(?:blobs/sha256/)?([0-9a-f]{64})(?:\.json)?$') { throw '归档缺少有效镜像配置摘要。' }
    $configId = 'sha256:' + $Matches[1]
    if ($configMember -notin $members) { throw '归档缺少镜像配置文件。' }
    $archiveHash = Get-Sha256 $archivePath
    $manifestPath = Join-Path $stage 'manifest.json'
    $manifest = [ordered]@{
        schema_version = 1; version = $Version; image = $image; image_id = $imageId; image_config_id = $configId
        platform = $Platform; archive = $archiveName; archive_sha256 = $archiveHash
        built_at = $builtAt; source_revision = $revision; source_dirty = $dirty
    }
    Write-Utf8 $manifestPath (($manifest | ConvertTo-Json) + "`n")
    $manifestHash = Get-Sha256 $manifestPath
    Write-Utf8 (Join-Path $stage 'SHA256SUMS') "$archiveHash  $archiveName`n$manifestHash  manifest.json`n"
    Move-Item -LiteralPath $stage -Destination $destination -ErrorAction Stop
    $published = $true
    Write-BuildLog 'INFO' "打包成功：$destination；镜像 $image，ID $imageId。"
    Write-Host '手动上传整个版本目录及 scripts/run.sh、stop.sh、upgrade.sh；正式 config.toml 由服务器本地维护。'
} catch {
    Write-BuildLog 'ERROR' $_.Exception.Message
    exit 1
} finally {
    if ($tagCreated -and -not $published) { & docker image rm $image > $null }
    if ($buildStarted) { & docker image rm $temporaryImage 2>$null > $null }
    if (Test-Path -LiteralPath $stage) {
        # 删除前核对绝对路径，清理仅限本次创建的 dist 临时目录。
        $resolvedStage = [IO.Path]::GetFullPath($stage)
        $distPrefix = [IO.Path]::GetFullPath($distRoot) + [IO.Path]::DirectorySeparatorChar
        if (-not $resolvedStage.StartsWith($distPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw '临时目录越出 dist，拒绝清理。' }
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
    if ($null -ne $lockHandle) {
        $lockHandle.Dispose()
        Remove-Item -LiteralPath $lockPath -Force
    }
}
