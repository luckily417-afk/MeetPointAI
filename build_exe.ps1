# 构建 Windows EXE

```powershell
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "未找到虚拟环境，请先执行：python -m venv .venv && .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

Write-Host "== 运行测试 =="
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Host "测试未通过，终止打包"; exit 1 }

Write-Host "== PyInstaller 打包 =="
& .\.venv\Scripts\pyinstaller.exe meetpoint.spec --noconfirm
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "== 复制运行配置 =="
foreach ($f in @(".env", ".env.example", "README.md")) {
    if (Test-Path $f) { Copy-Item $f "dist\MeetPointAI\$f" -Force }
}
New-Item -ItemType Directory -Force "dist\MeetPointAI\docs" | Out-Null
Get-ChildItem docs\*.md, docs\*.png -ErrorAction SilentlyContinue | Copy-Item -Destination "dist\MeetPointAI\docs" -Force

Write-Host "== 打包完成：dist\MeetPointAI\MeetPointAI.exe =="
Write-Host "可执行自检：dist\MeetPointAI\MeetPointAI.exe --self-test"
