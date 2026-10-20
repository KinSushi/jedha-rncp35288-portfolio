<#
.SYNOPSIS
    Windows task runner for the Docker stack: the PowerShell counterpart of the Makefile.

.DESCRIPTION
    The Makefile and the .sh scripts assume a Unix shell. This script runs the tasks needed for the
    demonstration natively (ADR-0005: local execution with Docker Compose), under the names printed
    by `.\run.ps1 help`. They differ from the Makefile's in places: DEMARRAGE-WINDOWS.md lists the
    correspondence.

    It drives Docker. The checks themselves are in scripts/smoke_check.py, run inside the
    container, so that PowerShell holds no embedded Python.

    Ports: `setup` probes the host ports (PostgreSQL 5432, dashboard 8501, API 8000) and writes the
    free ones to .env, which docker-compose.yml reads (POSTGRES_PORT, DASHBOARD_PORT, API_PORT). A
    PostgreSQL already listening on 5432 does not have to be stopped. The container side keeps its
    ports.

    Called by DEMARRER-WINDOWS.bat, or by hand.

    USAGE
    -----
        .\run.ps1 help
        .\run.ps1 start
        .\run.ps1 smoke

    If PowerShell refuses to run it, unblock the file for this session only:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>

param(
    [Parameter(Position = 0)]
    [string]$Task = "help",

    # Date anchor of the generated batch (ADR-0009), an ISO date or `today`. `start`, `demoday` and
    # `reset` hand it to the containers as BANKING_DATAOPS_GENERATED_AT. Left empty, the variable
    # already set in the session or in .env applies, and otherwise the fixed default date.
    [string]$GeneratedAt = ""
)

# "Continue", not "Stop": Docker writes its build progress to stderr, which PowerShell 5.1 treats
# as a terminating error under "Stop" (CHANGELOG 2.5.1). Failures of native commands are checked
# explicitly instead, through $LASTEXITCODE.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"   # speeds up Invoke-WebRequest markedly

Set-Location -Path $PSScriptRoot

function Write-Step($Message) { Write-Host "`n==> $Message" -ForegroundColor Yellow }
function Write-Ok($Message)   { Write-Host "  OK    $Message" -ForegroundColor Green }
function Write-Bad($Message)  { Write-Host "  FAIL  $Message" -ForegroundColor Red }
function Write-Info($Message) { Write-Host "        $Message" -ForegroundColor Gray }

function Test-PortFree {
    <#
      Returns $true when nothing is listening on the TCP port.

      The listeners come from .NET rather than from netstat, whose state column is localised (on
      a French Windows a regex on "LISTENING" can match nothing and report every port free), or
      from Get-NetTCPConnection, which raises an error when the port is free (CHANGELOG 2.5.1).
    #>
    param([int]$Port)

    try {
        $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        return -not ($listeners | Where-Object { $_.Port -eq $Port })
    } catch {
        # If the enumeration is unavailable, assume the port is taken rather
        # than free. A false "taken" costs one port number; a false "free"
        # costs a failed startup.
        Write-Info "Could not probe port $Port, assuming it is taken."
        return $false
    }
}

function Find-FreePort {
    param([int]$Start, [int]$Attempts = 20)

    for ($port = $Start; $port -lt ($Start + $Attempts); $port++) {
        if (Test-PortFree -Port $port) { return $port }
    }
    throw "No free port found between $Start and $($Start + $Attempts)."
}

