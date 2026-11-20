# Единое развёртывание проекта одной командой:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Опционально сразу запустить веб-интерфейс:
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Run
param(
    [switch]$Run
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> 1/4. Виртуальное окружение Python" -ForegroundColor Cyan
if (-not (Test-Path ".venv/Scripts/python.exe")) {
    py -3.12 -m venv .venv
    if (-not (Test-Path ".venv/Scripts/python.exe")) {
        python -m venv .venv
    }
}
$Python = ".venv/Scripts/python.exe"

Write-Host "==> 2/4. Зависимости Python (mcp, pydantic, streamlit)" -ForegroundColor Cyan
& $Python -m pip install --quiet -r requirements.txt

Write-Host "==> 3/4. Внешний сервер medical-mcp (npm)" -ForegroundColor Cyan
$env:PUPPETEER_SKIP_DOWNLOAD = "true"
if (-not (Test-Path "node_modules/medical-mcp/build/index.js")) {
    npm install medical-mcp --no-audit --no-fund
}

Write-Host "==> 4/4. Проверка stdio-сервера" -ForegroundColor Cyan
& $Python -c "from servers.stdio_server import mcp; print('Сервер stdio импортируется корректно')"

Write-Host ""
Write-Host "Готово. Запуск веб-интерфейса:" -ForegroundColor Green
Write-Host "    .venv/Scripts/python.exe -m streamlit run app.py"

if ($Run) {
    & $Python -m streamlit run app.py
}
