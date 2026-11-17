#requires -Version 5.0
<#
  Smikie Inspiron 5405 一键健康检查（v2.11+）

  用法：
    .\healthcheck.ps1                   # 全检（默认）
    .\healthcheck.ps1 -OnlyApi          # 仅 cms-api / image-processor health
    .\healthcheck.ps1 -OnlyShopee       # 仅 Shopee 凭证 + 7 国 token
    .\healthcheck.ps1 -Verbose          # 多打日志

  覆盖项：
    [docker] Postgres / Streamlit / cms_cloudflared / n8n / smikie_n8n_cloudflared
            / cms-api / image-processor / stock-monitor / pgweb
    [network] cms_net / smikie_n8n_net / smikie_shared (跨 stack)
    [endpoint] cms-api /health · /shopee/tokens · /shopee/debug-sign
                image-processor /health · /upscale (HEAD)
    [shopee] partner_id / partner_key / api_base / 7 国 refresh_token 落库情况
    [cloudflare] 公网入口 smikie-cms.cc + n8n.smikie-cms.cc 是否 200

  退出码：
    0 全绿；1 有黄（warn 但不影响）；2 有红（关键失败）
#>

[CmdletBinding()]
param(
    [switch]$OnlyApi,
    [switch]$OnlyShopee
)

$ErrorActionPreference = 'Continue'
$script:RedCount = 0
$script:YellowCount = 0

function Write-Ok ($msg)   { Write-Host "  ✅ $msg" -ForegroundColor Green }
function Write-Warn ($msg) { Write-Host "  ⚠️  $msg" -ForegroundColor Yellow; $script:YellowCount++ }
function Write-Err ($msg)  { Write-Host "  ❌ $msg" -ForegroundColor Red; $script:RedCount++ }
function Section ($title)  { Write-Host ""; Write-Host "== $title ==" -ForegroundColor Cyan }

function Test-Container ($name, $required=$true) {
    $info = docker ps --filter "name=^/${name}$" --format "{{.Status}}" 2>$null
    if (-not $info) {
        if ($required) { Write-Err "$name : 不在运行" } else { Write-Warn "$name : 不在运行（可选）" }
        return $false
    }
    if ($info -match "(?i)healthy|Up") { Write-Ok "$name : $info"; return $true }
    Write-Warn "$name : $info"
    return $false
}