function Initialize-Environment {
    <#
      Probes the three host ports, writes .env and reports what was chosen.

      Called by `setup`, which always rewrites .env (a port free yesterday may be taken today),
      and by Get-Ports, only when .env is missing or incomplete.
    #>

    Write-Step "Probing host ports"

    $pg   = Find-FreePort -Start 5432
    $dash = Find-FreePort -Start 8501
    $api  = Find-FreePort -Start 8000

    foreach ($pair in @(@{n="PostgreSQL"; d=5432; a=$pg}, @{n="Dashboard"; d=8501; a=$dash}, @{n="API"; d=8000; a=$api})) {
        if ($pair.a -eq $pair.d) {
            Write-Ok "$($pair.n) : $($pair.a)"
        } else {
            Write-Info "$($pair.n) : $($pair.d) is taken, using $($pair.a) instead"
        }
    }

    # ASCII, not UTF-8: Docker Compose does not read a BOM, and a BOM would make it ignore the
    # first variable silently (CHANGELOG 2.5.0).
    @"
POSTGRES_PORT=$pg
DASHBOARD_PORT=$dash
API_PORT=$api
"@ | Out-File -FilePath ".env" -Encoding ascii

    return @{ Postgres = $pg; Dashboard = $dash; Api = $api }
}

function Get-Ports {
    <#
      Reads the ports currently in effect from .env, and probes again if the file is missing,
      incomplete or unreadable. Each line is matched defensively: a truncated or hand-edited .env
      used to raise from deep inside a regex match, with an error that mentioned "Groups"
      (CHANGELOG 2.5.1).
    #>

    if (-not (Test-Path ".env")) { return Initialize-Environment }

    $values = @{}
    foreach ($line in (Get-Content ".env" -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*([A-Z_]+)\s*=\s*(\d+)\s*$') {
            $values[$Matches[1]] = [int]$Matches[2]
        }
    }

    if (-not ($values.ContainsKey("POSTGRES_PORT") -and
              $values.ContainsKey("DASHBOARD_PORT") -and
              $values.ContainsKey("API_PORT"))) {
        Write-Info ".env is incomplete or unreadable, re-probing the ports."
        return Initialize-Environment
    }

    return @{
        Postgres  = $values["POSTGRES_PORT"]
        Dashboard = $values["DASHBOARD_PORT"]
        Api       = $values["API_PORT"]
    }
}

function Invoke-InApp {
    <#
      Runs a command inside the app container and returns its output.

      The exit code is checked explicitly: with $ErrorActionPreference at Continue (see the top
      of this file), a failing native command does not raise. -Quiet suppresses the failure
      message, for a caller where a non-zero exit is an expected answer; no task uses it yet.
    #>
    param([string[]]$Arguments, [switch]$Quiet)

    $output = docker compose exec -T app @Arguments 2>&1
    if ($LASTEXITCODE -ne 0 -and -not $Quiet) {
        Write-Bad "Command failed inside the container: $($Arguments -join ' ')"
        Write-Info ($output | Out-String).Trim()
    }
    return $output
}

function Invoke-Sql {
    param([string]$Statement)

    docker compose exec -T postgres psql -U dataops -d banking_dataops -c $Statement
}

function Wait-ForDashboard {
    <#
      Blocks until the Streamlit health endpoint answers 200, or gives up after $TimeoutSeconds.
      Returns the seconds waited, or -1 on timeout. Polling replaces a fixed sleep because the
      cold start time depends on the machine.
    #>
    param([int]$Port, [int]$TimeoutSeconds = 240)

    Write-Host "        waiting for the dashboard " -NoNewline -ForegroundColor Gray
    for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
        try {
            $response = Invoke-WebRequest -Uri "http://localhost:$Port/_stcore/health" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                Write-Host ""
                return $i
            }
        } catch { }
        Write-Host "." -NoNewline -ForegroundColor Gray
        Start-Sleep -Seconds 1
    }
    Write-Host ""
    return -1
}

