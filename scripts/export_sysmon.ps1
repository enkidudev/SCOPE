<#
Export a bounded Sysmon replay without changing audit or Sysmon configuration.
Run on Windows. The output is XML, not binary EVTX. Runtime validation on a
Windows host is still required; the development environment was Linux.
#>
[CmdletBinding()]
param(
    [ValidateSet('process', 'network')][string]$Kind = 'process',
    [ValidateRange(1, 168)][int]$Hours = 1,
    [ValidateRange(1, 20000)][int]$MaxEvents = 1000,
    [Parameter(Mandatory = $true)][string]$OutputFile
)
$ErrorActionPreference = 'Stop'
$exportFile = [System.IO.Path]::GetFullPath($OutputFile)
$manifestFile = $exportFile + '.manifest.json'
if ((Test-Path -LiteralPath $exportFile) -or (Test-Path -LiteralPath $manifestFile)) {
    throw 'Output or manifest already exists. Choose a new filename.'
}
$parentDirectory = Split-Path -Parent $exportFile
if (-not (Test-Path -LiteralPath $parentDirectory)) {
    throw 'The output directory must already exist.'
}
$eventId = if ($Kind -eq 'process') { 1 } else { 3 }
$start = (Get-Date).AddHours(-$Hours)
$events = @(Get-WinEvent -FilterHashtable @{
    LogName = 'Microsoft-Windows-Sysmon/Operational'
    Id = $eventId
    StartTime = $start
} -MaxEvents $MaxEvents | Sort-Object RecordId)
if ($events.Count -eq 0) { throw 'No matching events were returned.' }
$xmlEvents = $events | ForEach-Object { $_.ToXml() }
$xml = '<Events>' + [Environment]::NewLine + ($xmlEvents -join [Environment]::NewLine) + [Environment]::NewLine + '</Events>'
[System.IO.File]::WriteAllText($exportFile, $xml, [System.Text.UTF8Encoding]::new($false))
$manifest = [ordered]@{
    DataKind = 'Captured Windows export'
    ExportedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    Computer = $env:COMPUTERNAME
    OSVersion = [System.Environment]::OSVersion.VersionString
    PowerShellVersion = $PSVersionTable.PSVersion.ToString()
    EventId = $eventId
    RequestedStartUtc = $start.ToUniversalTime().ToString('o')
    RequestedMaxEvents = $MaxEvents
    ReturnedEvents = $events.Count
    ReachedLimit = ($events.Count -eq $MaxEvents)
    FirstRecordId = $events[0].RecordId
    LastRecordId = $events[-1].RecordId
    Sha256 = (Get-FileHash -LiteralPath $exportFile -Algorithm SHA256).Hash
    SelectionNote = 'Bounded selected-event export. This does not establish complete telemetry collection.'
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestFile -Encoding UTF8
Write-Output "Exported $($events.Count) events to $exportFile"
Write-Output "Manifest: $manifestFile"
