$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = 8501
$Url = "http://localhost:$Port"
$LogDir = Join-Path $AppDir "logs"
$OutLog = Join-Path $LogDir "streamlit-out.log"
$ErrLog = Join-Path $LogDir "streamlit-err.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-PortOpen {
    param([int]$PortNumber)
    try {
        $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue
        return [bool]$conn
    } catch {
        $netstat = netstat -ano | Select-String ":$PortNumber"
        return [bool]$netstat
    }
}

function Find-PythonWithStreamlit {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "python"
    )

    foreach ($candidate in $candidates) {
        try {
            $cmd = Get-Command $candidate -ErrorAction Stop
            $exe = $cmd.Source
            & $exe -c "import streamlit" *> $null
            if ($LASTEXITCODE -eq 0) {
                return $exe
            }
        } catch {
        }
    }
    return $null
}

if (Test-PortOpen -PortNumber $Port) {
    Start-Process $Url
    exit 0
}

$PythonExe = Find-PythonWithStreamlit
if (-not $PythonExe) {
    $msg = "Nao encontrei Python com Streamlit instalado. Instale com: python -m pip install streamlit pandas openpyxl firebase-admin reportlab"
    Set-Content -Path $ErrLog -Value $msg -Encoding UTF8
    Write-Host $msg
    exit 1
}

Set-Content -Path $OutLog -Value "Iniciando app C6 em $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') com $PythonExe" -Encoding UTF8
Set-Content -Path $ErrLog -Value "" -Encoding UTF8

Start-Process -WindowStyle Hidden -FilePath $PythonExe -ArgumentList @(
    "-m", "streamlit", "run", "app.py",
    "--server.headless", "true",
    "--server.port", "$Port"
) -WorkingDirectory $AppDir -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog

$deadline = (Get-Date).AddSeconds(35)
while ((Get-Date) -lt $deadline) {
    if (Test-PortOpen -PortNumber $Port) {
        Start-Process $Url
        exit 0
    }
    Start-Sleep -Seconds 1
}

Start-Process notepad.exe $ErrLog
exit 1
