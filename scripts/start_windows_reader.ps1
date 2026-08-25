[CmdletBinding()]
param(
    [switch]$ListSessions,
    [switch]$SshTunnel,
    [string]$SshTarget = "vps",
    [ValidateRange(1, 65535)]
    [int]$LocalTunnelPort = 19090,
    [ValidateRange(1, 65535)]
    [int]$RemoteEryuPort = 9090,
    [string]$Endpoint = "",
    [string]$BasicAuthUser = "",
    [string]$EnvFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$readerPath = Join-Path $repoRoot "reader\windows_gsmtc_reader.py"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Repository Python was not found at $venvPython"
}
if (-not (Test-Path -LiteralPath $readerPath -PathType Leaf)) {
    throw "Windows Reader was not found at $readerPath"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $repoRoot ".env"
}

function Get-ReaderEnvValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*(.*?)\s*$')) {
            return $Matches[1].Trim('"', "'")
        }
    }
    return ""
}

function Test-LoopbackPort {
    param([int]$Port, [int]$TimeoutMilliseconds = 500)

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync([Net.IPAddress]::Loopback, $Port)
        if (-not $connectTask.Wait($TimeoutMilliseconds)) { return $false }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-EryuTunnelHealth {
    param([int]$Port)

    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    try {
        $response = $client.GetAsync("http://127.0.0.1:$Port/health").GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200 -or $body.Trim() -ne "ok") {
            throw "Eryu health check through the SSH tunnel did not return 200/ok."
        }
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-SshFailureSummary {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return "SSH tunnel startup failed."
    }
    $text = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    if ($text -match "Permission denied") { return "SSH key authentication was rejected." }
    if ($text -match "Address already in use|cannot listen to port") { return "The local tunnel port is already in use." }
    if ($text -match "Could not resolve hostname") { return "The SSH target alias could not be resolved." }
    if ($text -match "Host key verification failed") { return "SSH host-key verification failed." }
    return "SSH tunnel startup failed."
}

& $venvPython -B -c "import winrt.windows.media.control"
if ($LASTEXITCODE -ne 0) {
    throw "Windows Reader dependency is missing. Install reader\requirements-windows.txt first."
}

$readerArgs = @("-B", $readerPath, "--env-file", $EnvFile)
if ($ListSessions) {
    & $venvPython @readerArgs "--list-sessions"
    if ($LASTEXITCODE -ne 0) {
        throw "GSMTC session listing failed with exit code $LASTEXITCODE."
    }
    return
}

$previousEndpoint = [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_ENDPOINT", "Process")
$previousToken = [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_TOKEN", "Process")
$previousBasicUser = [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_USER", "Process")
$previousBasicPassword = [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_PASSWORD", "Process")
$setEndpoint = $false
$setToken = $false
$setBasicUser = $false
$setBasicPassword = $false
$tunnelProcess = $null
$tunnelErrorPath = ""

try {
    if ($SshTunnel) {
        if ($Endpoint) {
            throw "Do not combine -SshTunnel with -Endpoint; the tunnel uses its own loopback endpoint."
        }
        if ($BasicAuthUser) {
            throw "Do not combine -SshTunnel with -BasicAuthUser; the tunnel connects directly to Eryu Web."
        }
        if ($SshTarget -notmatch '^[A-Za-z0-9._-]+$') {
            throw "SshTarget contains unsupported characters."
        }
        if (Test-LoopbackPort -Port $LocalTunnelPort) {
            throw "Local port 127.0.0.1:$LocalTunnelPort is already in use; no tunnel was started."
        }

        $sshExe = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
        $sshConfig = Join-Path $env:USERPROFILE ".ssh\config"
        if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
            throw "Windows OpenSSH was not found at $sshExe"
        }
        if (-not (Test-Path -LiteralPath $sshConfig -PathType Leaf)) {
            throw "SSH config was not found at $sshConfig"
        }

        $tunnelErrorPath = Join-Path ([IO.Path]::GetTempPath()) ("eryu-reader-ssh-{0}.log" -f [guid]::NewGuid().ToString("N"))
        $sshArgs = @(
            "-F", $sshConfig,
            "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-L", "127.0.0.1:${LocalTunnelPort}:127.0.0.1:${RemoteEryuPort}",
            $SshTarget
        )
        $tunnelProcess = Start-Process -FilePath $sshExe -ArgumentList $sshArgs `
            -RedirectStandardError $tunnelErrorPath -PassThru -WindowStyle Hidden

        $deadline = [DateTime]::UtcNow.AddSeconds(12)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($tunnelProcess.HasExited) {
                throw (Get-SshFailureSummary -Path $tunnelErrorPath)
            }
            if (Test-LoopbackPort -Port $LocalTunnelPort) { break }
            Start-Sleep -Milliseconds 200
        }
        if (-not (Test-LoopbackPort -Port $LocalTunnelPort)) {
            throw "SSH tunnel did not become ready within 12 seconds."
        }
        Test-EryuTunnelHealth -Port $LocalTunnelPort

        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_ENDPOINT", "http://127.0.0.1:$LocalTunnelPort", "Process")
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_USER", "", "Process")
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_PASSWORD", "", "Process")
        $setEndpoint = $true
        $setBasicUser = $true
        $setBasicPassword = $true
        Write-Host "SSH tunnel ready: 127.0.0.1:$LocalTunnelPort -> $SshTarget 127.0.0.1:$RemoteEryuPort"
        Write-Host "Eryu Web health through tunnel: 200/ok"
    }
    elseif ($Endpoint) {
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_ENDPOINT", $Endpoint, "Process")
        $setEndpoint = $true
    }
    if (-not $SshTunnel -and $BasicAuthUser) {
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_USER", $BasicAuthUser, "Process")
        $setBasicUser = $true
    }
    elseif (-not $SshTunnel -and -not [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_USER", "Process")) {
        $fileBasicUser = Get-ReaderEnvValue -Path $EnvFile -Name "ERYU_PRESENCE_BASIC_AUTH_USER"
        if ($fileBasicUser) {
            [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_USER", $fileBasicUser, "Process")
            $setBasicUser = $true
        }
    }

    $existingPresenceToken = [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_TOKEN", "Process")
    $existingAuthToken = [Environment]::GetEnvironmentVariable("ERYU_AUTH_TOKEN", "Process")
    if (-not $existingPresenceToken -and -not $existingAuthToken) {
        $secureToken = Read-Host "Eryu full presence token" -AsSecureString
        $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        $plainToken = $null
        try {
            $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
            if ([string]::IsNullOrWhiteSpace($plainToken)) {
                throw "Presence token cannot be empty."
            }
            [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_TOKEN", $plainToken, "Process")
            $setToken = $true
        }
        finally {
            if ($null -ne $plainToken) { $plainToken = $null }
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
            $secureToken.Dispose()
        }
    }

    $activeBasicUser = [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_USER", "Process")
    $activeBasicPassword = [Environment]::GetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_PASSWORD", "Process")
    if ($activeBasicUser -and -not $activeBasicPassword) {
        $secureBasicPassword = Read-Host "Caddy Basic Auth password" -AsSecureString
        $basicPasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureBasicPassword)
        $plainBasicPassword = $null
        try {
            $plainBasicPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($basicPasswordPointer)
            if ([string]::IsNullOrEmpty($plainBasicPassword)) {
                throw "Basic Auth password cannot be empty."
            }
            [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_PASSWORD", $plainBasicPassword, "Process")
            $setBasicPassword = $true
        }
        finally {
            if ($null -ne $plainBasicPassword) { $plainBasicPassword = $null }
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($basicPasswordPointer)
            $secureBasicPassword.Dispose()
        }
    }

    & $venvPython @readerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Windows Reader stopped with exit code $LASTEXITCODE."
    }
}
finally {
    if ($null -ne $tunnelProcess) {
        if (-not $tunnelProcess.HasExited) {
            $tunnelProcess.Kill()
            $tunnelProcess.WaitForExit(5000) | Out-Null
        }
        $tunnelProcess.Dispose()
    }
    if ($tunnelErrorPath -and (Test-Path -LiteralPath $tunnelErrorPath -PathType Leaf)) {
        Remove-Item -LiteralPath $tunnelErrorPath -Force
    }
    if ($setBasicPassword) {
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_PASSWORD", $previousBasicPassword, "Process")
    }
    if ($setBasicUser) {
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_BASIC_AUTH_USER", $previousBasicUser, "Process")
    }
    if ($setToken) {
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_TOKEN", $previousToken, "Process")
    }
    if ($setEndpoint) {
        [Environment]::SetEnvironmentVariable("ERYU_PRESENCE_ENDPOINT", $previousEndpoint, "Process")
    }
}
