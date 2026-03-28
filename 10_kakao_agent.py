"""
10_kakao_agent.py — 카카오톡 PC 자동화 에이전트
================================================

원리:
  · pywinauto + win32api 로 PC 카카오톡 창을 직접 조작
  · RICHEDIT50W 컨트롤에 텍스트 삽입 → Enter 전송
  · 이미 열린 채팅방: 창 제목으로 즉시 접근
  · 닫힌 채팅방: 메인창 검색창 활성화 → 이름 입력 → Enter

안전:
  · dry_run=True 기본 — 항상 미리보기 후 명시적 confirm 필요
  · 시간당 최대 20통 제한
  · 메시지 길이 상한: 2000자

실행:
  python 10_kakao_agent.py --to "엄마" --msg "안녕하세요!"
  python 10_kakao_agent.py --to "엄마" --msg "안녕!" --confirm
  python 10_kakao_agent.py --list-windows   # 열린 채팅창 목록
"""

from __future__ import annotations

import os
import sys
import time
import ctypes
import sqlite3
import textwrap
import logging
import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kakao_agent")

# ── 의존성 지연 임포트 (설치 확인)
try:
    import pyautogui
    import win32gui
    import win32api
    import win32con
    from pywinauto import Application
    HAS_DEPS = True
except ImportError as _e:
    HAS_DEPS = False
    _MISSING = str(_e)

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  설정
# ══════════════════════════════════════════════════════════════════════════════

PROJECT_DIR        = os.getenv("PROJECT_DIR", r"C:\My_Digital_Persona_Own_LLM_Project")
KAKAO_LOG_DB       = os.path.join(PROJECT_DIR, "kakao_log.db")
MAX_SENDS_PER_HOUR = 20
MAX_MSG_LENGTH     = 2000

# pyautogui 안전 설정
pyautogui.PAUSE         = 0.15   # 각 동작 사이 딜레이
pyautogui.FAILSAFE      = True   # 마우스 코너 → 중단

# ══════════════════════════════════════════════════════════════════════════════
# 🗄️  발송 이력
# ══════════════════════════════════════════════════════════════════════════════

