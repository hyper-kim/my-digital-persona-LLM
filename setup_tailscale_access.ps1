# setup_tailscale_access.ps1
# 관리자 권한으로 실행: 우클릭 -> "PowerShell로 실행 (관리자)"
# 또는 터미널에서: Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File setup_tailscale_access.ps1"

$ProjectDir = "C:\My_Digital_Persona_Own_LLM_Project"
$PythonExe  = "$ProjectDir\venv\Scripts\python.exe"
$UiScript   = "$ProjectDir\5_transfer_ui.py"

Write-Host "=== Transfer Chatbot - Tailscale 접근 설정 ===" -ForegroundColor Cyan

# ── 1. 방화벽 규칙 (포트 7862) ──────────────────────────────────────────────
Write-Host "`n[1/3] 방화벽 포트 7862 허용..."
$exists = Get-NetFirewallRule -DisplayName "Transfer Chatbot UI 7862" -ErrorAction SilentlyContinue
if ($exists) {
    Write-Host "      이미 존재 - 건너뜀" -ForegroundColor Yellow
} else {
    New-NetFirewallRule `
        -DisplayName "Transfer Chatbot UI 7862" `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort 7862 `
        -Action Allow `
        -Profile Any | Out-Null
    Write-Host "      완료 - 포트 7862 인바운드 허용됨" -ForegroundColor Green
}

# ── 2. 로그인 시 자동 시작 (작업 스케줄러) ───────────────────────────────────
Write-Host "`n[2/3] 부팅/로그인 시 자동 시작 설정..."
$TaskName = "TransferChatbotUI"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "      기존 작업 삭제 후 재등록" -ForegroundColor Yellow
}

$action  = New-ScheduledTaskAction -Execute $PythonExe -Argument "-X utf8 `"$UiScript`"" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogon
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "편입 상담 챗봇 UI 자동 시작 (포트 7862)" | Out-Null

Write-Host "      완료 - 로그인 시 자동으로 UI가 시작됩니다" -ForegroundColor Green

# ── 3. Tailscale 설치 (winget) ───────────────────────────────────────────────
Write-Host "`n[3/3] Tailscale 설치 확인..."
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if ($ts) {
    Write-Host "      이미 설치됨: $($ts.Source)" -ForegroundColor Yellow
    tailscale status
} else {
    Write-Host "      winget으로 Tailscale 설치 시작..." -ForegroundColor Cyan
    winget install -e --id Tailscale.Tailscale --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -eq 0) {
        Write-Host "      Tailscale 설치 완료!" -ForegroundColor Green
        Write-Host "      -> 시스템 트레이에서 Tailscale 아이콘을 클릭해 로그인하세요" -ForegroundColor Yellow
    } else {
        Write-Host "      winget 실패. 수동 설치: https://tailscale.com/download/windows" -ForegroundColor Red
    }
}

# ── 완료 요약 ────────────────────────────────────────────────────────────────
Write-Host "`n=== 설정 완료 ===" -ForegroundColor Cyan
Write-Host @"

다음 단계:
  1. Tailscale 로그인 (시스템 트레이 아이콘 클릭)
  2. 접속할 다른 기기(폰/맥/윈도우 등)에도 같은 계정으로 Tailscale 설치
  3. 이 PC의 Tailscale IP 확인: tailscale ip -4
  4. 다른 기기에서: http://<tailscale-ip>:7862 으로 접속

  MagicDNS 사용 시: http://<hostname>:7862 (ip 없이 이름으로 접속)

"@ -ForegroundColor White

Read-Host "엔터를 누르면 종료합니다"
