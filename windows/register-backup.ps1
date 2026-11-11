# Ежедневное задание Планировщика: python -m dockbot.backup.
# Само копирование и лимит 14 копий живут в dockbot/backup.py.
# Время 02:00 — стенд, не норма ТЗ.
# На ОС, которая не Windows, скрипт завершается до вызова schtasks.

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\Apps\DockBot",
    [string]$PythonPath = "C:\Apps\DockBot\.venv\Scripts\python.exe",
    [string]$TaskName = "DockBotBackup",
    # Время суток на стенде. В ТЗ не задано.
    [string]$DailyAt = "02:00"
)

$ErrorActionPreference = "Stop"

function Test-WindowsHost {
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        return [bool]$IsWindows
    }
    return $true
}

if (-not (Test-WindowsHost)) {
    [Console]::Error.WriteLine("Task Scheduler runs on Windows only. This host is not Windows, so schtasks was not started.")
    exit 2
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    [Console]::Error.WriteLine("Python from the venv was not found. The task was not registered.")
    exit 2
}

$BackupDir = Join-Path $InstallRoot "backups"
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

# Рабочий каталог — корень установки, откуда читается .env. Пароль в команду не входит.
$command = "cmd.exe /c cd /d `"$InstallRoot`" && `"$PythonPath`" -m dockbot.backup"
schtasks.exe /Create /F /TN $TaskName /SC DAILY /ST $DailyAt /RU SYSTEM /RL HIGHEST /TR $command
exit $LASTEXITCODE
