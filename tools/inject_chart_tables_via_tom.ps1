<#
.SYNOPSIS
  Inject chart_* (and related) CSV data into the live Power BI Desktop model as calculated tables,
  then Backup the database to an ABF used as Dashboard_ModelOnly DataModel.
#>
$ErrorActionPreference = "Stop"
$Project = "D:\Projects\Vendor-Performance-Analysis"
$NugetRoot = "$Project\tools\_nuget"
$Lib = "$NugetRoot\amo\lib\net45"
$CsvDir = "$Project\exports\powerbi"

# Download AMO/TOM package on demand (not kept in the repo)
if (-not (Test-Path "$Lib\Microsoft.AnalysisServices.Tabular.dll")) {
  Write-Host "Downloading Analysis Services TOM package..."
  New-Item -ItemType Directory -Force -Path $NugetRoot | Out-Null
  $zip = "$NugetRoot\amo.zip"
  Invoke-WebRequest -UseBasicParsing -OutFile $zip `
    "https://api.nuget.org/v3-flatcontainer/microsoft.analysisservices.retail.amd64/19.84.1/microsoft.analysisservices.retail.amd64.19.84.1.nupkg"
  if (Test-Path "$NugetRoot\amo") { Remove-Item "$NugetRoot\amo" -Recurse -Force }
  Expand-Archive $zip "$NugetRoot\amo" -Force
}

Add-Type -Path "$Lib\Microsoft.AnalysisServices.Core.dll"
Add-Type -Path "$Lib\Microsoft.AnalysisServices.dll"
Add-Type -Path "$Lib\Microsoft.AnalysisServices.Tabular.dll"
Add-Type -Path "$Lib\Microsoft.AnalysisServices.Tabular.Json.dll"

function Get-MsmdPort {
  $ms = Get-CimInstance Win32_Process -Filter "Name='msmdsrv.exe'" | Select-Object -First 1
  if (-not $ms) { throw "msmdsrv not running - open a PBIX in Power BI Desktop first" }
  if ($ms.CommandLine -notmatch '-s "([^"]+)"') { throw "Cannot parse msmdsrv data path" }
  $portFile = Join-Path $Matches[1] "msmdsrv.port.txt"
  return (Get-Content $portFile -Encoding Unicode).Trim()
}

function Escape-DaxString([string]$s) {
  if ($null -eq $s) { return "" }
  return ($s -replace '"', '""')
}

function Csv-To-DatatableExpr([string]$path) {
  $rows = Import-Csv -Path $path
  if (-not $rows -or $rows.Count -eq 0) { throw "Empty CSV: $path" }
  $cols = @($rows[0].PSObject.Properties.Name)
  # Infer types from first non-empty values
  $types = @{}
  foreach ($c in $cols) {
    $sample = @($rows | ForEach-Object { $_.$c } | Where-Object { $_ -ne $null -and $_ -ne "" } | Select-Object -First 30)
    $asNum = 0
    foreach ($v in $sample) {
      $n = 0.0
      if ([double]::TryParse([string]$v, [ref]$n)) { $asNum++ }
    }
    $types[$c] = if ($sample.Count -gt 0 -and ($asNum / $sample.Count) -gt 0.8) { "DOUBLE" } else { "STRING" }
  }
  $headerParts = foreach ($c in $cols) { '"{0}", {1}' -f (Escape-DaxString $c), $types[$c] }
  $rowParts = foreach ($r in $rows) {
    $vals = foreach ($c in $cols) {
      $v = [string]$r.$c
      if ($types[$c] -eq "DOUBLE") {
        if ([string]::IsNullOrWhiteSpace($v)) { "BLANK()" }
        else {
          $n = 0.0
          if (-not [double]::TryParse($v, [ref]$n)) { "BLANK()" } else { $n.ToString([Globalization.CultureInfo]::InvariantCulture) }
        }
      } else {
        '"{0}"' -f (Escape-DaxString $v)
      }
    }
    "{" + ($vals -join ", ") + "}"
  }
  return "DATATABLE(" + ($headerParts -join ", ") + ", {" + ($rowParts -join ", ") + "})"
}

$tablesWanted = @(
  "chart_monthly_spend","chart_top_suppliers","chart_brand_spend","chart_lead_time_hist",
  "chart_lead_time_trend","chart_aging_buckets","chart_match_status","chart_po_key_metrics",
  "chart_store_inventory","chart_brand_inventory","chart_planning_risk","chart_supplier_lead_time",
  "chart_performance_status","chart_inventory_kpis","fact_supplier_performance","LowTurnoverVendor"
)

$port = Get-MsmdPort
Write-Host "Connecting to localhost:$port"
$server = New-Object Microsoft.AnalysisServices.Tabular.Server
$server.Connect("Data Source=localhost:$port")
Write-Host "Databases: $($server.Databases.Count)"
$db = $server.Databases[0]
Write-Host "DB=$($db.Name) tables=$($db.Model.Tables.Count)"
$db.Model.Tables | ForEach-Object { Write-Host ("  existing: " + $_.Name) }

foreach ($tname in $tablesWanted) {
  $csv = Join-Path $CsvDir "$tname.csv"
  if (-not (Test-Path $csv)) { Write-Host "SKIP missing $csv"; continue }
  $expr = Csv-To-DatatableExpr $csv
  if ($db.Model.Tables.Contains($tname)) {
    Write-Host "REPLACE $tname"
    $db.Model.Tables.Remove($tname)
  } else {
    Write-Host "ADD $tname"
  }
  $table = New-Object Microsoft.AnalysisServices.Tabular.Table
  $table.Name = $tname
  $part = New-Object Microsoft.AnalysisServices.Tabular.CalculatedPartitionSource
  $part.Expression = $expr
  $partition = New-Object Microsoft.AnalysisServices.Tabular.Partition
  $partition.Name = $tname
  $partition.Source = $part
  $table.Partitions.Add($partition)
  # Columns are inferred on refresh for calculated tables - add stub columns from CSV headers
  $hdr = (Import-Csv $csv | Select-Object -First 1).PSObject.Properties.Name
  foreach ($c in $hdr) {
    $col = New-Object Microsoft.AnalysisServices.Tabular.CalculatedTableColumn
    $col.Name = $c
    $col.SourceColumn = $c
    $table.Columns.Add($col)
  }
  $db.Model.Tables.Add($table)
}

Write-Host "Saving model changes + refreshing calculated tables..."
foreach ($tname in $tablesWanted) {
  if ($db.Model.Tables.Contains($tname)) {
    $db.Model.Tables[$tname].RequestRefresh([Microsoft.AnalysisServices.Tabular.RefreshType]::Full)
  }
}
$saveResult = $db.Model.SaveChanges()
Write-Host "SaveChanges done (impacted=$($saveResult.Impact.Count))"

Write-Host "Tables now: $($db.Model.Tables.Count)"
$db.Model.Tables | ForEach-Object { Write-Host ("  " + $_.Name) }
$server.Disconnect()

Write-Host ""
Write-Host "DONE. In Power BI Desktop: File > Save to update Dashboard.pbix with the chart tables."
