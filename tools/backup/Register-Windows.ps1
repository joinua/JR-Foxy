param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Tools,
    [ValidateSet('Prepare','Enable','Pause')][string]$Mode = 'Prepare'
)
$ErrorActionPreference = 'Stop'
$TaskName = 'JR-Foxy Backup'
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
if ($Mode -eq 'Pause') {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Output 'WINDOWS_SCHEDULE_PAUSED'
    exit 0
}
if ($Mode -eq 'Prepare') {
    # A real Start menu shortcut carries the registered AppUserModelID.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

public static class FoxyShortcut {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink {}

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
    private interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int max, IntPtr data, uint flags);
        void GetIDList(out IntPtr list);
        void SetIDList(IntPtr list);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int max);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int max);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int max);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int show);
        void SetShowCmd(int show);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int max, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr hwnd, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }
    [StructLayout(LayoutKind.Sequential, Pack=4)]
    private struct PropertyKey { public Guid format; public uint id; }
    [StructLayout(LayoutKind.Explicit, Size=24)]
    private struct PropVariant {
        [FieldOffset(0)] public ushort kind;
        [FieldOffset(8)] public IntPtr value;
    }
    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    private interface IPropertyStore {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }
    public static void Save(string shortcut, string executable, string args, string directory) {
        object obj = new ShellLink();
        IShellLinkW link = (IShellLinkW)obj;
        link.SetPath(executable);
        link.SetArguments(args);
        link.SetWorkingDirectory(directory);
        link.SetDescription("JR-Foxy Backup");
        var key = new PropertyKey { format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), id = 5 };
        var value = new PropVariant { kind = 31, value = Marshal.StringToCoTaskMemUni("JokerRecon.JRFoxy.Backup") };
        try {
            IPropertyStore store = (IPropertyStore)obj;
            store.SetValue(ref key, ref value);
            store.Commit();
            ((IPersistFile)obj).Save(shortcut, true);
        } finally {
            Marshal.FreeCoTaskMem(value.value);
            Marshal.FinalReleaseComObject(obj);
        }
    }
}
'@
    $Programs = [Environment]::GetFolderPath('Programs')
    $Shortcut = Join-Path $Programs 'JR-Foxy Backup.lnk'
    $StatusScript = Join-Path $Tools 'Status.ps1'
    $PowerShell = Join-Path $PSHOME 'powershell.exe'
    [FoxyShortcut]::Save($Shortcut, $PowerShell, ('-NoProfile -NoExit -ExecutionPolicy Bypass -File "' + $StatusScript + '"'), $Tools)
    $AppId = 'HKCU:\Software\Classes\AppUserModelId\JokerRecon.JRFoxy.Backup'
    New-Item -Path $AppId -Force | Out-Null
    New-ItemProperty -Path $AppId -Name DisplayName -Value 'JR-Foxy Backup' -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $AppId -Name ShowInSettings -Value 1 -PropertyType DWord -Force | Out-Null
    Write-Output 'WINDOWS_NOTIFICATIONS_REGISTERED'
    exit 0
}

$Pythonw = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $Pythonw)) {
    throw 'pythonw.exe is required for a background task without a console window.'
}
$Client = Join-Path $Tools 'client.py'
$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"' + $Client + '" sync') -WorkingDirectory $Tools
$Login = New-ScheduledTaskTrigger -AtLogOn -User $Identity.Name
$Login.Delay = 'PT2M'
$Periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(30) -RepetitionInterval (New-TimeSpan -Minutes 30)
$Principal = New-ScheduledTaskPrincipal -UserId $Identity.Name -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Login,$Periodic) -Principal $Principal -Settings $Settings -Description 'Download and verify JR-Foxy encrypted backups after login and every 30 minutes.' -Force | Out-Null
$Task = Get-ScheduledTask -TaskName $TaskName
if ($Task.State -eq 'Disabled') { throw 'The Windows backup task is disabled.' }
Write-Output 'WINDOWS_SCHEDULE_ENABLED: login +2 minutes; every 30 minutes while signed in'
