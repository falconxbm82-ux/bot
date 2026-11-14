# Служба DockBot через NSSM.
# Второй способ службы не используется.
# Журнал событий Windows не пишется, поэтому pywin32 не ставится.
# На ОС, которая не Windows, скрипт завершается до вызова nssm.

[CmdletBinding()]
param(
    [string]$NssmPath = "C:\Apps\DockBot\tools\nssm.exe",
    [string]$InstallRoot = "C:\Apps\DockBot",
    [string]$PythonPath = "C:\Apps\DockBot\.venv\Scripts\python.exe",
    [string]$ServiceName = "DockBot",
    # Имя службы PostgreSQL 16 с официального установщика. В ТЗ не задано.
    [string]$PostgresService = "postgresql-x64-16",
    # Пауза перед повторным запуском, миллисекунды. Стенд, не норма ТЗ.
    [int]$RestartDelayMs = 5000
)

$ErrorActionPreference = "Stop"

function Test-WindowsHost {
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        return [bool]$IsWindows
    }
    return $true
}

if (-not (Test-WindowsHost)) {
    [Console]::Error.WriteLine("NSSM runs on Windows only. This host is not Windows, so NSSM was not started.")
    exit 2
}

if (-not (Test-Path -LiteralPath $NssmPath)) {
    [Console]::Error.WriteLine("nssm.exe was not found. The service was not installed.")
    exit 2
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    [Console]::Error.WriteLine("Python from the venv was not found. The service was not installed.")
    exit 2
}

if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$LogDir = Join-Path $InstallRoot "logs"
$BackupDir = Join-Path $InstallRoot "backups"
New-Item -ItemType Directory -Force -Path $LogDir, $BackupDir | Out-Null

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$NssmArgs)
    & $NssmPath @NssmArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Повторный запуск: снять прежнюю службу, если она уже есть. Ошибки здесь ожидаемы.
& $NssmPath stop $ServiceName confirm
& $NssmPath remove $ServiceName confirm

Invoke-Nssm install $ServiceName $PythonPath
Invoke-Nssm set $ServiceName AppParameters "-m dockbot"
Invoke-Nssm set $ServiceName AppDirectory $InstallRoot
Invoke-Nssm set $ServiceName DisplayName "DockBot"
Invoke-Nssm set $ServiceName Start SERVICE_AUTO_START
Invoke-Nssm set $ServiceName AppExit Default Restart
Invoke-Nssm set $ServiceName AppRestartDelay "$RestartDelayMs"
Invoke-Nssm set $ServiceName DependOnService $PostgresService
Invoke-Nssm set $ServiceName AppStdout (Join-Path $LogDir "service-stdout.log")
Invoke-Nssm set $ServiceName AppStderr (Join-Path $LogDir "service-stderr.log")
Invoke-Nssm set $ServiceName AppRotateFiles 1
Invoke-Nssm set $ServiceName AppRotateOnline 1
Invoke-Nssm set $ServiceName AppRotateBytes 10485760
Invoke-Nssm set $ServiceName AppEnvironmentExtra "LOG_DIR=$LogDir`nBACKUP_DIR=$BackupDir"
Invoke-Nssm start $ServiceName
exit 0
