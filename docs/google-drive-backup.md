# Encrypted repository backups to Google Drive (Windows)

This optional tool backs up **everything saved under your repositories folder**:
working files, hidden `.git` directories, local branches, commits, stashes,
untracked files, ignored files, and FlowFix's private workspace. It does not
commit, push, pull, or change Git history. Unsaved editor buffers are not backed
up. Files outside that folder, including external Git worktree metadata and
symlink/junction targets, are not covered; include their actual locations in a
separate backup if you use those layouts.

It uses [restic](https://restic.net/) for authenticated encryption and versioned,
deduplicated snapshots, and [rclone](https://rclone.org/drive/) for the Google
Drive API. This replaces the proposed full-archive uploads: only new data needs
uploading each time. File contents and names are encrypted before leaving the
machine. Google still sees account information, upload times, and stored sizes.
No plaintext repository contents are uploaded.

All snapshots are retained. There is **no automatic deletion or pruning**.
Check Drive quota periodically; running out of space makes backups fail.
This is backup, not two-way sync, and changes on Drive are never pulled into
your working repositories.

## 1. Install and authorize on Windows

1. Install the current Windows releases of **restic** and **rclone** from their
   official sites. Keep their executable paths handy. Windows PowerShell 5.1 or
   PowerShell 7 is required.
2. In Google Cloud Console, create a project, enable **Google Drive API**, and
   configure an OAuth consent screen and an OAuth client of type **Desktop app**.
   Follow rclone's [client ID instructions](https://rclone.org/drive/#making-your-own-client-id).
   Use your own client ID and secret rather than relying on rclone's shared client.
   For an external personal app, finish the consent setup and move it out of
   **Testing** before relying on unattended backups: refresh tokens for Drive
   scopes in Testing typically expire after seven days. Follow Google's current
   verification requirements if applicable to your account/app.
3. Create a private settings folder **outside** the repositories folder:

   ```powershell
   $state = Join-Path $env:LOCALAPPDATA 'RepositoryBackup'
   New-Item -ItemType Directory -Path $state -Force
   $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
   icacls.exe $state /inheritance:r /grant:r "${user}:(OI)(CI)F" "SYSTEM:(OI)(CI)F"
   if ($LASTEXITCODE -ne 0) { throw 'Could not protect the settings folder.' }
   ```

4. Authorize rclone interactively (replace the executable path):

   ```powershell
   & 'C:\Tools\rclone\rclone.exe' --config "$state\rclone.conf" config
   ```

   Create a remote named `gdrive`, choose `drive`, enter your OAuth client ID and
   secret, and choose the **`drive.file` scope** (only files this app creates).
   Complete browser authorization. Do not select full access, a service account,
   or a shared drive for this personal setup. Leave the root folder ID blank.
   The backup will create its own dedicated folder.

OAuth tokens and the local encryption password are secrets. Never commit or
share the settings folder. The script refuses settings/password/rclone files
located inside the source folder.

## 2. Store a recovery password and configuration

Choose a strong, unique password and keep it in a password manager accessible
after losing this Windows drive. **Without it, the backup cannot be recovered.**
For unattended execution, a copy is stored in the ACL-protected settings folder.
This protects against other ordinary Windows users, not malware running as you
or an administrator.

```powershell
$secure = Read-Host 'Backup encryption password (also save in your password manager)' -AsSecureString
$password = [System.Net.NetworkCredential]::new('', $secure).Password
[IO.File]::WriteAllText("$state\password.txt", $password, [Text.UTF8Encoding]::new($false))
Remove-Variable password, secure

@{
    SourcePath = "$env:USERPROFILE\github\repos"
    Repository = 'rclone:gdrive:RepositoryBackup'
    PasswordFile = "$state\password.txt"
    RcloneConfig = "$state\rclone.conf"
    ResticPath = 'C:\Tools\restic\restic.exe'
    RclonePath = 'C:\Tools\rclone\rclone.exe'
} | ConvertTo-Json | Set-Content -LiteralPath "$state\config.json" -Encoding UTF8
```

Change `SourcePath` to your actual Windows repositories folder and adjust the
executable paths. Everything under that folder is included: large dependencies,
build output, and secrets as well as code. Do not put other backup copies in it.
Use one dedicated restic repository per independently configured backup.

## 3. Initialize, test, then schedule

Run from your FlowFix checkout:

```powershell
.\tools\Backup-Repositories.ps1 -Action Init
.\tools\Backup-Repositories.ps1 -Action Backup
.\tools\Backup-Repositories.ps1 -Action Snapshots
.\tools\Backup-Repositories.ps1 -Action Check
```

Stop if any command fails. `Init` is only for creating a new remote repository;
do not run it again when reconnecting after a reinstall. `Check` downloads and
validates **all backup data**, so allow time and bandwidth. Each command exits
nonzero on failure, including restic's "incomplete snapshot" exit code.

Before relying on scheduling, copy a snapshot ID from `Snapshots` and test a
restore to a **new** folder outside the source:

```powershell
.\tools\Backup-Repositories.ps1 -Action Restore -Snapshot 'YOUR_SNAPSHOT_ID' `
    -RestoreTarget 'C:\RestoreTest\repositories'
```

Inspect the restored tree, find a repository's hidden `.git` directory, and run
`git -C 'PATH_TO_RESTORED_REPO' status` and `git -C 'PATH_TO_RESTORED_REPO' fsck`.
Confirm a known uncommitted file and local commit/stash are present. Restic
preserves the source hierarchy beneath the target; the target itself may not
be the repository root. Restore refuses existing targets to avoid overwriting
current work, and verifies restored file contents.

Then register the task:

```powershell
.\tools\Backup-Repositories.ps1 -Action Schedule
```

The task `RepositoryBackup-GoogleDrive` starts in about one minute and repeats
every **30 minutes while your user is logged on**, including on battery power.
It does not overlap its own running job or impose a time limit. A sleeping or
powered-off machine cannot back up; missed runs start when available. The task
uses the current PowerShell executable, script path, and configuration path.
Re-register it if you move these. Registration refuses to overwrite an existing
task; manage/remove that task explicitly through Task Scheduler if necessary.
If your execution policy blocks local scripts, review and configure your
approved script policy first; the task does not bypass it.

Daily logs are in `%LOCALAPPDATA%\RepositoryBackup\logs`. They contain local
paths and diagnostic information, not the password. Logs are retained without
automatic deletion. Check Task Scheduler's **Last Run Result** (`0` is success),
logs, and snapshot timestamps regularly. There are no email/pop-up failure
alerts. No backup is guaranteed merely because the task exists.

These are live-file backups, not Windows VSS snapshots. Avoid Git operations,
builds, and edits during an initial/test backup; concurrent changes can produce
a non-atomic view of a repository. Unreadable files cause a reported failure
even if restic stores a partial snapshot. If a run is interrupted, inspect the
error and consult restic's lock recovery instructions; do not blindly unlock an
active backup.

## Recovery after reinstalling Windows

1. Install restic/rclone again and obtain this script/documentation from GitHub
   or another independently stored copy.
2. Retrieve your encryption password and OAuth client details from safe
   off-device storage. Reauthorize the **same OAuth app** with `drive.file`
   access to the same Google account, and recreate the local configuration.
   Keep the original OAuth app/project: a different app may not see the old
   files with this limited scope. Do not revoke the app or delete its Drive
   backup folder as part of cleanup.
3. Keep `Repository` pointing at the original backup folder. Set `SourcePath`
   to a new, existing repositories folder. Run `Snapshots`, not `Init`, and
   restore an explicit snapshot into a new, separate directory.
4. Verify recovered repositories and files before moving them into place,
   then perform a new backup and re-register the task.

Google Drive is not immutable storage: account compromise or deletion can still
destroy backups. Keep a second independent backup and periodically test
recovery. Do not reinstall/reformat until an off-device backup and a restore
test have actually succeeded.

## Local tool validation

With PowerShell 7, Git, and restic on `PATH`, run:

```powershell
pwsh -NoProfile -File tests/test_backup.ps1
```

This tests command routing, spaced paths, credential placement, failure exit
codes, restore safeguards, and an encrypted backup/check/restore using a
temporary local repository, including recovery of a real local Git commit,
branch, stash, uncommitted edits, and an ignored file. It never contacts Google
or registers a task.
Native Windows scheduling and authorized Google Drive access must still be
tested on the destination Windows machine using the setup steps above.
