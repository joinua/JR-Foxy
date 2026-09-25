param([string]$Payload, [switch]$Test)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$Stage = 'Runtime'
try {
    if ($PSVersionTable.PSEdition -ne 'Desktop') {
        throw 'Use Windows PowerShell 5.1 (powershell.exe).'
    }
    $AppId = 'JokerRecon.JRFoxy.Backup'
    $ManagerType = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
    $NotifierType = [Windows.UI.Notifications.ToastNotifier, Windows.UI.Notifications, ContentType=WindowsRuntime]
    [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    [Windows.UI.Notifications.NotificationSetting, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null

    $Stage = 'NotifierCreation'
    $Create = $ManagerType.GetMethod('CreateToastNotifier', [type[]]@([string]))
    $Notifier = $Create.Invoke($null, [object[]]@($AppId))
    if ($null -eq $Notifier) { throw 'Windows returned no toast notifier.' }
    if ($Test) {
        Write-Output ('NOTIFIER_TYPE=' + $Notifier.GetType().FullName)
        $Shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'JR-Foxy Backup.lnk'
        Write-Output ('SHORTCUT_EXISTS=' + (Test-Path -LiteralPath $Shortcut))
        if (Test-Path -LiteralPath $Shortcut) {
            $Shell = New-Object -ComObject Shell.Application
            $ShortcutFolder = $Shell.NameSpace((Split-Path -Parent $Shortcut))
            $ShortcutItem = $ShortcutFolder.ParseName((Split-Path -Leaf $Shortcut))
            Write-Output ('SHORTCUT_APP_ID=' + $ShortcutItem.ExtendedProperty('System.AppUserModel.ID'))
        }
    }

    # Invoke the declared WinRT property explicitly. PowerShell's dynamic COM
    # adapter can return no value; that must never be labelled "disabled".
    $Stage = 'SettingRead'
    $SettingProperty = $NotifierType.GetProperty('Setting')
    if ($null -eq $SettingProperty) { throw 'ToastNotifier metadata has no Setting property.' }
    $Setting = $SettingProperty.GetValue($Notifier, $null)
    if ($null -eq $Setting) { throw 'Windows notification setting could not be read.' }
    $SettingCode = [int]$Setting
    $SettingNames = @('Enabled', 'DisabledForApplication', 'DisabledForUser', 'DisabledByGroupPolicy', 'DisabledByManifest')
    if ($SettingCode -lt 0 -or $SettingCode -ge $SettingNames.Count) {
        throw 'Windows returned an unknown notification setting.'
    }
    $SettingName = $SettingNames[$SettingCode]
    Write-Output ('NOTIFICATION_SETTING=' + $SettingName)
    if ($SettingCode -ne 0) {
        $Stage = $SettingName
        throw ('Windows notification permission: ' + $SettingName)
    }

    $Stage = 'Payload'
    if ($Test) {
        $Message = @{ title = 'JR-Foxy: перевірка сповіщень'; body = 'Якщо бачиш це повідомлення, показ сповіщень працює.' }
    } else {
        if ([string]::IsNullOrEmpty($Payload)) { throw 'A notification payload is required.' }
        $Message = Get-Content -LiteralPath $Payload -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $Title = [System.Security.SecurityElement]::Escape([string]$Message.title)
    $Body = [System.Security.SecurityElement]::Escape([string]$Message.body)
    $Xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $Xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$Title</text><text>$Body</text></binding></visual></toast>")
    $Toast = [Windows.UI.Notifications.ToastNotification]::new($Xml)
    $Toast.ExpirationTime = [DateTimeOffset]::Now.AddHours(1)
    $Stage = 'Show'
    $Show = $NotifierType.GetMethod('Show', [type[]]@([Windows.UI.Notifications.ToastNotification]))
    $Show.Invoke($Notifier, [object[]]@($Toast)) | Out-Null
    # Show() returning is submission, not proof of visual delivery.
    Write-Output 'TOAST_SUBMITTED'
} catch {
    Write-Output ('NOTIFY_ERROR=' + $Stage)
    Write-Error -ErrorRecord $_ -ErrorAction Continue
    exit 1
}
