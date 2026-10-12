# Elevated install/uninstall of the "YoussefRepair" SYSTEM scheduled task (Windows PowerShell 5.1).
# Started only by `YoussefConnector.exe --install-repair | --uninstall-repair`, which the installer launches with UAC ("runas").
#   install:   %ProgramFiles%\YoussefConnector (+ \repair): standard users cannot create anything under Program Files. A folder
#              there that is not ours (reparse point, inherited DACL, owner not Administrators/SYSTEM, or a write ACE for anyone
#              else) is renamed aside, never adopted. New folders get their DACL at creation (protected, SYSTEM + Administrators
#              full, Users read/execute, owner Administrators); repair\repair.ps1 is created new with its DACL; the task
#              (principal SYSTEM, RunLevel Highest, fixed action, no triggers, no arguments) is deleted first, then created with
#              TASK_CREATE and its security descriptor (Users read + run, nothing more) in the same call; then everything is
#              verified (owner, DACL, reparse points, hash, task action) and rolled back (repair_uninstall.ps1) when anything is off.
#   uninstall: repair_uninstall.ps1 (task, then only known file names in our folders, never recursive).
# Principals are SIDs only (localized Windows names "Users"/"Administrators" differ). No external programs. Exit 0 ok, 1 failed.
param(
    [ValidateSet('install', 'uninstall')][string]$Mode = 'install',
    [string]$Source = ''
)
$ErrorActionPreference = 'Stop'
# no user-controlled environment in the elevated process (set before any cmdlet autoloads): modules only from $PSHOME,
# no PATH lookups
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
$env:PATH = [Environment]::SystemDirectory
$TaskName = 'YoussefRepair'
$SidSystem = New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-18'
$SidAdmins = New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'
$SidUsers = New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-545'
$TaskSddl = 'D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;BU)'
# 64-bit Program Files from the (admin-only) 64-bit registry view, not from the environment, in either process bitness
$Pf = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64').OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion').GetValue('ProgramFilesDir')
$Base = Join-Path $Pf 'YoussefConnector'
$Dir = Join-Path $Base 'repair'
$Script = Join-Path $Dir 'repair.ps1'
$Uninstaller = Join-Path $PSScriptRoot 'repair_uninstall.ps1'
$PowerShellExe = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
$TaskArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script + '"'
$WriteRights = [System.Security.AccessControl.FileSystemRights]'WriteData, AppendData, WriteExtendedAttributes, WriteAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'

function Say([string]$m) { Write-Output "repair_install: $m" }

function Get-Scheduler { $s = New-Object -ComObject Schedule.Service; $s.Connect(); $s }

function Remove-RepairTask {
    $root = (Get-Scheduler).GetFolder('\')
    try { $root.DeleteTask($TaskName, 0); Say "task $TaskName removed" } catch { Say "task $TaskName not present" }
    $gone = $false
    try { $null = $root.GetTask($TaskName) } catch { $gone = $true }
    if (-not $gone) { throw "task $TaskName could not be removed" }
}

function Test-Reparse([string]$p) {
    return [bool]([IO.File]::GetAttributes($p) -band [IO.FileAttributes]::ReparsePoint)
}

function Test-There([string]$p) { return ([IO.Directory]::Exists($p) -or [IO.File]::Exists($p)) }

function New-Acl([bool]$isDir) {
    # Administrators own it; protected (nothing inherited), SYSTEM + Administrators full, Users read/execute. Folder ACEs
    # inherit to new files (last.json written later by SYSTEM is therefore Users-readable, not writable).
    if ($isDir) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $inh = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $inh = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetOwner($SidAdmins)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($r in @(@($SidSystem, 'FullControl'), @($SidAdmins, 'FullControl'), @($SidUsers, 'ReadAndExecute'))) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($r[0], [System.Security.AccessControl.FileSystemRights]$r[1], $inh, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)))
    }
    return $acl
}

function New-SecureDir([string]$p) {
    # CreateDirectory on an existing path is a silent no-op that would skip the DACL: the caller made sure it is gone
    if (Test-There $p) { throw "$p exists" }
    [void][IO.Directory]::CreateDirectory($p, (New-Acl $true))
}

function Test-Ours([string]$p) {
    # the log line goes to the host, not the pipeline: pipeline output would become part of the (then truthy) return value
    try { Assert-SecureAcl $p; return $true } catch { Say "not ours: $($_.Exception.Message)" | Out-Host; return $false }
}

function Move-Aside([string]$p) {
    # rename only (a rename never follows a link and never deletes foreign content); failure stops the install
    $new = (Split-Path -Leaf $p) + '.foreign-' + [guid]::NewGuid().ToString('N')
    Rename-Item -LiteralPath $p -NewName $new
    Say "moved aside $p -> $new"
}

function Assert-SecureAcl([string]$p) {
    if (Test-Reparse $p) { throw "$p is a reparse point" }
    if ([IO.Directory]::Exists($p)) { $acl = [IO.Directory]::GetAccessControl($p) } else { $acl = [IO.File]::GetAccessControl($p) }
    if (-not $acl.AreAccessRulesProtected) { throw "$p inherits ACEs" }
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier])
    if ($owner -ne $SidAdmins -and $owner -ne $SidSystem) { throw "$p owner is $owner" }
    foreach ($a in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        if ($a.AccessControlType -ne 'Allow') { continue }
        if ($a.IdentityReference -eq $SidSystem -or $a.IdentityReference -eq $SidAdmins) { continue }
        if ($a.FileSystemRights -band $WriteRights) { throw "$p grants write to $($a.IdentityReference): $($a.FileSystemRights)" }
    }
}

