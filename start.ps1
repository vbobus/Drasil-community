# =============================================================
# Drasil - Dev Environment Startup Script
# Starts both the FastAPI backend and Vite frontend in parallel.
# Run from the repo root: .\start.ps1
# =============================================================

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  Drasil - Starting Dev Environment  " -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# --- Backend Setup ---
$BackendDir  = Join-Path $RepoRoot "backend"
$VenvPython  = Join-Path $BackendDir "venv\Scripts\python.exe"
$VenvUvicorn = Join-Path $BackendDir "venv\Scripts\uvicorn.exe"
$VenvPip     = Join-Path $BackendDir "venv\Scripts\pip.exe"
$Requirements = Join-Path $BackendDir "requirements.txt"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[Backend] venv not found - creating it now..." -ForegroundColor Yellow
    python -m venv (Join-Path $BackendDir "venv")
    Write-Host "[Backend] Installing dependencies..." -ForegroundColor Yellow
    & $VenvPip install -r $Requirements --quiet
}

# --- Frontend Setup ---
$FrontendDir = Join-Path $RepoRoot "frontend"
$NodeModules = Join-Path $FrontendDir "node_modules"

if (-not (Test-Path $NodeModules)) {
    Write-Host "[Frontend] node_modules not found - running npm ci..." -ForegroundColor Yellow
    Push-Location $FrontendDir
    npm ci
    Pop-Location
}

# --- Start Backend (new window) ---
Write-Host "[Backend]  Starting FastAPI on http://127.0.0.1:8000 ..." -ForegroundColor Green
$backendCmd = "Set-Location '$BackendDir'; Write-Host 'Drasil Backend - FastAPI + Uvicorn' -ForegroundColor Cyan; & '$VenvUvicorn' app.main:app --reload --host 127.0.0.1 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd

# Small delay so backend starts first
Start-Sleep -Seconds 2

# --- Start Frontend (new window) ---
Write-Host "[Frontend] Starting Vite on http://localhost:5173 ..." -ForegroundColor Green
$frontendCmd = "Set-Location '$FrontendDir'; Write-Host 'Drasil Frontend - Vite + React' -ForegroundColor Cyan; npm run dev"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

Write-Host ""
Write-Host "--------------------------------------" -ForegroundColor Cyan
Write-Host "  Backend:  http://127.0.0.1:8000" -ForegroundColor White
Write-Host "  API Docs: http://127.0.0.1:8000/docs" -ForegroundColor White
Write-Host "  Frontend: http://localhost:5173" -ForegroundColor White
Write-Host "--------------------------------------" -ForegroundColor Cyan
Write-Host ""
Write-Host "Both servers are running in separate windows." -ForegroundColor Gray
Write-Host "Close those windows (or Ctrl+C inside them) to stop." -ForegroundColor Gray
Write-Host ""
