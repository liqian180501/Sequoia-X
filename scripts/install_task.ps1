<#
.SYNOPSIS
    注册 / 卸载 Sequoia-X 的每日选股定时任务（Windows 任务计划程序）。

.DESCRIPTION
    把"每个交易日收盘后跑一次"固化成系统任务，避免每天手动执行。
    默认工作日 15:30 触发（A 股 15:00 收盘，留 30 分钟给数据源落地）。

    任务指向 scripts/run_daily.py（而不是 main.py），因为那一层带健康检查与失败告警。

.EXAMPLE
    .\scripts\install_task.ps1
    .\scripts\install_task.ps1 -Time 16:10
    .\scripts\install_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$TaskName = "Sequoia-X 每日选股",
    [string]$Time = "15:30",
    [string]$Python,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not $Python) {
    $candidate = Join-Path $root ".venv\Scripts\python.exe"
    $Python = if (Test-Path $candidate) { $candidate } else { "python" }
}

if ($Unregister) {
    schtasks /Delete /TN $TaskName /F 2>&1 | Out-Null
    Write-Host "已删除任务：$TaskName"
    exit 0
}

$script = Join-Path $root "scripts\run_daily.py"
if (-not (Test-Path $script)) { throw "找不到 $script" }
if (-not (Test-Path $Python)) { throw "找不到 Python：$Python" }

# 日志目录：任务计划程序不会把 stdout 给你，必须自己重定向
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

$args = "`"$script`""
$tr = "`"$Python`" $args >> `"$logDir\daily.log`" 2>&1"
if ($env:PYTHONIOENCODING -ne "utf-8") { $tr = "cmd /c set PYTHONIOENCODING=utf-8 && $tr" }

Write-Host "注册任务：$TaskName"
Write-Host "  程序：$Python"
Write-Host "  参数：$script"
Write-Host "  时间：每周一至周五 $Time"
Write-Host "  日志：$logDir\daily.log"

schtasks /Create `
    /TN $TaskName `
    /TR $tr `
    /SC WEEKLY `
    /D MON,TUE,WED,THU,FRI `
    /ST $Time `
    /F | Out-Null

if ($LASTEXITCODE -ne 0) { throw "注册失败，请以管理员身份运行或检查任务计划程序" }

Write-Host "`n完成。常用操作：" -ForegroundColor Green
Write-Host "  立即试跑： schtasks /Run /TN `"$TaskName`""
Write-Host "  查看状态： schtasks /Query /TN `"$TaskName`" /V /FO LIST"
Write-Host "  删除任务： .\scripts\install_task.ps1 -Unregister"
Write-Host "`n注意：15:30 只是数据源通常已更新的时刻，法定节假日也会触发——"
Write-Host "      数据不新鲜时 run_daily.py 会中止并告警，不会推送过期数据。"
