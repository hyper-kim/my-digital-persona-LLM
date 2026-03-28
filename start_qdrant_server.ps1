# start_qdrant_server.ps1 — Qdrant 서버 시작 스크립트
# 사용: .\start_qdrant_server.ps1
# 자동시작 등록: 시작 메뉴에 바로가기 만들기 또는 작업 스케줄러 등록 권장

$ROOT = "C:\My_Digital_Persona_Own_LLM_Project"
$EXE  = "$ROOT\qdrant_server\qdrant.exe"
$CFG  = "$ROOT\qdrant_server\config.yaml"
$LOG  = "$ROOT\qdrant_server\qdrant.log"
$ERRLOG = "$ROOT\qdrant_server\qdrant_err.log"

# 이미 실행 중이면 스킵
if (Get-Process qdrant -EA SilentlyContinue) {
    Write-Host "Qdrant 이미 실행 중" -ForegroundColor Green
    Invoke-RestMethod "http://localhost:6333/healthz" | Out-Null
    Write-Host "상태: OK (http://localhost:6333)" -ForegroundColor Green
    exit 0
}

Write-Host "Qdrant 서버 시작 중..."
Start-Process -FilePath $EXE `
    -ArgumentList "--config-path", $CFG `
    -NoNewWindow `
    -RedirectStandardOutput $LOG `
    -RedirectStandardError $ERRLOG

# 최대 15초 대기
for ($i=0; $i -lt 15; $i++) {
    Start-Sleep 1
    try {
        Invoke-RestMethod "http://localhost:6333/healthz" -TimeoutSec 2 | Out-Null
        Write-Host "Qdrant 서버 시작 완료: http://localhost:6333" -ForegroundColor Green
        exit 0
    } catch {}
}

Write-Host "Qdrant 서버 시작 실패. 로그 확인:" -ForegroundColor Red
Get-Content $ERRLOG -Tail 10
exit 1
