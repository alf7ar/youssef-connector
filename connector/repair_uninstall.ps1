# Elevated removal of the "YoussefRepair" SYSTEM task and %ProgramFiles%\YoussefConnector (Windows PowerShell 5.1).
# Run by repair_install.ps1 (-Mode uninstall, install rollback) and, as a fixed -EncodedCommand of System32 powershell.exe, by
# the Inno uninstaller (no user-writable exe is elevated; packaging/YoussefConnector.iss holds the encoding of the code lines
# below, comment lines and indentation stripped; tests/test_repair_setup.py keeps both in sync). Bitness-independent (64-bit
# Program Files from the 64-bit registry view). Folders are touched only when ours (not a reparse point, protected DACL, owner
# Administrators/SYSTEM, no write ACE for anyone else); then only the known file names go, never recursively. Exit 0 ok, 1 not.
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
$env:PATH = [Environment]::SystemDirectory
$rc = 0
$sv = New-Object -ComObject Schedule.Service
$sv.Connect()
try { $sv.GetFolder('\').DeleteTask('YoussefRepair', 0) } catch { }
try { $null = $sv.GetFolder('\').GetTask('YoussefRepair'); 'task YoussefRepair still present'; $rc = 1 } catch { 'task YoussefRepair absent' }
$pf = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64').OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion').GetValue('ProgramFilesDir')
$base = Join-Path $pf 'YoussefConnector'
$dir = Join-Path $base 'repair'
function Ours([string]$p) {
    if ([IO.File]::GetAttributes($p) -band [IO.FileAttributes]::ReparsePoint) { return $false }
    $a = [IO.Directory]::GetAccessControl($p)
    $o = $a.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if (-not $a.AreAccessRulesProtected -or $o -notin 'S-1-5-32-544', 'S-1-5-18') { return $false }
    foreach ($r in $a.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ("$($r.AccessControlType)" -eq 'Allow' -and $r.IdentityReference.Value -notin 'S-1-5-32-544', 'S-1-5-18' -and ([int64]$r.FileSystemRights -band 0x500D0156)) { return $false }
    }
    return $true
}
function There([string]$p) { [IO.Directory]::Exists($p) -or [IO.File]::Exists($p) }
try {
    if (There $base) {
        if (-not (Ours $base)) { throw "$base is not ours, left in place" }
        if (There $dir) {
            if (-not (Ours $dir)) { throw "$dir is not ours, left in place" }
            foreach ($f in 'repair.ps1', 'last.json', 'last.json.tmp') { [IO.File]::Delete((Join-Path $dir $f)) }
            [IO.Directory]::Delete($dir, $false)
        }
        [IO.Directory]::Delete($base, $false)
    }
    "removed $base"
} catch { 'FAILED: ' + $_.Exception.Message; $rc = 1 }
exit $rc