function Test-Endpoint ($url, $name, $expectJson=$true) {
    try {
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing 2>$null
        if ($resp.StatusCode -eq 200) {
            if ($expectJson -and $resp.Headers["Content-Type"] -notmatch "json") {
                Write-Warn "$name HTTP 200 但 Content-Type 不是 json: $($resp.Headers['Content-Type'])"
            } else {
                Write-Ok "$name HTTP 200"
            }
            return $resp
        }
        Write-Err "$name HTTP $($resp.StatusCode)"
    } catch {
        Write-Err "$name 连不上: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    }
    return $null
}

if (-not $OnlyShopee) {
    Section "Docker 容器（N8N stack）"
    Test-Container "smikie_n8n" | Out-Null
    Test-Container "smikie_n8n_cloudflared" | Out-Null
    Test-Container "smikie_cms_api" | Out-Null
    Test-Container "smikie_image_processor" | Out-Null
    Test-Container "smikie_stock_monitor" $false | Out-Null

    Section "Docker 容器（CMS V2.3 stack）"
    Test-Container "cms_postgres" | Out-Null
    Test-Container "cms_streamlit" | Out-Null
    Test-Container "cms_cloudflared" | Out-Null
    Test-Container "cms_pgweb" $false | Out-Null

    Section "Docker 网络"
    foreach ($net in @("cms_net","smikie_n8n_net","smikie_shared")) {
        $found = docker network ls --filter "name=^${net}$" --format "{{.Name}}" 2>$null
        if ($found -eq $net) { Write-Ok "$net" } else { Write-Err "$net 不存在（跨 stack 跑不通）" }
    }
    # smikie_shared 里应该有 cms_postgres + smikie_cms_api 至少 2 个
    $sharedContainers = docker network inspect smikie_shared --format "{{range .Containers}}{{.Name}} {{end}}" 2>$null
    if ($sharedContainers) {
        Write-Host "    smikie_shared members: $sharedContainers" -ForegroundColor DarkGray
        if ($sharedContainers -notmatch "cms_postgres") { Write-Warn "cms_postgres 不在 smikie_shared 网络" }
        if ($sharedContainers -notmatch "smikie_cms_api") { Write-Warn "smikie_cms_api 不在 smikie_shared 网络" }
    }
}

if (-not $OnlyShopee) {
    Section "本地端点"
    Test-Endpoint "http://localhost:8789/health" "cms-api /health" | Out-Null
    Test-Endpoint "http://localhost:8788/health" "image-processor /health" | Out-Null
    Test-Endpoint "http://localhost:5678/healthz" "n8n /healthz" $false | Out-Null
}

if (-not $OnlyApi) {
    Section "Shopee 凭证 + token"
    $debug = $null
    try {
        $debug = Invoke-RestMethod "http://localhost:8789/api/automation/shopee/debug-sign" -TimeoutSec 5
    } catch {
        Write-Err "/debug-sign 连不上（cms-api 起来没？）"
    }
    if ($debug) {
        if ($debug.partner_id) { Write-Ok "SHOPEE_PARTNER_ID = $($debug.partner_id)" } else { Write-Err "SHOPEE_PARTNER_ID 未配" }
        if ($debug.partner_key_len -eq 64) {
            Write-Ok "partner_key_len = 64 (Test 含 shpk 完整 key)"
        } elseif ($debug.partner_key_len -eq 60) {
            Write-Warn "partner_key_len = 60（v2.10 起应保留完整 64 char，被 strip 了？）"
        } else {
            Write-Err "partner_key_len = $($debug.partner_key_len) 异常"
        }
        if ($debug.api_base -match "openplatform") { Write-Ok "api_base = $($debug.api_base)" }
        elseif ($debug.api_base -match "partner\.shopeemobile\.com") { Write-Err "api_base 是 v1 老域名 ($($debug.api_base))，必报 Wrong sign" }
        else { Write-Warn "api_base = $($debug.api_base)" }
    }

    $tokens = $null
    try { $tokens = Invoke-RestMethod "http://localhost:8789/api/automation/shopee/tokens" -TimeoutSec 5 } catch {}
    $markets = @("TW","PH","MY","SG","TH","VN","ID")
    $stored = if ($tokens.refresh_tokens) { $tokens.refresh_tokens.PSObject.Properties.Name } else { @() }
    foreach ($mk in $markets) {
        if ($stored -contains $mk) { Write-Ok "$mk refresh_token: ✅ 落库" }
        else { Write-Warn "$mk refresh_token: ⏳ 未授权" }
    }
    if ($tokens.updated_at) { Write-Host "    tokens.updated_at = $($tokens.updated_at)" -ForegroundColor DarkGray }
}

if (-not $OnlyApi -and -not $OnlyShopee) {
    Section "公网入口（Cloudflare Tunnel）"
    # tokens は公網から見えてはいけない（2026-11-17 監査 C1）→ 404 なら OK
    try {
        $r = Invoke-WebRequest -Uri "https://smikie-cms.cc/api/automation/shopee/tokens" -TimeoutSec 5 -UseBasicParsing 2>$null
        Write-Err "smikie-cms.cc/api/automation/shopee/tokens 公网可读 (HTTP $($r.StatusCode))"
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) { Write-Ok "tokens 公网不可见 (404)" }
        else { Write-Warn "tokens 公网检查: $($_.Exception.Message.Split([Environment]::NewLine)[0])" }
    }
    Test-Endpoint "https://smikie-cms.cc" "smikie-cms.cc (streamlit)" $false | Out-Null
    Test-Endpoint "https://n8n.smikie-cms.cc/healthz" "n8n.smikie-cms.cc" $false | Out-Null
}

Write-Host ""
Write-Host "============================" -ForegroundColor Cyan
if ($script:RedCount -eq 0 -and $script:YellowCount -eq 0) {
    Write-Host "✅ 全绿，所有服务和凭证就绪。可以跑 N8N B1-B5 E2E 测试。" -ForegroundColor Green
    exit 0
} elseif ($script:RedCount -eq 0) {
    Write-Host "🟡 黄 $($script:YellowCount) 项（不影响主链路，但有非阻塞问题）" -ForegroundColor Yellow
    exit 1
} else {
    Write-Host "🔴 红 $($script:RedCount) 项 + 🟡 黄 $($script:YellowCount) 项 — 需先修复关键失败" -ForegroundColor Red
    Write-Host "    排查路径见: MORNING-NEXT.md / SOP-*.md" -ForegroundColor DarkGray
    exit 2
}