function Install-Repair {
    if (-not $Source -or -not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "source script missing: $Source" }
    $bytes = [IO.File]::ReadAllBytes($Source)

    # 1. base folder: keep it only when ours, else rename it aside (never adopt a folder we did not create)
    if ((Test-There $Base) -and -not (Test-Ours $Base)) { Move-Aside $Base }
    if (-not (Test-There $Base)) { New-SecureDir $Base }

    # 2. repair folder: drop ours (known names only, non-recursive), rename anything else aside, then create it fresh
    if (Test-There $Dir) {
        $removed = $false
        if (Test-Ours $Dir) {
            try {
                foreach ($f in 'repair.ps1', 'last.json', 'last.json.tmp') { [IO.File]::Delete((Join-Path $Dir $f)) }
                [IO.Directory]::Delete($Dir, $false)
                $removed = $true
            } catch { Say "could not empty $($Dir): $($_.Exception.Message)" }
        }
        if (-not $removed) { Move-Aside $Dir }
    }
    New-SecureDir $Dir

    # 3. the script: a new file (CreateNew) with its DACL set at creation
    $fs = New-Object IO.FileStream($Script, [IO.FileMode]::CreateNew, [System.Security.AccessControl.FileSystemRights]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, (New-Acl $false))
    try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }

    # 4. the task: SYSTEM, highest, no triggers, fixed action; Users may read + run it (GRGX), not change it
    $svc = Get-Scheduler
    $def = $svc.NewTask(0)
    $def.RegistrationInfo.Description = 'Youssef Connector: restart the Bluetooth adapter and the Bluetooth services.'
    $def.RegistrationInfo.Author = 'Youssef Connector'
    $def.Principal.UserId = 'SYSTEM'   # the account name SYSTEM is not localized (its domain is)
    $def.Principal.LogonType = 5        # TASK_LOGON_SERVICE_ACCOUNT
    $def.Principal.RunLevel = 1         # TASK_RUNLEVEL_HIGHEST
    $def.Settings.Enabled = $true
    $def.Settings.AllowDemandStart = $true
    $def.Settings.StartWhenAvailable = $false
    $def.Settings.DisallowStartIfOnBatteries = $false
    $def.Settings.StopIfGoingOnBatteries = $false
    $def.Settings.ExecutionTimeLimit = 'PT5M'
    $def.Settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
    $act = $def.Actions.Create(0)       # TASK_ACTION_EXEC
    $act.Path = $PowerShellExe
    $act.Arguments = $TaskArgs
    # never update a same-named task someone else created: delete it, then TASK_CREATE (fails if one reappeared) with the SDDL
    Remove-RepairTask
    $null = $svc.GetFolder('\').RegisterTaskDefinition($TaskName, $def, 2, 'SYSTEM', $null, 5, $TaskSddl)   # 2 = TASK_CREATE
    Say "task $TaskName registered"

    # 5. verify everything; a failed check rolls the whole install back
    foreach ($p in @($Base, $Dir, $Script)) { Assert-SecureAcl $p }
    $h1 = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
    $h2 = (Get-FileHash -LiteralPath $Script -Algorithm SHA256).Hash
    if ($h1 -ne $h2) { throw 'installed script differs from the source' }
    $extra = @(Get-ChildItem -LiteralPath $Dir -Force | Where-Object { $_.Name -ne 'repair.ps1' })
    if ($extra.Count) { throw "unexpected files in the repair folder: $($extra.Name -join ', ')" }
    $t = $svc.GetFolder('\').GetTask($TaskName)
    $x = [xml]$t.Xml
    $ns = @{ t = 'http://schemas.microsoft.com/windows/2004/02/mit/task' }
    $uid = (Select-Xml -Xml $x -Namespace $ns -XPath '//t:Principal/t:UserId').Node.InnerText
    $lvl = (Select-Xml -Xml $x -Namespace $ns -XPath '//t:Principal/t:RunLevel').Node.InnerText
    if ($uid -ne 'S-1-5-18' -and $uid -ne 'SYSTEM') { throw "task principal is $uid" }
    if ($lvl -ne 'HighestAvailable') { throw "task run level is $lvl" }
    if ($t.Definition.Actions.Count -ne 1) { throw 'task must have exactly one action' }
    $a = $t.Definition.Actions.Item(1)
    if ($a.Type -ne 0 -or $a.Path -ne $PowerShellExe -or $a.Arguments -ne $TaskArgs) { throw "task action is $($a.Path) $($a.Arguments)" }
    if ($t.Definition.Triggers.Count -ne 0) { throw 'task has triggers' }
    $sd = $t.GetSecurityDescriptor(4)   # DACL_SECURITY_INFORMATION
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor $sd
    foreach ($ace in $raw.DiscretionaryAcl) {
        if ($ace.SecurityIdentifier -eq $SidSystem -or $ace.SecurityIdentifier -eq $SidAdmins) { continue }
        # GENERIC_WRITE | GENERIC_ALL | FILE_WRITE_DATA | FILE_APPEND_DATA | DELETE | WRITE_DAC | WRITE_OWNER
        if ($ace.AceQualifier -eq 'AccessAllowed' -and ($ace.AccessMask -band 0x500D0006)) { throw "task grants write to $($ace.SecurityIdentifier): $sd" }
    }
    Say "installed $Script (sddl $sd)"
}

try {
    if ($Mode -eq 'uninstall') {
        $out = @(& $Uninstaller)
        $rc = $LASTEXITCODE
        $out | ForEach-Object { Say $_ }
        exit $rc
    }
    Install-Repair
    exit 0
} catch {
    Say ("FAILED: " + $_.Exception.Message)
    if ($Mode -eq 'install') {
        try { @(& $Uninstaller) | ForEach-Object { Say "rollback: $_" } } catch { Say "rollback: $($_.Exception.Message)" }
    }
    exit 1
}
