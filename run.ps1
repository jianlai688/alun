param(
    [int]$Port = 8010,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSCommandPath
$parent = Split-Path -Parent $root
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$parent;$env:PYTHONPATH" } else { $parent }
Set-Location $parent

$candidates = @()
if ($env:ALUN_PYTHON) { $candidates += $env:ALUN_PYTHON }
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd -and $cmd.Source) { $candidates += $cmd.Source }
$bundled = "C:\Users\43030\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if (Test-Path $bundled) { $candidates += $bundled }

$py = $null
foreach ($candidate in $candidates) {
    if (-not $candidate) { continue }
    try {
        & $candidate -c "import uvicorn, fastapi" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $candidate; break }
    } catch {
        continue
    }
}

if (-not $py) {
    Write-Host "未找到可用的 Python（需要 fastapi/uvicorn）。请安装依赖，或设置 ALUN_PYTHON 指向 python.exe。" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  A论 · 论文导读智能体" -ForegroundColor DarkRed
Write-Host "  http://127.0.0.1:$Port" -ForegroundColor DarkGray
Write-Host ""

$uvArgs = @("-m", "uvicorn", "alun.app.main:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) { $uvArgs += "--reload" }
& $py @uvArgs
