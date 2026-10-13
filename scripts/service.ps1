# 管理后台服务。通常通过根目录的 wechat-feed.cmd 调用：
#   wechat-feed.cmd setup | start | stop | restart | status | install | uninstall | logs
param([Parameter(Position = 0)][string]$Action = "status")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$link = Join-Path ([Environment]::GetFolderPath("Startup")) "wechat-feed.lnk"
$log = Join-Path $root "data\logs\wechat-feed.log"
$port = 8765

function Get-Owner {
    Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
}

function Start-Feed {
    if (Get-Owner) { Write-Host "服务已经在运行"; return }
    if (-not (Test-Path $pythonw)) { throw "找不到 $pythonw，先在项目目录执行 uv sync" }
    Start-Process -FilePath $pythonw -ArgumentList "-m", "wechat_feed", "serve" -WorkingDirectory $root -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-Owner) { Write-Host "已在后台启动：http://127.0.0.1:$port/"; return }
    }
    Write-Host "15 秒内没有起来，看日志：$log"
}

function Stop-Feed {
    $owner = Get-Owner
    if (-not $owner) { Write-Host "服务没有在运行"; return }
    Stop-Process -Id $owner -Force
    for ($i = 0; $i -lt 20 -and (Get-Owner); $i++) { Start-Sleep -Milliseconds 250 }
    Write-Host "已停止（进程 $owner）"
}

function Show-Status {
    $owner = Get-Owner
    if ($owner) {
        $process = Get-Process -Id $owner
        $memory = [math]::Round($process.WorkingSet64 / 1MB)
        Write-Host "运行中：进程 $owner，内存 $memory MB"
        try {
            $raw = Invoke-WebRequest "http://127.0.0.1:$port/api/health" -TimeoutSec 3 -UseBasicParsing
            $health = [Text.Encoding]::UTF8.GetString($raw.RawContentStream.ToArray()) | ConvertFrom-Json
            Write-Host "数据来源：$($health.source)（$($health.sourceDetail)），已存 $($health.stored) 条"
        } catch {
            Write-Host "端口在监听，但健康检查没有响应"
        }
    } else {
        Write-Host "没有运行"
    }
    if (Test-Path $link) { Write-Host "开机自启：已开启" } else { Write-Host "开机自启：未开启" }
}

function Install-Autostart {
    if (-not (Test-Path $pythonw)) { throw "找不到 $pythonw，先在项目目录执行 uv sync" }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "-m wechat_feed serve"
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = "微信群信息流本地服务"
    $shortcut.Save()
    Write-Host "已开启开机自启：$link"
}

function Invoke-Setup {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "没有找到 uv。先安装 uv，再重新打开命令行执行 wechat-feed.cmd setup："
        Write-Host '  powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        return
    }
    Push-Location $root
    try { uv sync --no-dev } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "uv sync 失败" }
    Write-Host "依赖已装好。下一步：先开 WeChatDataAnalysis 完成解密，再执行 wechat-feed.cmd install"
}

switch ($Action.ToLower()) {
    "setup" { Invoke-Setup }
    "start" { Start-Feed }
    "stop" { Stop-Feed }
    "restart" { Stop-Feed; Start-Feed }
    "status" { Show-Status }
    "install" { Install-Autostart; Start-Feed }
    "uninstall" {
        if (Test-Path $link) { Remove-Item $link; Write-Host "已取消开机自启" } else { Write-Host "本来就没有开机自启" }
    }
    "logs" {
        if (Test-Path $log) { Get-Content $log -Tail 40 -Encoding utf8 } else { Write-Host "还没有日志：$log" }
    }
    default { Write-Host "用法：wechat-feed.cmd setup | start | stop | restart | status | install | uninstall | logs" }
}
