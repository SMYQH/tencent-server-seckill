$ErrorActionPreference = 'Stop'

function Get-ProxyServer {
    param([System.Net.IWebProxy]$Proxy, [uri]$Destination)

    $server = $Proxy.GetProxy($Destination)
    if ($null -ne $server -and $server -ne $Destination) {
        $server.GetComponents([UriComponents]::SchemeAndServer, [UriFormat]::UriEscaped)
    }
}

function Get-SystemProxyEnvironment {
    $proxy = [System.Net.WebRequest]::GetSystemWebProxy()
    $settings = @{
        TENCENT_SECKILL_API_PROXY = Get-ProxyServer $proxy 'https://act-api.cloud.tencent.com/'
    }

    if ($env:ALL_PROXY) {
        return $settings
    }

    if (-not $env:HTTP_PROXY) {
        $settings.HTTP_PROXY = Get-ProxyServer $proxy 'http://pypi.org/'
    }

    if (-not $env:HTTPS_PROXY) {
        # Download clients accept one proxy per scheme. Preserve direct routes for the default hosts.
        $directHosts = @('localhost', '127.0.0.1', '[::1]', '::1')
        $downloadHosts = @(
            'cdn.playwright.dev'
            'storage.googleapis.com'
            'playwright.download.prss.microsoft.com'
            'pypi.org'
            'files.pythonhosted.org'
            'github.com'
            'release-assets.githubusercontent.com'
        )
        foreach ($hostName in $downloadHosts) {
            $server = Get-ProxyServer $proxy "https://$hostName/"
            if (-not $server) {
                $directHosts += $hostName
            } elseif (-not $settings.HTTPS_PROXY) {
                $settings.HTTPS_PROXY = $server
            }
        }
        if ($settings.HTTPS_PROXY) {
            $settings.NO_PROXY = (@($env:NO_PROXY -split ',') + $directHosts |
                Where-Object { $_ } | Select-Object -Unique) -join ','
        }
    }

    return $settings
}

# ---- auto-detect uv ----
# `if exist uv.exe` never searches PATH in cmd, so `where` must be used.
# Get-Command performs the PATH lookup in PowerShell.
# Order: PATH uv.exe, then the default bin directory of the standalone installer.
# uv finds a real Python 3.10+ interpreter and can install one if necessary.
# Interpreter detection, including filtering Microsoft Store aliases, is handled by uv.
$uv = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty Source
if (-not $uv) {
    # The standalone installer may have updated PATH only for new shells.
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
        [Console]::Error.WriteLine('[ERROR] uv was not found. Install uv: https://docs.astral.sh/uv/getting-started/installation/')
        exit 1
    }
}

$previousEnvironment = @{}
Push-Location -LiteralPath $PSScriptRoot
try {
    try {
        $proxyEnvironment = Get-SystemProxyEnvironment
    } catch {
        [Console]::Error.WriteLine('[WARN] Could not read the Windows proxy. Using the current environment.')
        $proxyEnvironment = @{}
    }

    foreach ($name in $proxyEnvironment.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
        [Environment]::SetEnvironmentVariable($name, $proxyEnvironment[$name])
    }

    # ---- run the project with the locked dependencies ----
    # uv creates the project environment and installs the runtime dependencies.
    # Call uv directly to pass stdin through to the menu and keep file redirects
    # and batch automation working with the same input as the Python script.
    & $uv run --locked --no-dev python main.py
    $exitCode = $LASTEXITCODE
} finally {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name])
    }
    Pop-Location
}
exit $exitCode
