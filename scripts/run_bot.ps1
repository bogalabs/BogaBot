# Lanzador para el autorun de BogaBot (consola visible o Tarea Programada
# de Windows). Recibe el ambiente explícito (staging/production), setea
# BOGABOT_ENV y espeja todo a un log diario en logs\<ambiente>\, para poder
# revisar qué pasó aunque se haya cerrado la ventana.
#
# Uso:
#   .\scripts\run_bot.ps1                          # staging (default)
#   .\scripts\run_bot.ps1 -Environment production   # pide confirmación
#   .\scripts\run_bot.ps1 -Environment production -Force  # sin confirmar (autorun)

param(
    [ValidateSet("staging", "production")]
    [string]$Environment = "staging",
    [switch]$Force
)

Set-Location -Path (Join-Path $PSScriptRoot "..")

if ($Environment -eq "production" -and -not $Force) {
    $confirm = Read-Host "Vas a correr BogaBot en PRODUCCION. Escribi 'si' para confirmar"
    if ($confirm -ne "si") {
        Write-Host "Cancelado." -ForegroundColor Yellow
        exit 1
    }
}

$env:BOGABOT_ENV = $Environment

$logDir = Join-Path "logs" $Environment
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir ("bot_{0:yyyy-MM-dd}.log" -f (Get-Date))

Write-Host "=== BogaBot === ambiente: $($Environment.ToUpper())  iniciando $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
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
