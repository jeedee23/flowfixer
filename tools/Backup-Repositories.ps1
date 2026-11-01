[CmdletBinding()]
param(
    [ValidateSet('Init', 'Backup', 'Snapshots', 'Check', 'Restore', 'Schedule')]
    [string]$Action = 'Backup',
    [string]$ConfigPath = "$env:LOCALAPPDATA\RepositoryBackup\config.json",
    [string]$Snapshot,
    [string]$RestoreTarget
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$transcribing = $false

function Invoke-Restic {
    param([string[]]$Arguments)

    & $config.ResticPath --repo $config.Repository --password-file $config.PasswordFile `
        -o "rclone.program=$($config.RclonePath)" @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "restic failed with exit code $LASTEXITCODE. A partial backup is not a successful backup."
    }
}

function Test-InSource {
    param([string]$Path)

    $source = $config.SourcePath.TrimEnd([char[]]'\/')
    return $Path.Equals($source, [StringComparison]::OrdinalIgnoreCase) -or `
        $Path.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

try {
    $ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).ProviderPath
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    foreach ($field in @('SourcePath', 'PasswordFile', 'RcloneConfig', 'ResticPath', 'RclonePath')) {
        $value = $config.$field
        if ([string]::IsNullOrWhiteSpace($value) -or -not [IO.Path]::IsPathRooted($value)) {
            throw "$field must be an absolute path in the configuration."
        }
        $item = Get-Item -LiteralPath $value
        if (($field -eq 'SourcePath') -ne $item.PSIsContainer) {
            throw "$field has the wrong path type."
        }
        $config.$field = $item.FullName
    }
    if ($config.Repository -notmatch '^rclone:[A-Za-z0-9_-]+:.+$') {
        throw 'Repository must be a dedicated Drive path, for example rclone:gdrive:RepositoryBackup.'
    }
    foreach ($privatePath in @($ConfigPath, $config.PasswordFile, $config.RcloneConfig)) {
        if (Test-InSource $privatePath) {
            throw "Backup credentials and configuration must be outside SourcePath: $privatePath"
        }
    }
    if ([string]::IsNullOrWhiteSpace([IO.File]::ReadAllText($config.PasswordFile))) {
        throw 'The encryption password file must not be empty.'
    }

    $logDirectory = Join-Path (Split-Path -Parent $ConfigPath) 'logs'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    Start-Transcript -Path (Join-Path $logDirectory ("backup-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))) -Append | Out-Null
    $transcribing = $true
    $env:RCLONE_CONFIG = $config.RcloneConfig

    switch ($Action) {
        'Init' { Invoke-Restic -Arguments @('init') }
        'Backup' {
            Invoke-Restic -Arguments @('backup', '--tag', 'repository-backup', '--', $config.SourcePath)
        }
        'Snapshots' {
            Invoke-Restic -Arguments @('snapshots', '--tag', 'repository-backup')
        }
        'Check' { Invoke-Restic -Arguments @('check', '--read-data') }
        'Restore' {
            if ([string]::IsNullOrWhiteSpace($Snapshot) -or $Snapshot -notmatch '^[0-9a-fA-F]{8,64}$') {
                throw 'Specify an explicit snapshot ID from the Snapshots command.'
            }
            if ([string]::IsNullOrWhiteSpace($RestoreTarget) -or -not [IO.Path]::IsPathRooted($RestoreTarget)) {
                throw 'RestoreTarget must be an absolute path to a new directory.'
            }
            $RestoreTarget = [IO.Path]::GetFullPath($RestoreTarget)
            if ((Test-Path -LiteralPath $RestoreTarget) -or (Test-InSource $RestoreTarget)) {
                throw 'RestoreTarget must not exist and must be outside SourcePath; existing work is never overwritten.'
            }
            Invoke-Restic -Arguments @('restore', $Snapshot, '--target', $RestoreTarget, '--verify')
        }
        'Schedule' {
            if ($env:OS -ne 'Windows_NT') {
                throw 'Scheduling is only supported on Windows.'
            }
            $scriptPath = $PSCommandPath
            foreach ($path in @($scriptPath, $ConfigPath)) {
                if ($path.Contains('"')) {
                    throw 'Scheduled script and configuration paths cannot contain double quotes.'
                }
            }
            $powershell = (Get-Process -Id $PID).Path
            $arguments = '-NoProfile -NonInteractive -File "{0}" -Action Backup -ConfigPath "{1}"' -f $scriptPath, $ConfigPath
            $taskAction = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
            $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                -RepetitionInterval (New-TimeSpan -Minutes 30)
            $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Hours 0)
            $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
            Register-ScheduledTask -TaskName 'RepositoryBackup-GoogleDrive' -Action $taskAction `
                -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
            Write-Output 'Scheduled every 30 minutes while this Windows user is logged on. Existing tasks are not overwritten.'
        }
    }
    Write-Output "$Action completed successfully."
}
catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
finally {
    if ($transcribing) {
        Stop-Transcript | Out-Null
    }
}