function Task-Help {
    Write-Host @"

  banking-dataops-monitoring - Windows task runner

  IF SOMETHING GOES WRONG
    .\run.ps1 doctor          diagnose the environment and print what is wrong

  START HERE
    .\run.ps1 setup           probe ports, build the image  (run this once, days ahead)
    .\run.ps1 start           start everything and wait until the dashboard answers
    .\run.ps1 start -GeneratedAt today   same, with a batch dated today (the verdict is PASS, see ADR-0009)
    .\run.ps1 train           train the model - once, consciously
    .\run.ps1 smoke           end-to-end check, must print DEMO READY
    .\run.ps1 verify          run the pipeline with --verify (one run, no extra check yet)

  DEMO SCENARIO
    .\run.ps1 demo-inject     inject the degraded batch
    .\run.ps1 demo-reset      remove it and re-run the controls
    .\run.ps1 demo-status     how many demo rows are present

  STACK
    .\run.ps1 stop            stop, keep the data
    .\run.ps1 reset           wipe the volume and restart
    .\run.ps1 logs            follow the pipeline logs
    .\run.ps1 status          state of the two containers (postgres and app)
    .\run.ps1 demoday         presentation-day start: database and application only

  QUALITY
    .\run.ps1 test            the full suite
    .\run.ps1 lint            ruff
    .\run.ps1 reverse-test    the forbidden-path suite, verbose

  AUDIT
    .\run.ps1 audit           last 25 journal events, with their business reason
    .\run.ps1 verdict         the publication verdict
    .\run.ps1 queue           the investigation queue produced by the model

  POWER BI
    .\run.ps1 powerbi         create the star-schema views and print the connection details

  Documentation: README.md, decisions in docs\adr\

"@ -ForegroundColor White
}

function Task-Doctor {
    <#
      Prints the facts that decide whether the project can start at all: PowerShell, Docker,
      project files, ports, .env, images and containers. The output is meant to be copied whole
      into a bug report.
    #>

    Write-Step "Environment"
    Write-Info "PowerShell   $($PSVersionTable.PSVersion)"
    Write-Info "Edition      $($PSVersionTable.PSEdition)"
    Write-Info "OS           $([System.Environment]::OSVersion.VersionString)"
    Write-Info "Folder       $PSScriptRoot"

    Write-Step "Docker"
    $dockerVersion = (docker --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) { Write-Ok $dockerVersion } else { Write-Bad "docker not found in PATH"; Write-Info $dockerVersion }

    $composeVersion = (docker compose version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) { Write-Ok $composeVersion } else { Write-Bad "docker compose (v2) not available"; Write-Info $composeVersion }

    docker info *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Docker daemon is running" } else { Write-Bad "Docker daemon is NOT running - start Docker Desktop" }

    Write-Step "Project files"
    foreach ($file in @("docker-compose.yml", "Dockerfile", "run_pipeline.py", "src", "sql")) {
        if (Test-Path $file) { Write-Ok $file } else { Write-Bad "$file is missing - are you in the right folder?" }
    }

    Write-Step "Ports"
    foreach ($port in @(5432, 5433, 5434, 8000, 8501)) {
        if (Test-PortFree -Port $port) { Write-Ok "$port free" } else { Write-Info "$port taken" }
    }

    Write-Step "Configuration"
    if (Test-Path ".env") {
        Write-Ok ".env present"
        Get-Content ".env" | ForEach-Object { Write-Info $_ }
    } else {
        Write-Info ".env absent - run: .\run.ps1 setup"
    }

    Write-Step "Images"
    $images = docker images --format "{{.Repository}}:{{.Tag}}" 2>&1 | Select-String "banking"
    if ($images) { $images | ForEach-Object { Write-Ok $_ } } else { Write-Info "no image built yet - run: .\run.ps1 setup" }

    Write-Step "Containers"
    docker compose ps 2>&1 | Out-String | Write-Host

    Write-Host "`n  Copy this entire output if you need help diagnosing.`n" -ForegroundColor White
}

function Task-Setup {
    Write-Step "Checking Docker"
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Write-Bad "Docker is not running. Start Docker Desktop first."; exit 1 }
    Write-Ok "Docker is running"

    $ports = Initialize-Environment

    Write-Step "Building the image"
    Write-Info "Five to ten minutes the first time. Do this days ahead, never the night before."
    docker compose build
    if ($LASTEXITCODE -ne 0) { Write-Bad "Build failed"; exit 1 }
    Write-Ok "Image built"

    Write-Host "`n  Next: .\run.ps1 start`n" -ForegroundColor White
}

