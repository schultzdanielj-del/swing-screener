$os = Get-CimInstance Win32_OperatingSystem
$freeGB = [math]::Round($os.FreePhysicalMemory/1MB, 2)
$totalGB = [math]::Round($os.TotalVisibleMemorySize/1MB, 2)
$usedGB = $totalGB - $freeGB
Write-Host "Physical: $usedGB / $totalGB GB used, $freeGB GB free"

# Get pages/sec to detect paging activity
$samples = Get-Counter '\Memory\Pages/sec' -SampleInterval 1 -MaxSamples 3 -ErrorAction SilentlyContinue
if ($samples) {
    $avg = ($samples.CounterSamples.CookedValue | Measure-Object -Average).Average
    Write-Host ("Pages/sec avg: " + [math]::Round($avg, 0))
}

# Python process memory total
$py = Get-Process python -ErrorAction SilentlyContinue
$pyTotal = ($py | Measure-Object WorkingSet64 -Sum).Sum / 1GB
Write-Host ("Python total: " + [math]::Round($pyTotal, 2) + " GB across " + $py.Count + " procs")
