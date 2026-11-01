# Run with PowerShell 7: pwsh -NoProfile -File tests/test_backup.ps1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ("repository-backup-test-" + [guid]::NewGuid())
$script = Join-Path $PSScriptRoot '../tools/Backup-Repositories.ps1'
$stub = (Resolve-Path (Join-Path $PSScriptRoot 'fixtures/restic-stub.ps1')).ProviderPath
$pwsh = (Get-Process -Id $PID).Path

function Assert {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Run-Backup {
    param([string[]]$Arguments, [int]$ExpectedExit = 0)
    $output = & $pwsh -NoProfile -File $script -ConfigPath $configPath @Arguments 2>&1
    Assert ($LASTEXITCODE -eq $ExpectedExit) "Unexpected script exit code: $LASTEXITCODE. $output"
    return $output
}

function Read-Call {
    return @(Get-Content -LiteralPath $env:BACKUP_TEST_CALL -Raw | ConvertFrom-Json)
}

function Invoke-TestGit {
    param([string]$Directory, [string[]]$Arguments)
    $output = & git -C $Directory @Arguments 2>&1
    Assert ($LASTEXITCODE -eq 0) "Git test setup/verification failed: $output"
    return $output
}

try {
    $source = Join-Path $root 'repos with spaces'
    $state = Join-Path $root 'private settings'
    New-Item -ItemType Directory -Path $source, $state | Out-Null
    $password = Join-Path $state 'password.txt'
    $rcloneConfig = Join-Path $state 'rclone.conf'
    $configPath = Join-Path $state 'config.json'
    Set-Content -LiteralPath $password -Value 'test-only-password'
    Set-Content -LiteralPath $rcloneConfig -Value 'test-only-config'
    $config = @{
        SourcePath = $source
        PasswordFile = $password
        RcloneConfig = $rcloneConfig
        ResticPath = $stub
        RclonePath = $stub
        Repository = 'rclone:gdrive:RepositoryBackup'
    }
    $config | ConvertTo-Json | Set-Content -LiteralPath $configPath
    $env:BACKUP_TEST_CALL = Join-Path $state 'call.json'
    $env:BACKUP_TEST_EXIT = '0'

    Run-Backup @('-Action', 'Init') | Out-Null
    Assert ((Read-Call)[-1] -eq 'init') 'Init must explicitly initialize.'
    Run-Backup @('-Action', 'Backup') | Out-Null
    $call = Read-Call
    Assert ($call[-1] -eq $source) 'Paths with spaces must be passed as one argument.'
    Assert ($call -contains 'backup') 'Backup command missing.'
    Assert ($call -contains '--') 'Source must be separated from options.'
    Assert (-not ($call -contains '--exclude')) 'No files should be excluded.'
    Assert ($call -contains $password) 'Password must be supplied through a file.'
    Assert (-not ($call -contains 'test-only-password')) 'Password must not appear in arguments.'
    Run-Backup @('-Action', 'Check') | Out-Null
    Assert ((Read-Call) -contains '--read-data') 'Check must validate stored data.'
    Run-Backup @('-Action', 'Snapshots') | Out-Null
    Assert ((Read-Call) -contains 'snapshots') 'Snapshots command missing.'

    $target = Join-Path $root 'new restore'
    Run-Backup @('-Action', 'Restore', '-Snapshot', 'abcdef12', '-RestoreTarget', $target) | Out-Null
    Assert ((Read-Call) -contains '--verify') 'Restore must verify recovered contents.'
    Run-Backup @('-Action', 'Restore', '-Snapshot', 'latest', '-RestoreTarget', $target) 1 | Out-Null
    Run-Backup @('-Action', 'Restore', '-Snapshot', 'abcdef12', '-RestoreTarget', $source) 1 | Out-Null
    Run-Backup @('-Action', 'Restore', '-Snapshot', 'abcdef12', '-RestoreTarget', (Join-Path $source 'new')) 1 | Out-Null

    foreach ($code in @('1', '3', '10', '11', '12')) {
        $env:BACKUP_TEST_EXIT = $code
        Run-Backup @('-Action', 'Backup') 1 | Out-Null
    }
    $env:BACKUP_TEST_EXIT = '0'
    $config.PasswordFile = Join-Path $source 'secret.txt'
    Set-Content -LiteralPath $config.PasswordFile -Value 'test-only-password'
    $config | ConvertTo-Json | Set-Content -LiteralPath $configPath
    Run-Backup @('-Action', 'Backup') 1 | Out-Null
    $config.PasswordFile = $password
    $config.Repository = 'local-repository'
    $config | ConvertTo-Json | Set-Content -LiteralPath $configPath
    Run-Backup @('-Action', 'Backup') 1 | Out-Null

    # Verify the real encryption/restore engine without contacting Google.
    $restic = (Get-Command restic -CommandType Application | Select-Object -First 1).Source
    $repository = Join-Path $root 'encrypted repository'
    Invoke-TestGit $source @('init', '--initial-branch=main') | Out-Null
    Set-Content -LiteralPath (Join-Path $source 'tracked.txt') -Value 'original commit'
    Invoke-TestGit $source @('add', 'tracked.txt') | Out-Null
    Invoke-TestGit $source @('-c', 'user.name=Backup Test', '-c', 'user.email=test@example.invalid', 'commit',
        '-m', 'Local test commit', '-m', 'Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>') | Out-Null
    Invoke-TestGit $source @('branch', 'local-only') | Out-Null
    Set-Content -LiteralPath (Join-Path $source 'tracked.txt') -Value 'stashed change'
    Invoke-TestGit $source @('stash', 'push', '-m', 'local test stash') | Out-Null
    Set-Content -LiteralPath (Join-Path $source 'tracked.txt') -Value 'uncommitted tracked change'
    Set-Content -LiteralPath (Join-Path $source 'uncommitted.txt') -Value 'saved local changes'
    Set-Content -LiteralPath (Join-Path $source '.env') -Value 'test-only-ignored-file'
    Set-Content -LiteralPath (Join-Path $source '.gitignore') -Value '.env'
    & $restic --repo $repository --password-file $password init
    Assert ($LASTEXITCODE -eq 0) 'Real restic init failed.'
    & $restic --repo $repository --password-file $password backup --tag repository-backup -- $source
    Assert ($LASTEXITCODE -eq 0) 'Real restic backup failed.'
    & $restic --repo $repository --password-file $password check --read-data
    Assert ($LASTEXITCODE -eq 0) 'Real restic integrity check failed.'
    & $restic --repo $repository --password-file $password restore latest --target $target --verify
    Assert ($LASTEXITCODE -eq 0) 'Real restic restore failed.'
    $marker = @(Get-ChildItem -LiteralPath $target -Recurse -Force -File | Where-Object Name -EQ 'uncommitted.txt')
    Assert ($marker.Count -eq 1) 'Restore is missing the uncommitted file.'
    $restoredSource = $marker[0].DirectoryName
    foreach ($name in @('.git/HEAD', 'tracked.txt', 'uncommitted.txt', '.env')) {
        $original = Join-Path $source $name
        $restored = Join-Path $restoredSource $name
        Assert ((Get-FileHash -LiteralPath $original).Hash -eq (Get-FileHash -LiteralPath $restored).Hash) "Restored $name differs."
    }
    Invoke-TestGit $restoredSource @('fsck', '--full') | Out-Null
    $originalHead = Invoke-TestGit $source @('rev-parse', 'HEAD')
    Assert ((Invoke-TestGit $restoredSource @('rev-parse', 'HEAD')) -eq $originalHead) 'Local commit was not restored.'
    Assert ((Invoke-TestGit $restoredSource @('branch', '--list', 'local-only')) -match 'local-only') 'Local branch was not restored.'
    Assert ((Invoke-TestGit $restoredSource @('stash', 'list')) -match 'local test stash') 'Stash was not restored.'
    Write-Output 'All backup wrapper and local encrypted restore tests passed.'
}
finally {
    Remove-Item Env:BACKUP_TEST_CALL, Env:BACKUP_TEST_EXIT -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $root) {
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