function Set-GeneratedAt {
    # Compose reads BANKING_DATAOPS_GENERATED_AT from the environment of the process that runs it
    # (docker-compose.yml, service app), so setting it for this process is enough.
    if ($GeneratedAt) { $env:BANKING_DATAOPS_GENERATED_AT = $GeneratedAt }
}

function Task-Start {
    Set-GeneratedAt
    $ports = Get-Ports

    Write-Step "Starting the stack from a clean volume"
    # `down -v` removes the postgres_data volume: every start, and so every reset, begins from an
    # empty database that the entrypoint then rebuilds.
    docker compose down -v *> $null
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { Write-Bad "Startup failed. See: .\run.ps1 logs"; exit 1 }

    $elapsed = Wait-ForDashboard -Port $ports.Dashboard
    if ($elapsed -lt 0) {
        Write-Bad "The dashboard never answered. See: .\run.ps1 logs"
        exit 1
    }
    Write-Ok "Ready in $elapsed seconds"

    Write-Host ""
    Write-Host "  Dashboard   http://localhost:$($ports.Dashboard)" -ForegroundColor Cyan
    Write-Host "  API         http://localhost:$($ports.Api)/verdict" -ForegroundColor Cyan
    Write-Host "  PostgreSQL  localhost:$($ports.Postgres)" -ForegroundColor Cyan
    Write-Host "`n  Next: .\run.ps1 train`n" -ForegroundColor White
}

function Task-DemoDay {
    <#
      Presentation-day start: postgres and app, without removing the volume, then waits for the
      dashboard.

      The API is not left out: it runs in the app container, started by docker/entrypoint.sh
      next to the dashboard (ADR-0006, precision of October 2026). The difference with `start`
      is that no `down -v` runs first.
    #>
    $ports = Get-Ports

    Write-Step "Presentation start - database and application only"
    Set-GeneratedAt
    docker compose up -d postgres app
    $elapsed = Wait-ForDashboard -Port $ports.Dashboard
    if ($elapsed -lt 0) { Write-Bad "The dashboard never answered"; exit 1 }

    Write-Ok "Ready in $elapsed seconds"
    Write-Host "`n  Open http://localhost:$($ports.Dashboard) and do not touch anything else.`n" -ForegroundColor Cyan
}

function Task-Train {
    Write-Step "Training the model"
    Write-Info "Compares a logistic baseline against a tuned forest, ships the winner."
    # --train reruns the whole pipeline, then trains the model and writes
    # models/suspicion_classifier.joblib. It does not fill gold.predictions, so the scoring step
    # (`python -m banking_dataops.ml.predict`) follows: without it smoke fails on
    # "gold.predictions populated".
    Invoke-InApp @("python", "run_pipeline.py", "--train")
    Invoke-InApp @("python", "-m", "banking_dataops.ml.predict")
    Write-Ok "Model trained, persisted and used to score the batch"
}

function Task-Smoke {
    <#
      End-to-end check: scripts/smoke_check.py inside the container, then the dashboard from
      the host.

      The checks live in Python because they used to be one-liners embedded in PowerShell
      strings. PowerShell escapes a single quote by doubling it, so an SQL literal '' ended the
      surrounding string and the script failed at parse time, before any check ran
      (CHANGELOG 2.5.2).
    #>

    $ports = Get-Ports

    Write-Step "Running the end-to-end check inside the container"
    docker compose exec -T app python scripts/smoke_check.py
    $code = $LASTEXITCODE

    Write-Step "Dashboard"
    $healthy = $false
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$($ports.Dashboard)/_stcore/health" -UseBasicParsing -TimeoutSec 10
        $healthy = ($response.StatusCode -eq 200)
    } catch { }

    if ($healthy) { Write-Ok "dashboard answers on http://localhost:$($ports.Dashboard)" }
    else          { Write-Bad "dashboard does not answer - see: .\run.ps1 logs" }

    if ($code -ne 0 -or -not $healthy) {
        Write-Host ""
        Write-Bad "NOT READY"
        Write-Info "Diagnose with: .\run.ps1 doctor"
        exit 1
    }
}

