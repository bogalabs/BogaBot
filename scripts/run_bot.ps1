# Lanzador para el autorun de BogaBot (Tarea Programada de Windows).
# Corre el bot con la consola visible y además espeja todo a un log diario
# en logs\, para poder revisar qué pasó aunque se haya cerrado la ventana.

Set-Location -Path (Join-Path $PSScriptRoot "..")

$logDir = "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir ("bot_{0:yyyy-MM-dd}.log" -f (Get-Date))

Write-Host "=== BogaBot === iniciando $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "Log: $logFile"
Write-Host ""

try {
    & ".\venv\Scripts\python.exe" "run.py" 2>&1 | Tee-Object -FilePath $logFile -Append
}
catch {
    Write-Host "Error al arrancar el bot: $_" -ForegroundColor Red
}
finally {
    Write-Host ""
    Write-Host "El bot se detuvo. Log completo en $logFile" -ForegroundColor Yellow
    Read-Host "Presioná Enter para cerrar esta ventana"
}
