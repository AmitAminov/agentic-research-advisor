param(
  [switch]$NoConnect,
  [switch]$SkipSplitTunnelSafety,
  [string[]]$RequiredBypassApps = @(),
  [int]$TimeoutSec = 90
)

$ErrorActionPreference = 'Stop'

function GuardLog($message) {
  Write-Output ("[expressvpn] {0}" -f $message)
}

function Get-ConfiguredExpressVpnUrl {
  if ($env:EXPRESSVPN_MCP_URL) { return $env:EXPRESSVPN_MCP_URL }

  $candidates = @(
    'C:\Users\ADMIN\.claude.json',
    'C:\Users\ADMIN\.codex\config.toml'
  )
  $pattern = 'http://127\.0\.0\.1:\d+/mcp\?token=[^''"\s,})]+'

  foreach ($path in $candidates) {
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $text = Get-Content -Raw -LiteralPath $path
    $m = [regex]::Match($text, $pattern)
    if ($m.Success) { return $m.Value }
  }

  throw 'ExpressVPN MCP URL not found. Configure Claude Code/Codex MCP server "expressvpn" first.'
}

function ConvertFrom-McpEventContent([string]$content) {
  if (-not $content) { return $null }
  $data = (($content -split "`n") |
    Where-Object { $_ -like 'data: *' } |
    ForEach-Object { $_.Substring(6) }) -join "`n"
  if (-not $data) { return $null }
  return $data | ConvertFrom-Json
}

