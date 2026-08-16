param(
    [ValidateSet('equity', 'last-hour', 'stocks', 'patterns', 'research', 'campaigns')]
    [string]$Page = 'equity'
)

$ErrorActionPreference = 'Stop'

$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonRoot = 'C:\Users\bhavi\AppData\Local\Programs\Python\Python312'
$python = Join-Path $pythonRoot 'pythonw.exe'
$port = 8790
$baseUrl = "http://127.0.0.1:$port"
$url = "$baseUrl/$Page"

function Test-TEScanner {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/equity" -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match '<title>Equity LEAPS Engine</title>'
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Python was not found at $python.",
        'TE Scanner'
    ) | Out-Null
    exit 1
}

if (-not (Test-TEScanner)) {
    $occupied = Test-NetConnection -ComputerName 127.0.0.1 -Port $port `
        -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($occupied) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "Port $port is occupied by another application. TE Scanner was not opened to avoid showing the wrong app.",
            'TE Scanner'
        ) | Out-Null
        exit 1
    }

    $previousPort = $env:FVS_WEB_PORT
    $env:FVS_WEB_PORT = [string]$port
    try {
        $process = Start-Process -FilePath $python -ArgumentList @('webapp.py') `
            -WorkingDirectory $appRoot -WindowStyle Hidden -PassThru
    } finally {
        if ($null -eq $previousPort) {
            Remove-Item Env:FVS_WEB_PORT -ErrorAction SilentlyContinue
        } else {
            $env:FVS_WEB_PORT = $previousPort
        }
    }

    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-TEScanner) {
            $ready = $true
            break
        }
        if ($process.HasExited) {
            break
        }
    }
    if (-not $ready) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            'TE Scanner could not start. Run webapp.py in a terminal to see the startup error.',
            'TE Scanner'
        ) | Out-Null
        exit 1
    }
}

Start-Process $url
