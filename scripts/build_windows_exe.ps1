#!/usr/bin/env pwsh
# 构建 Windows x64 单文件发布程序。

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ($env:PYTHON) {
  $Python = $env:PYTHON
} else {
  $candidates = @(
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path (Split-Path -Parent $ProjectRoot) ".venv\Scripts\python.exe")
  )
  $Python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Python -or -not (Test-Path $Python)) {
  Write-Error "未找到 Python。请先激活 venv，或设置 PYTHON=D:\path\to\python.exe"
  exit 2
}

& $Python -m pip install --disable-pip-version-check -q -e ".[tui]" "pyinstaller>=6.10"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --console `
  --name "conti-agent" `
  --specpath "build-release" `
  --workpath "build-release" `
  --distpath "dist" `
  --paths "src" `
  --collect-all "prompt_toolkit" `
  "scripts/exe_entry.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "发布文件：$ProjectRoot\dist\conti-agent.exe（单文件，可任意目录分发）"
Write-Host "配置来源：~\.conti-agent\config.toml（全局）或工作目录 .conti\config.toml（项目覆盖）"