def _init_db():
    conn = sqlite3.connect(KAKAO_LOG_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at     TEXT    NOT NULL,
            recipient   TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            status      TEXT    DEFAULT 'sent'
        )
    """)
    conn.commit()
    return conn


def _log_sent(recipient: str, message: str, status: str = "sent"):
    try:
        conn = _init_db()
        conn.execute(
            "INSERT INTO sent_messages (sent_at, recipient, message, status) VALUES (?,?,?,?)",
            (datetime.now().isoformat(), recipient, message[:500], status)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("카카오 로그 저장 실패: %s", e)


def _count_recent(hours: int = 1) -> int:
    try:
        conn = _init_db()
        cutoff = datetime.fromtimestamp(time.time() - hours * 3600).isoformat()
        n = conn.execute(
            "SELECT COUNT(*) FROM sent_messages WHERE sent_at >= ? AND status='sent'", (cutoff,)
        ).fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def get_history(n: int = 20) -> list[dict]:
    try:
        conn = _init_db()
        rows = conn.execute(
            "SELECT sent_at, recipient, message, status FROM sent_messages ORDER BY id DESC LIMIT ?",
            (n,)
        ).fetchall()
        conn.close()
        return [{"sent_at": r[0], "to": r[1], "msg": r[2], "status": r[3]} for r in rows]
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════════════════════
# 🔍  카카오톡 창 탐색
# ══════════════════════════════════════════════════════════════════════════════

def _get_kakao_app() -> Optional[Application]:
    """KakaoTalk.exe에 연결 (없으면 None)"""
    if not HAS_DEPS:
        raise RuntimeError(f"의존성 없음: {_MISSING}\n  pip install pywinauto pyautogui pygetwindow")
    try:
        app = Application(backend="win32").connect(path="KakaoTalk.exe", timeout=3)
        return app
    except Exception:
        return None


def list_open_chats() -> list[str]:
    """현재 열린 채팅창 제목 목록"""
    app = _get_kakao_app()
    if not app:
        return []
    try:
        wins = app.windows()
        titles = []
        for w in wins:
            t = w.window_text()
            cls = w.class_name()
            # 채팅창: EVA_Window_Dblclk + 'EVA_Window' 외의 제목
            if cls == "EVA_Window_Dblclk" and t and t != "카카오톡":
                titles.append(t)
        return titles
    except Exception:
        return []


def _find_chat_window(app: Application, contact: str):
    """contact 이름이 포함된 채팅창 찾기 (None이면 없음)"""
    try:
        wins = app.windows()
        # 완전 일치 우선
        for w in wins:
            if w.window_text() == contact:
                return w
        # 부분 일치
        for w in wins:
            t = w.window_text()
            if t and contact in t:
                return w
    except Exception:
        pass
    return None


def _get_richedit(chat_win) -> Optional[object]:
    """채팅창에서 메시지 입력 RICHEDIT50W 찾기"""
    try:
        for c in chat_win.children():
            if c.class_name() == "RICHEDIT50W":
                return c
    except Exception:
        pass
    return None


def _force_foreground(hwnd: int):
    """창을 강제로 최전면으로 (Windows 제약 우회)"""
    ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    # AttachThreadInput 우회 테크닉
    curr_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_hwnd     = ctypes.windll.user32.GetForegroundWindow()
    fg_thread   = ctypes.windll.user32.GetWindowThreadProcessId(fg_hwnd, None)
    if curr_thread != fg_thread:
        ctypes.windll.user32.AttachThreadInput(curr_thread, fg_thread, True)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.AttachThreadInput(curr_thread, fg_thread, False)
    else:
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)

# ══════════════════════════════════════════════════════════════════════════════
# 🔎  메인창 검색으로 채팅 열기
# ══════════════════════════════════════════════════════════════════════════════

def _open_chat_via_search(app: Application, contact: str) -> bool:
    """
    카카오톡 메인창에서 검색 → 채팅창 열기.
    반환: 성공 여부
    """
    try:
        main = app.window(title="카카오톡")
        _force_foreground(main.handle)
        time.sleep(0.4)

        r = main.rectangle()
        win_x, win_y = r.left, r.top
        win_w, win_h = r.width(), r.height()

        # ── 검색창 클릭 (메인창 리스트 영역 상단 좌측)
        # 카카오톡 레이아웃: 좌측 아이콘바(~98px), 검색창은 그 오른쪽 상단
        # 실측: ContactListView 시작 x=1284(+98 from L=1186), y=560(+46 from T=514)
        # 검색 입력 영역: 창 내부 x=100~530, y=46~80
        search_x = win_x + int(win_w * 0.50)   # 중앙
        search_y = win_y + int(win_h * 0.063)  # 상단 약 6% 위치 (≈y=47)

        pyautogui.click(search_x, search_y)
        time.sleep(0.3)

        # 검색창에 입력
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.write(contact, interval=0.05)
        time.sleep(1.0)  # 검색 결과 대기

        # 첫 번째 결과 선택
        pyautogui.press("down")
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(1.0)  # 채팅창 열림 대기

        # 검색창 초기화 (Esc)
        pyautogui.press("escape")
        return True

    except Exception as e:
        log.warning("검색 실패: %s", e)
        return False

# ══════════════════════════════════════════════════════════════════════════════
# 📨  메시지 전송 핵심 함수
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class KakaoMessage:
    recipient: str    # 채팅창 이름 (창 제목과 일치해야 함)
    message:   str
    dry_run:   bool = True   # True면 전송 안 함


def send_kakao(
    recipient: str,
    message: str,
    dry_run: bool = True,
) -> dict:
    """
    카카오톡 PC에서 지정 대화상대에게 메시지 전송.

    ⚠️ dry_run=True (기본): 미리보기만, 실제 전송 없음
       dry_run=False       : 실제 전송 (명시적 호출 필요)

    반환:
      {"success": bool, "recipient": str, "message": str, "dry_run": bool, "error": str}
    """
    if not HAS_DEPS:
        return {"success": False, "error": f"의존성 없음: {_MISSING}"}

    # ── 입력 검증
    if not recipient.strip():
        return {"success": False, "error": "수신자 이름 없음"}
    if not message.strip():
        return {"success": False, "error": "메시지 내용 없음"}
    if len(message) > MAX_MSG_LENGTH:
        return {"success": False, "error": f"메시지가 너무 깁니다 ({len(message)}/{MAX_MSG_LENGTH}자)"}

    # ── dry_run 미리보기
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "recipient": recipient,
            "message": message,
            "note": "dry_run=True — 실제 전송 없음. send_kakao(..., dry_run=False) 로 전송.",
        }

    # ── 시간당 발송 제한
    recent = _count_recent(1)
    if recent >= MAX_SENDS_PER_HOUR:
        return {"success": False, "error": f"시간당 발송 제한 ({MAX_SENDS_PER_HOUR}통) 초과"}

    # ── 카카오톡 연결
    app = _get_kakao_app()
    if not app:
        return {"success": False, "error": "KakaoTalk.exe 실행 중이지 않습니다"}

    # ── 1단계: 이미 열린 채팅창 찾기
    chat_win = _find_chat_window(app, recipient)

    # ── 2단계: 없으면 메인창 검색으로 열기
    if chat_win is None:
        ok = _open_chat_via_search(app, recipient)
        if ok:
            time.sleep(1.0)
            chat_win = _find_chat_window(app, recipient)

    if chat_win is None:
        return {
            "success": False,
            "error": f"'{recipient}' 채팅창을 찾지 못했습니다.\n"
                     f"현재 열린 채팅창: {list_open_chats()}\n"
                     f"힌트: --list-windows 로 정확한 채팅창 이름 확인",
        }

    # ── 3단계: RICHEDIT50W 입력창 찾기
    richedit = _get_richedit(chat_win)
    if richedit is None:
        # 대안: 채팅창 활성화 → 하단 클릭 → 키보드 입력
        _force_foreground(chat_win.handle)
        r = chat_win.rectangle()
        # 입력창: 하단 약 15% 지점
        input_x = r.left + r.width() // 2
        input_y = r.bottom - int(r.height() * 0.10)
        pyautogui.click(input_x, input_y)
        time.sleep(0.3)
    else:
        # RICHEDIT50W에 직접 클릭 후 포커스
        _force_foreground(chat_win.handle)
        try:
            richedit.click_input()
        except Exception:
            r2 = richedit.rectangle()
            pyautogui.click(r2.left + 10, r2.top + 10)
        time.sleep(0.3)

    # ── 4단계: 기존 텍스트 지우고 메시지 입력
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)

    # 한글 포함 메시지: pyperclip + paste 방식이 가장 안정적
    try:
        import pyperclip
        pyperclip.copy(message)
        pyautogui.hotkey("ctrl", "v")
    except ImportError:
        # pyperclip 없으면 win32 clipboard 방식
        import ctypes
        _copy_to_clipboard(message)
        pyautogui.hotkey("ctrl", "v")

    time.sleep(0.3)

    # ── 5단계: Enter 전송
    pyautogui.press("enter")
    time.sleep(0.3)

    _log_sent(recipient, message, "sent")
    return {
        "success": True,
        "dry_run": False,
        "recipient": recipient,
        "message": message,
        "sent_at": datetime.now().isoformat(),
    }


def _copy_to_clipboard(text: str):
    """win32 API로 클립보드에 텍스트 복사 (pyperclip 없을 때 대안)"""
    CF_UNICODETEXT = 13
    ctypes.windll.user32.OpenClipboard(0)
    ctypes.windll.user32.EmptyClipboard()
    encoded = (text + "\0").encode("utf-16-le")
    h = ctypes.windll.kernel32.GlobalAlloc(0x0042, len(encoded))  # GMEM_MOVEABLE|GMEM_ZEROINIT
    p = ctypes.windll.kernel32.GlobalLock(h)
    ctypes.memmove(p, encoded, len(encoded))
    ctypes.windll.kernel32.GlobalUnlock(h)
    ctypes.windll.user32.SetClipboardData(CF_UNICODETEXT, h)
    ctypes.windll.user32.CloseClipboard()

# ══════════════════════════════════════════════════════════════════════════════
# 🛠️  유틸리티
# ══════════════════════════════════════════════════════════════════════════════

def format_history(records: list[dict]) -> str:
    if not records:
        return "📭 발송 이력 없음"
    lines = [f"📊 최근 {len(records)}건"]
    for r in records:
        icon = "✅" if r["status"] == "sent" else "❌"
        lines.append(f"{icon} {r['sent_at'][:16]}  →  {r['to']}")
        lines.append(f"   {r['msg'][:80]}")
    return "\n".join(lines)


def check_system() -> str:
    lines = []
    if not HAS_DEPS:
        return f"🔴 의존성 없음: {_MISSING}\n  pip install pywinauto pyautogui pyperclip"
    app = _get_kakao_app()
    if app:
        open_chats = list_open_chats()
        lines.append(f"🟢 KakaoTalk.exe 실행 중")
        lines.append(f"   열린 채팅방 {len(open_chats)}개: {', '.join(open_chats[:5])}")
    else:
        lines.append("🔴 KakaoTalk.exe 실행되지 않음")
    recent = _count_recent(1)
    lines.append(f"📊 시간당 발송: {recent}/{MAX_SENDS_PER_HOUR}통")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="카카오톡 PC 자동화 에이전트")
    parser.add_argument("--to",           "-t", type=str, help="수신자 (채팅창 제목과 일치)")
    parser.add_argument("--msg",          "-m", type=str, help="전송할 메시지")
    parser.add_argument("--confirm",            action="store_true",
                        help="이 플래그 없으면 dry_run (미리보기만)")
    parser.add_argument("--list-windows", "-l", action="store_true",
                        help="현재 열린 채팅창 목록")
    parser.add_argument("--history",            action="store_true",
                        help="발송 이력 출력")
    parser.add_argument("--status",             action="store_true",
                        help="시스템 상태")
    args = parser.parse_args()

    if args.status:
        print(check_system())
        sys.exit(0)

    if args.history:
        print(format_history(get_history()))
        sys.exit(0)

    if args.list_windows:
        chats = list_open_chats()
        if chats:
            print("📱 열린 채팅창:")
            for c in chats:
                print(f"  · {c}")
        else:
            print("열린 채팅창 없음 (KakaoTalk 실행 여부 확인)")
        sys.exit(0)

    if not args.to or not args.msg:
        parser.print_help()
        print("\n예시:")
        print("  python 10_kakao_agent.py -l                        # 열린 채팅창 목록")
        print("  python 10_kakao_agent.py --to 엄마 --msg '안녕!'   # 미리보기")
        print("  python 10_kakao_agent.py --to 엄마 --msg '안녕!' --confirm  # 실제 전송")
        sys.exit(1)

    dry = not args.confirm

    print(f"\n{'='*60}")
    print(f"  📱 카카오톡 에이전트")
    print(f"  수신자: {args.to}")
    print(f"  메시지: {args.msg[:80]}")
    print(f"  모드:   {'미리보기 (dry_run)' if dry else '실제 전송'}")
    print(f"{'='*60}\n")

    if not dry:
        confirm = input("정말 전송하시겠습니까? [yes/NO]: ").strip().lower()
        if confirm != "yes":
            print("취소됨.")
            sys.exit(0)

    result = send_kakao(args.to, args.msg, dry_run=dry)

    if result.get("dry_run"):
        print("📋 미리보기 (전송 안 됨):")
        print(f"  To:  {result['recipient']}")
        print(f"  Msg: {result['message']}")
        print(f"\n  ▶ 실제 전송: python 10_kakao_agent.py --to {args.to!r} --msg {args.msg!r} --confirm")
    elif result["success"]:
        print(f"✅ 전송 완료: {result['sent_at']}")
        print(f"  To:  {result['recipient']}")
        print(f"  Msg: {result['message'][:80]}")
    else:
        print(f"❌ 실패: {result['error']}")
