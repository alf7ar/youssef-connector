# Elevated install/uninstall of the "YoussefRepair" SYSTEM scheduled task (Windows PowerShell 5.1).
# Started only by `YoussefConnector.exe --install-repair | --uninstall-repair`, which the installer launches with UAC ("runas").
#   install:   %ProgramData%\YoussefConnector (+ \repair) owned by Administrators, inheritance off, SYSTEM + Administrators full,
#              Users read/execute only; copies -Source to repair\repair.ps1; registers the task (principal SYSTEM, RunLevel Highest,
#              fixed action, no triggers, no arguments) whose security descriptor lets Users read + run it, nothing more;
#              then verifies all of that and rolls back (task + folder removed) when anything is off.
#   uninstall: removes the task and %ProgramData%\YoussefConnector. Missing task/folder = success.
# Principals are SIDs only (localized Windows names "Users"/"Administrators" differ). Exit 0 ok, 1 failed.
param(
    [ValidateSet('install', 'uninstall')][string]$Mode = 'install',
    [string]$Source = ''
)
$ErrorActionPreference = 'Stop'
$TaskName = 'YoussefRepair'
$SidSystem = New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-18'
$SidAdmins = New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'
$SidUsers = New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-545'
$TaskSddl = 'D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;BU)'
# known-folder lookup, not $env:ProgramData (the environment of an elevated process comes from the user's profile)
$Base = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'YoussefConnector'
$Dir = Join-Path $Base 'repair'
$Script = Join-Path $Dir 'repair.ps1'
$PowerShellExe = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
$TaskArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script + '"'
$WriteRights = [System.Security.AccessControl.FileSystemRights]'WriteData, AppendData, WriteExtendedAttributes, WriteAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'

function Say([string]$m) { Write-Output "repair_install: $m" }

function Get-Scheduler { $s = New-Object -ComObject Schedule.Service; $s.Connect(); $s }

function Remove-RepairTask {
    $root = (Get-Scheduler).GetFolder('\')
    try { $root.DeleteTask($TaskName, 0); Say "task $TaskName removed" } catch { Say "task $TaskName not present" }
}

function Test-Reparse([string]$p) {
    $i = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    return ($i -and ($i.Attributes -band [IO.FileAttributes]::ReparsePoint))
}

function Remove-Tree([string]$p) {
    # links: drop the link only; folders: cmd rmdir /s never follows junctions or symlinks inside the tree
    if (-not (Test-Path -LiteralPath $p)) { return }
    $i = Get-Item -LiteralPath $p -Force
    if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        if ($i.PSIsContainer) { [IO.Directory]::Delete($p, $false) } else { [IO.File]::Delete($p) }
        return
    }
    & cmd.exe /d /c rmdir /s /q "$p" | Out-Null   # no 2>&1: PS 5.1 turns native stderr into terminating errors under Stop
}

function Set-SecureAcl([string]$p, [bool]$isDir) {
    # Administrators own it; the DACL is replaced as a whole: protected (no inheritance from ProgramData, where Users may create
    # folders and become CREATOR OWNER), SYSTEM + Administrators full, Users read/execute. Folder ACEs inherit to new files
    # (last.json written later by SYSTEM is therefore Users-readable, not writable).
    & icacls.exe "$p" /setowner '*S-1-5-32-544' /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls /setowner failed ($LASTEXITCODE) on $p" }
    if ($isDir) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $inh = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $inh = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($r in @(@($SidSystem, 'FullControl'), @($SidAdmins, 'FullControl'), @($SidUsers, 'ReadAndExecute'))) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($r[0], [System.Security.AccessControl.FileSystemRights]$r[1], $inh, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)))
    }
    (Get-Item -LiteralPath $p -Force).SetAccessControl($acl)
}

function Assert-SecureAcl([string]$p) {
    if (Test-Reparse $p) { throw "$p is a reparse point" }
    $acl = Get-Acl -LiteralPath $p
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

    # 1. parent folder: refuse links, take it over (a user may have pre-created it), then lock it down
    if (Test-Reparse $Base) { Remove-Tree $Base; Say 'removed a link at the base folder' }
    if (-not (Test-Path -LiteralPath $Base)) { New-Item -ItemType Directory -Path $Base | Out-Null }
    Set-SecureAcl $Base $true

    # 2. repair folder: always start from a fresh one (anything already there may not be ours)
    if (Test-Path -LiteralPath $Dir) {
        if (Test-Reparse $Dir) { Remove-Tree $Dir }
        else {
            $old = Join-Path $Base ('repair.old-' + [guid]::NewGuid().ToString('N'))
            Rename-Item -LiteralPath $Dir -NewName (Split-Path -Leaf $old)
            Remove-Tree $old
        }
    }
    New-Item -ItemType Directory -Path $Dir | Out-Null
    Set-SecureAcl $Dir $true

    # 3. the script (new file, never written through an existing one)
    [IO.File]::WriteAllBytes($Script, $bytes)
    Set-SecureAcl $Script $false

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
    $root = $svc.GetFolder('\')
    $task = $root.RegisterTaskDefinition($TaskName, $def, 6, 'SYSTEM', $null, 5)   # 6 = CREATE_OR_UPDATE
    $task.SetSecurityDescriptor($TaskSddl, 0)
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
        Remove-RepairTask
        Remove-Tree $Base
        if (Test-Path -LiteralPath $Base) { throw "could not remove $Base" }
        Say "removed $Base"
        exit 0
    }
    Install-Repair
    exit 0
} catch {
    Say ("FAILED: " + $_.Exception.Message)
    if ($Mode -eq 'install') {
        try { Remove-RepairTask } catch { }
        try { Remove-Tree $Dir } catch { }
    }
    exit 1
}