function Task-Verify {
    Write-Step "Running the pipeline with --verify (it adds no check yet)"
    # --verify is accepted by run_pipeline.py but adds no check: this is one ordinary pipeline run,
    # not two. Idempotence comes from the TRUNCATE then INSERT of the load steps.
    Invoke-InApp @("python", "run_pipeline.py", "--verify")
}

function Task-Audit {
    Invoke-Sql "SELECT occurred_at, category, action, row_count, left(reason,60) AS reason FROM audit_events ORDER BY occurred_at DESC LIMIT 25;"
}

function Task-Verdict {
    Invoke-Sql "SELECT * FROM vw_publication_verdict;"
}

function Task-Queue {
    Invoke-Sql "SELECT priority_band, COUNT(*), ROUND(AVG(suspicion_score),4) AS avg_score FROM gold.predictions GROUP BY priority_band ORDER BY 1;"
}

function Task-PowerBI {
    $ports = Get-Ports
    Write-Step "Creating the star-schema views"
    Invoke-InApp @("python", "-m", "banking_dataops.powerbi_views")

    Write-Host "`n  Power BI Desktop connection" -ForegroundColor Cyan
    Write-Host "    Server    localhost:$($ports.Postgres)" -ForegroundColor White
    Write-Host "    Database  banking_dataops" -ForegroundColor White
    Write-Host "    User      dataops" -ForegroundColor White
    Write-Host "    Password  dataops" -ForegroundColor White
    Write-Host "    Mode      Import" -ForegroundColor White
    Write-Host "`n  Import only the views prefixed vw_ - never the raw tables.`n" -ForegroundColor Gray
}

switch ($Task.ToLower()) {
    "help"          { Task-Help }
    "doctor"        { Task-Doctor }
    "setup"         { Task-Setup }
    "start"         { Task-Start }
    "demoday"       { Task-DemoDay }
    "train"         { Task-Train }
    "smoke"         { Task-Smoke }
    "verify"        { Task-Verify }
    "stop"          { docker compose down }
    "reset"         { docker compose down -v; Task-Start }
    "logs"          { docker compose logs -f app }
    "status"        { docker compose ps }
    # test, lint and reverse-test run inside the container, whose image installs requirements.txt
    # and the package but not the dev extra (Dockerfile): pytest and ruff are not part of it.
    "test"          { Invoke-InApp @("python", "-m", "pytest", "-q") }
    "lint"          { Invoke-InApp @("python", "-m", "ruff", "check", ".") }
    "reverse-test"  { Invoke-InApp @("python", "-m", "pytest", "tests/test_reverse_paths.py", "-v") }
    "demo-inject"   { Invoke-InApp @("python", "-m", "banking_dataops.demo_scenario", "inject"); Invoke-InApp @("python", "-m", "banking_dataops.quality_checks") }
    "demo-reset"    { Invoke-InApp @("python", "-m", "banking_dataops.demo_scenario", "reset"); Invoke-InApp @("python", "-m", "banking_dataops.quality_checks") }
    "demo-status"   { Invoke-InApp @("python", "-m", "banking_dataops.demo_scenario", "status") }
    "audit"         { Task-Audit }
    "verdict"       { Task-Verdict }
    "queue"         { Task-Queue }
    "powerbi"       { Task-PowerBI }
    default {
        Write-Bad "Unknown task: $Task"
        Write-Info "Run .\run.ps1 help for the list."
        exit 1
    }
}
