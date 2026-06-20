$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $root "data"
$outDir = Join-Path $root "exports"

Write-Host "==== SucyuBot 用户数据导出 ===="
Write-Host ""

if (-not (Test-Path $dataDir)) {
    Write-Host "[错误] 找不到 data 目录: $dataDir"
    Read-Host "按回车退出"
    exit 1
}

function Test-LocalPort([int]$portNumber) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $portNumber, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(250)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# 服务运行时导出可能拷到写一半的数据，先提醒停服务
if (Test-LocalPort 8787) {
    Write-Host "[警告] 本地 8787 端口在监听，服务可能正在运行。"
    Write-Host "        运行中导出可能拷到写入一半的数据，建议先停止服务再导出。"
    $ans = Read-Host "仍要继续导出吗？(y/N)"
    if ($ans -ne "y" -and $ans -ne "Y") {
        Write-Host "已取消。"
        Read-Host "按回车退出"
        exit 0
    }
}

# 要打包的核心用户数据（state + 长期记忆），外加配置（含密钥）
$files = @()
foreach ($name in @("state.json", "memory.sqlite3", "config.json")) {
    $p = Join-Path $dataDir $name
    if (Test-Path $p) {
        $files += $p
        Write-Host ("  + " + $name)
    } else {
        Write-Host ("  - " + $name + " (不存在，跳过)")
    }
}
if ($files.Count -eq 0) {
    Write-Host "[错误] data 目录里没有可导出的数据文件。"
    Read-Host "按回车退出"
    exit 1
}

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zipPath = Join-Path $outDir ("sucyubot-userdata-" + $stamp + ".zip")
Compress-Archive -Path $files -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "导出完成："
Write-Host ("  " + $zipPath)
Write-Host ""
Write-Host "------------------------------------------------------------"
Write-Host "迁移到新机器：把压缩包里的文件解压到新机器的 data\ 目录即可。"
Write-Host "（state.json 与 memory.sqlite3 必须一起迁，记忆才能对上角色。）"
Write-Host ""
Write-Host "[安全警告] 该压缩包含 telegram_bot_token、API key 和真实用户聊天记录！"
Write-Host "           切勿上传 GitHub、发到群里或任何他人可见的地方，仅用于你自己换机器迁移。"
Write-Host "------------------------------------------------------------"
Read-Host "按回车退出"
