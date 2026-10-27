# 构建 Windows EXE（GUI + CLI）
#
# 用法：
#   .\build_exe.ps1              # 先跑测试，再打包 GUI 与 CLI
#   .\build_exe.ps1 -SkipTests   # 跳过测试，只打包
#
# 产物：
#   dist\MeetPoint AI\MeetPoint AI.exe   （GUI，单目录模式）
#   dist\meetpoint-debug.exe                               （CLI，单文件模式）

param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "未找到虚拟环境，请先执行：python -m venv .venv 然后 .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

if (-not $SkipTests) {
    Write-Host "== 运行测试 =="
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "测试未通过，终止打包"
        exit 1
    }
}

foreach ($spec in @("meetpoint-ai.spec", "meetpoint-debug.spec")) {
    if (-not (Test-Path $spec)) {
        Write-Host "缺少打包配置：$spec"
        exit 1
    }
    Write-Host "== PyInstaller：$spec =="
    & $python -m PyInstaller $spec --noconfirm
    if ($LASTEXITCODE -ne 0) {
        Write-Host "打包失败：$spec"
        exit 1
    }
}

Write-Host "== 复制运行配置到 GUI 目录 =="
$guiDir = "dist\MeetPoint AI"
if (Test-Path $guiDir) {
    foreach ($f in @(".env", ".env.example", "README.md")) {
        if (Test-Path $f) { Copy-Item $f (Join-Path $guiDir $f) -Force }
    }
    $docs = Join-Path $guiDir "docs"
    New-Item -ItemType Directory -Force $docs | Out-Null
    Get-ChildItem docs\*.md, docs\*.png -ErrorAction SilentlyContinue |
        Copy-Item -Destination $docs -Force
}

Write-Host "== 打包完成 =="
Write-Host "GUI：$guiDir\MeetPoint AI.exe"
Write-Host "CLI：dist\meetpoint-debug.exe"
Write-Host "自检：dist\meetpoint-debug.exe health --json；GUI：$guiDir\MeetPoint AI.exe --self-test"