function Invoke-McpPost($url, $sessionId, $payload, $timeout = 30) {
  $headers = @{
    'Accept' = 'application/json, text/event-stream'
    'Mcp-Protocol-Version' = '2025-06-18'
  }
  if ($sessionId) { $headers['Mcp-Session-Id'] = $sessionId }

  $body = $payload | ConvertTo-Json -Depth 20 -Compress
  try {
    $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -Method Post `
      -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec $timeout
    return [pscustomobject]@{
      Response = $resp
      Json = ConvertFrom-McpEventContent ([string]$resp.Content)
    }
  } catch {
    $status = $null
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    throw "MCP request failed (status=$status): $($_.Exception.Message)"
  }
}

function Get-ToolText($call) {
  $item = @($call.Json.result.content) | Where-Object { $_.type -eq 'text' } | Select-Object -First 1
  if ($item) { return [string]$item.text }
  return ''
}

try {
  $url = Get-ConfiguredExpressVpnUrl
  $uri = [Uri]$url
  if ($uri.Host -ne '127.0.0.1' -or $uri.AbsolutePath -ne '/mcp' -or $uri.Query -notmatch 'token=') {
    throw 'ExpressVPN MCP URL has an unexpected shape; refusing to use it.'
  }

  $init = Invoke-McpPost $url $null @{
    jsonrpc = '2.0'
    id = 1
    method = 'initialize'
    params = @{
      protocolVersion = '2025-06-18'
      capabilities = @{}
      clientInfo = @{ name = 'researcher-expressvpn-guard'; version = '1.0.0' }
    }
  }
  $sessionId = $init.Response.Headers['Mcp-Session-Id']
  if (-not $sessionId) { throw 'ExpressVPN MCP did not return a session id.' }

  $null = Invoke-McpPost $url $sessionId @{ jsonrpc = '2.0'; method = 'notifications/initialized'; params = @{} }

  $tools = Invoke-McpPost $url $sessionId @{ jsonrpc = '2.0'; id = 2; method = 'tools/list'; params = @{} }
  $toolNames = @($tools.Json.result.tools | ForEach-Object { $_.name })
  foreach ($required in @('expressvpn_ping', 'expressvpn_get_connectionstate', 'expressvpn_get_dnsconfigured', 'expressvpn_connect')) {
    if ($toolNames -notcontains $required) { throw "ExpressVPN MCP missing required tool: $required" }
  }

  $ping = Get-ToolText (Invoke-McpPost $url $sessionId @{
    jsonrpc = '2.0'; id = 3; method = 'tools/call'
    params = @{ name = 'expressvpn_ping'; arguments = @{} }
  })
  if ($ping -ne 'pong') { throw "ExpressVPN MCP ping returned '$ping' instead of 'pong'." }

  if (-not $SkipSplitTunnelSafety) {
    foreach ($required in @('expressvpn_get_splittunnel', 'expressvpn_get_split_app')) {
      if ($toolNames -notcontains $required) { throw "ExpressVPN MCP missing split-tunnel safety tool: $required" }
    }

    $splitTunnel = Get-ToolText (Invoke-McpPost $url $sessionId @{
      jsonrpc = '2.0'; id = 40; method = 'tools/call'
      params = @{ name = 'expressvpn_get_splittunnel'; arguments = @{} }
    })
    if ($splitTunnel -ne 'true') {
      throw 'Split tunneling is not enabled. Refusing to connect VPN because HUJI SSH/MobaXterm sessions may be interrupted.'
    }

    $splitRules = Get-ToolText (Invoke-McpPost $url $sessionId @{
      jsonrpc = '2.0'; id = 41; method = 'tools/call'
      params = @{ name = 'expressvpn_get_split_app'; arguments = @{} }
    })
    $normalizedSplitRules = $splitRules -replace '\\', '/'

    $requiredGroups = @(
      [pscustomobject]@{
        Name = 'MobaXterm'
        Paths = @(
          'C:\Program Files (x86)\Mobatek\MobaXterm\MobaXterm.exe',
          'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\MobaXterm\MobaXterm.lnk',
          'C:\Users\ADMIN\OneDrive\Desktop\Academic\MobaXterm.lnk'
        )
      },
      [pscustomobject]@{
        Name = 'VS Code Remote SSH / OpenSSH'
        Paths = @(
          'C:\Windows\System32\OpenSSH\ssh.exe',
          'C:\Users\ADMIN\AppData\Local\Programs\Microsoft VS Code\Code.exe',
          'C:\Users\ADMIN\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Visual Studio Code\Visual Studio Code.lnk',
          'C:\Users\ADMIN\OneDrive\Desktop\Professional\Software\Visual Studio Code.lnk'
        )
      },
      [pscustomobject]@{
        Name = 'WinSCP HUJI / bava'
        Paths = @(
          'C:\Program Files (x86)\WinSCP\WinSCP.exe',
          'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\WinSCP.lnk',
          'C:\Users\ADMIN\OneDrive\Desktop\Academic\WinSCP.lnk'
        )
      }
    )

    $missingRules = @()
    foreach ($group in $requiredGroups) {
      $existingPaths = @($group.Paths | Where-Object { Test-Path -LiteralPath $_ })
      if ($existingPaths.Count -eq 0) { continue }
      $hasRule = $false
      foreach ($candidate in $existingPaths) {
        $normalizedCandidate = $candidate -replace '\\', '/'
        if ($normalizedSplitRules.IndexOf($normalizedCandidate, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
          $hasRule = $true
          break
        }
      }
      if (-not $hasRule) { $missingRules += $group.Name }
    }

    foreach ($app in $RequiredBypassApps) {
      $normalizedApp = $app -replace '\\', '/'
      if ((Test-Path -LiteralPath $app) -and ($normalizedSplitRules.IndexOf($normalizedApp, [System.StringComparison]::OrdinalIgnoreCase) -lt 0)) {
        $missingRules += $app
      }
    }
    if ($missingRules.Count -gt 0) {
      throw ("Split tunneling is enabled, but these HUJI client bypass rule groups are missing: " + ($missingRules -join '; '))
    }
  }

  $state = Get-ToolText (Invoke-McpPost $url $sessionId @{
    jsonrpc = '2.0'; id = 4; method = 'tools/call'
    params = @{ name = 'expressvpn_get_connectionstate'; arguments = @{} }
  })
  $dns = Get-ToolText (Invoke-McpPost $url $sessionId @{
    jsonrpc = '2.0'; id = 5; method = 'tools/call'
    params = @{ name = 'expressvpn_get_dnsconfigured'; arguments = @{} }
  })

  if (($state -ne 'Connected' -or $dns -ne 'true') -and $NoConnect) {
    throw "VPN is not ready (state=$state dnsConfigured=$dns) and -NoConnect was set."
  }

  if ($state -ne 'Connected' -or $dns -ne 'true') {
    GuardLog "VPN not ready (state=$state dnsConfigured=$dns); connecting through ExpressVPN MCP."
    $null = Invoke-McpPost $url $sessionId @{
      jsonrpc = '2.0'; id = 6; method = 'tools/call'
      params = @{ name = 'expressvpn_connect'; arguments = @{} }
    } 60

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
      Start-Sleep -Seconds 2
      $state = Get-ToolText (Invoke-McpPost $url $sessionId @{
        jsonrpc = '2.0'; id = 7; method = 'tools/call'
        params = @{ name = 'expressvpn_get_connectionstate'; arguments = @{} }
      })
      $dns = Get-ToolText (Invoke-McpPost $url $sessionId @{
        jsonrpc = '2.0'; id = 8; method = 'tools/call'
        params = @{ name = 'expressvpn_get_dnsconfigured'; arguments = @{} }
      })
    } while (($state -ne 'Connected' -or $dns -ne 'true') -and (Get-Date) -lt $deadline)
  }

  if ($state -ne 'Connected' -or $dns -ne 'true') {
    throw "VPN failed readiness check after ${TimeoutSec}s (state=$state dnsConfigured=$dns)."
  }

  if ($toolNames -contains 'expressvpn_get_networklock') {
    $networkLock = Get-ToolText (Invoke-McpPost $url $sessionId @{
      jsonrpc = '2.0'; id = 9; method = 'tools/call'
      params = @{ name = 'expressvpn_get_networklock'; arguments = @{} }
    })
    if ($networkLock -ne 'true') {
      throw 'ExpressVPN Network Lock is disabled. Refusing to run internet automation without kill-switch protection.'
    }
  }

  GuardLog 'VPN ready via ExpressVPN MCP (ping=pong, state=Connected, dnsConfigured=true, networkLock=true).'
  exit 0
} catch {
  GuardLog ("ERROR: {0}" -f $_.Exception.Message)
  exit 2
}
