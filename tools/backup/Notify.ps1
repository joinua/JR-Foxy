param([Parameter(Mandatory=$true)][string]$Payload)
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$Message = Get-Content -LiteralPath $Payload -Raw -Encoding UTF8 | ConvertFrom-Json
$Title = [System.Security.SecurityElement]::Escape([string]$Message.title)
$Body = [System.Security.SecurityElement]::Escape([string]$Message.body)
$Xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$Xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$Title</text><text>$Body</text></binding></visual></toast>")
$Toast = [Windows.UI.Notifications.ToastNotification]::new($Xml)
$Toast.ExpirationTime = [DateTimeOffset]::Now.AddHours(1)
$Notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('JokerRecon.JRFoxy.Backup')
if ($Notifier.Setting -ne [Windows.UI.Notifications.NotificationSetting]::Enabled) {
    throw 'Windows notifications are disabled for JR-Foxy Backup.'
}
$Notifier.Show($Toast)
