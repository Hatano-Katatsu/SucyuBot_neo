$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 8787
$url = "http://127.0.0.1:$port/"
$logDir = Join-Path $root "logs"
$launchLog = Join-Path $logDir "service.launch.log"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-LaunchLog([string]$message) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Encoding UTF8 -Path $launchLog -Value "$stamp $message"
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

function Open-WebUi {
  try {
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $url
    $psi.UseShellExecute = $true
    [System.Diagnostics.Process]::Start($psi) | Out-Null
  } catch {
    Write-LaunchLog "open browser failed: $($_.Exception.Message)"
  }
}

function Resolve-Python {
  $bundled = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if (Test-Path -LiteralPath $bundled) {
    return @{ File = $bundled; PrefixArgs = @() }
  }
  $py = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($py) {
    return @{ File = $py.Source; PrefixArgs = @("-3") }
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($python) {
    return @{ File = $python.Source; PrefixArgs = @() }
  }
  throw "No Python runtime found."
}

if (Test-LocalPort $port) {
  Write-LaunchLog "service already listening on $port"
  Open-WebUi
  exit 0
}

$python = Resolve-Python
$args = @($python.PrefixArgs) + @(
  "-m", "telegram_comfyui_selfie",
  "--config", "data/config.json",
  "--state", "data/state.json",
  "--web-port", "$port"
)

$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $python.File
$psi.Arguments = ($args | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join " "
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true

$proc = [System.Diagnostics.Process]::Start($psi)
Write-LaunchLog "started service pid=$($proc.Id)"

$deadline = (Get-Date).AddSeconds(12)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 500
  if (Test-LocalPort $port) {
    Write-LaunchLog "service is listening on $port"
    Open-WebUi
    exit 0
  }
}

Write-LaunchLog "service did not open port $port within timeout"
Open-WebUi
