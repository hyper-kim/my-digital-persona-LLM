"""
4_transfer_agent.py — 미국 명문대 편입 전문 팩트체크 에이전트
=============================================================
역할: 대학 공식 .edu 페이지 + 사용자 개인 자료(Qdrant RAG)를 결합하여
      100% 팩트 기반 편입 정보를 제공한다.

실행: python 4_transfer_agent.py
"""

import os
import sys
import re
import time
import logging
import textwrap

import httpx
from bs4 import BeautifulSoup
import ollama
import qdrant_client
from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ─── 인코딩 안전 설정 ────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  환경 설정
# ══════════════════════════════════════════════════════════════════════════════
PROJECT_DIR       = os.getenv("PROJECT_DIR",       r"C:\My_Digital_Persona_Own_LLM_Project")
QDRANT_PATH       = os.getenv("QDRANT_PATH",       os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL        = os.getenv("QDRANT_URL", "")
LOCAL_OLLAMA_URL  = os.getenv("LOCAL_OLLAMA_URL",  "http://localhost:11434")
EMBED_MODEL_NAME  = os.getenv("EMBED_MODEL_NAME",  "intfloat/multilingual-e5-large")
# 팩트체크 모델: 정확한 인용·instruction-following 특화
# 권장: gemma3:12b (VRAM ~7GB, Google — grounding 최강)
#       qwen3:14b (VRAM ~8-9GB, 한국어 추론 우수)
CHAT_MODEL        = os.getenv("TRANSFER_CHAT_MODEL", "qwen3:14b")

# 어드바이저 모델: 다단계 추론·전략 분석 특화
# 권장: qwen3:14b (CoT + 한국어 이해 우수)
#       gemma3:27b @ GCP if VRAM 부족 시
ADVISOR_MODEL     = os.getenv("TRANSFER_ADVISOR_MODEL", "qwen3:14b")

TOP_K             = int(os.getenv("TOP_K",             "6"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "12000"))
WEB_TIMEOUT_SEC   = float(os.getenv("WEB_TIMEOUT_SEC", "15"))

# ── 사용자 프로필 ─────────────────────────────────────────────────────────────
USER_PROFILE: dict[str, str] = {
    "school":     os.getenv("USER_SCHOOL",   "Korea University (고려대학교)"),
    "major":      os.getenv("USER_MAJOR",    "Artificial Intelligence"),
    "gpa":        os.getenv("USER_GPA",      "3.95 / 4.5"),
    "highschool": os.getenv("USER_HS",       "Hansung Science High School (한성과학고)"),
    "toefl":      os.getenv("USER_TOEFL",    "85"),
    "duolingo":   os.getenv("USER_DUOLINGO", "unknown"),
    "sat":        os.getenv("USER_SAT",      "NOT TAKEN"),
    "credits":    os.getenv("USER_CREDITS",  "~60 credits (1+ year completed)"),
}

APPLIED_UNIVERSITIES = {
    "cornell", "stanford", "nyu", "upenn",
    "uiuc", "purdue", "uchicago", "northwestern",
}  # 지원 완료 / 지원 예정
TOP_PRIORITY = "cornell"

# ══════════════════════════════════════════════════════════════════════════════
# 🗺️  대학별 공식 편입 URL 레지스트리
#     priority=True → 이미 지원 완료 → 마감일·서류 최우선 체크
# ══════════════════════════════════════════════════════════════════════════════
UNIVERSITY_REGISTRY: dict[str, dict] = {
    "cornell": {
        "name":     "Cornell University",
        "priority": True,
        "urls": [
            "https://admissions.cornell.edu/apply/transfer-applicants",
            "https://admissions.cornell.edu/apply/transfer-applicants/checklist",
        ],
        "portals": [
            {"url": "https://apply.cornell.edu/apply/", "use_chrome": True, "click_button": "Sign In"},
        ],
        "keywords": ["cornell", "ivy"],
    },
    "stanford": {
        "name":     "Stanford University",
        "priority": True,
        "urls": [
            "https://admission.stanford.edu/apply/transfer/",
            "https://admission.stanford.edu/apply/transfer/faq.html",
        ],
        "portals": [
            {"url": "https://commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
        "keywords": ["stanford"],
    },
    "nyu": {
        "name":     "New York University (NYU)",
        "priority": True,
        "urls": [
            "https://www.nyu.edu/admissions/undergraduate-admissions/how-to-apply/transfer.html",
            "https://www.nyu.edu/admissions/undergraduate-admissions/how-to-apply/transfer/"
            "international-transfer-students.html",
        ],
        "portals": [
            {"url": "https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate", "use_chrome": True, "click_button": "Login"},
        ],
        "keywords": ["nyu", "new york university"],
    },
    "upenn": {
        "name":     "University of Pennsylvania (UPenn)",
        "priority": True,
        "urls": [
            "https://admissions.upenn.edu/admissions-and-financial-aid/transfer-applicants",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
        "keywords": ["upenn", "penn", "university of pennsylvania"],
    },
    "gatech": {
        "name":     "Georgia Institute of Technology",
        "priority": False,
        "urls": [
            "https://admission.gatech.edu/transfer/",
            "https://admission.gatech.edu/transfer/international-transfer",
        ],
        "portals": [
            {"url": "https://admission.gatech.edu/apply", "use_chrome": True, "click_button": "Apply"},
        ],
        "keywords": ["georgia tech", "gatech", "gt"],
    },
    "umich": {
        "name":     "University of Michigan",
        "priority": False,
        "urls": [
            "https://admissions.umich.edu/apply/transfer",
        ],
        "portals": [
            {"url": "https://apply.umich.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
        "keywords": ["umich", "michigan", "university of michigan"],
    },
    "uiuc": {
        "name":     "University of Illinois Urbana-Champaign (UIUC)",
        "priority": True,
        "urls": [
            "https://admissions.illinois.edu/Apply/Transfer",
            "https://admissions.illinois.edu/Requirements/Transfer",
            "https://cs.illinois.edu/admissions/transfer/transfer-applicants",
        ],
        "portals": [
            {"url": "https://myillini.illinois.edu/", "use_chrome": True, "click_button": "Login"},
        ],
        "keywords": ["uiuc", "illinois", "urbana", "일리노이", "어바나"],
    },
    "ucsd": {
        "name":     "UC San Diego (UCSD)",
        "priority": False,
        "urls": [
            "https://admissions.ucsd.edu/transfer/index.html",
        ],
        "portals": [
            {"url": "https://apply.universityofcalifornia.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
        "keywords": ["ucsd", "uc san diego", "san diego"],
    },
    "usc": {
        "name":     "University of Southern California (USC)",
        "priority": False,
        "urls": [
            "https://admission.usc.edu/transfer/",
        ],
        "portals": [
            {"url": "https://apply.usc.edu/", "use_chrome": True, "click_button": "Sign In"},
        ],
        "keywords": ["usc", "southern california"],
    },
    "bu": {
        "name":     "Boston University (BU)",
        "priority": False,
        "urls": [
            "https://www.bu.edu/admissions/apply/transfer/",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
        "keywords": ["bu", "boston university"],
    },
    "northeastern": {
        "name":     "Northeastern University",
        "priority": False,
        "urls": [
            "https://admissions.northeastern.edu/apply/transfer-students/",
        ],
        "portals": [
            {"url": "https://apply.northeastern.edu/", "use_chrome": True, "click_button": "Sign in"},
        ],
        "keywords": ["northeastern"],
    },
    "purdue": {
        "name":     "Purdue University",
        "priority": True,
        "urls": [
            "https://www.admissions.purdue.edu/transfer/index.php",
            "https://www.admissions.purdue.edu/transfer/international-transfer-students.php",
            "https://www.admissions.purdue.edu/transfer/transfer-faq.php",
        ],
        "portals": [
            {"url": "https://admissions.purdue.edu/apply/transfer/", "use_chrome": True, "click_button": "Apply"},
        ],
        "keywords": ["purdue", "퍼듀"],
    },
    "uchicago": {
        "name":     "University of Chicago (UChicago)",
        "priority": True,
        "urls": [
            "https://collegeadmissions.uchicago.edu/apply/transfer-students",
            "https://collegeadmissions.uchicago.edu/apply",
            "https://collegeadmissions.uchicago.edu/apply/requirements",
        ],
        "portals": [
            {"url": "https://applyingtomaroon.uchicago.edu/", "use_chrome": True, "click_button": "Transfer"},
        ],
        "keywords": ["uchicago", "university of chicago", "시카고", "유시카고"],
    },
    "northwestern": {
        "name":     "Northwestern University",
        "priority": True,
        "urls": [
            "https://admissions.northwestern.edu/apply/identities/transfer.html",
            "https://admissions.northwestern.edu/faqs/transferring-to-northwestern/index.html",
            "https://admissions.northwestern.edu/apply/requirements/index.html",
        ],
        "portals": [
            {"url": "https://apply.northwestern.edu/", "use_chrome": True, "click_button": "Transfer"},
        ],
        "keywords": ["northwestern", "노스웨스턴"],
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# 🌐  웹 스크래퍼 — 공식 .edu 페이지만 신뢰
# ══════════════════════════════════════════════════════════════════════════════
_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# 공식 페이지당 최대 글자 수 (LLM 컨텍스트 윈도우 보호)
_MAX_PAGE_CHARS = 8000


def fetch_url(url: str, timeout: float = WEB_TIMEOUT_SEC) -> tuple[str, str]:
    """URL을 GET하여 (정제된 텍스트, 최종_URL)을 반환. 실패 시 ("", url)."""
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=8.0, read=timeout, write=8.0, pool=5.0),
            headers=_WEB_HEADERS,
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            final_url = str(r.url)
            raw_html  = r.text

        soup = BeautifulSoup(raw_html, "lxml")

        # 노이즈 제거 (스크립트·스타일·네비·푸터)
        for tag in soup(["script", "style", "nav", "footer",
                          "header", "noscript", "aside", "iframe"]):
            tag.decompose()

        # 주요 컨텐츠 영역 우선 탐색
        main_area = (
            soup.find("main")
            or soup.find(id=re.compile(r"(main|content|primary)", re.I))
            or soup.find("article")
            or (soup.body or soup)
        )
        text = main_area.get_text(separator="\n", strip=True)

        # 연속된 빈 줄·공백 정제
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)

        return text.strip()[:_MAX_PAGE_CHARS], final_url

    except httpx.HTTPStatusError as e:
        logging.warning(f"[WEB] HTTP {e.response.status_code} → {url}")
        return "", url
    except Exception as e:
        logging.warning(f"[WEB] 접근 실패 → {url} | {e}")
        return "", url


def _get_chrome_major() -> int | None:
    """Windows Chrome 설치 경로에서 major version 번호를 반환한다."""
    import os as _os, re as _re
    for base in [
        r"C:\Program Files\Google\Chrome\Application",
        r"C:\Program Files (x86)\Google\Chrome\Application",
    ]:
        if _os.path.isdir(base):
            for d in _os.listdir(base):
                if _re.match(r"^\d+\.\d+\.\d+\.\d+$", d):
                    return int(d.split(".")[0])
    return None


def _get_chrome_binary_path() -> str:
    """정식 Chrome 실행 파일 경로를 반환한다."""
    import os
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ""


_CF_BLOCK_SIGNALS = (
    "Human Verification", "CAPTCHA", "Attention Required",
    "Just a moment", "cf-browser-verification",
)


def _is_cloudflare_blocked(title: str, text: str) -> bool:
    joined = title + " " + text[:500]
    return any(s.lower() in joined.lower() for s in _CF_BLOCK_SIGNALS)


_LOGIN_LABELS = ("sign in", "log in", "login", "apply now", "apply", "create account", "get started")


def _extract_page_text_4(driver) -> str:
    """현재 페이지 전체 텍스트 추출(script/style/nav 제거)"""
    soup = BeautifulSoup(driver.page_source, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()
    area = (
        soup.find("main")
        or soup.find(id=re.compile(r"(main|content|primary)", re.I))
        or soup.find("article")
        or soup.body
        or soup
    )
    text = area.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:_MAX_PAGE_CHARS]


def _click_labels_4(driver, labels, wait_sec: int = 2) -> None:
    """레이블 리스트 순서대로 첫 번째 클릭 가능한 버튼/링크를 클릭"""
    import time as _t2
    from selenium.webdriver.common.by import By
    _t2.sleep(wait_sec)
    for label in labels:
        try:
            xpath = (
                "//*[contains(translate(normalize-space(text()),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                + repr(label) + ")]"
            )
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed() and el.tag_name in ("a", "button", "input"):
                    el.click()
                    _t2.sleep(2)
                    return
        except Exception:
            continue


def _fetch_visible_chrome_4(
    url: str,
    click_button: str | None = None,
    timeout: int = 35,
    manual_wait_sec: int = 25,
) -> str:
    """실제 Chrome 창(headless=False)으로 수동 로그인/클릭 유도.
    우선순위:
      1) 정식 Chrome(Program Files) + 기존 사용자 프로필 + 주소창 입력
      2) undetected_chromedriver visible
    """
    import os, time as _t3

    def _chrome_binary_path() -> str:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return ""

    def _navigate_via_omnibox(driver_obj, target_url: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        try:
            body = driver_obj.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.CONTROL, "l")
            body.send_keys(target_url)
            body.send_keys(Keys.ENTER)
        except Exception:
            driver_obj.get(target_url)

    user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

    # 1) 정식 Chrome + Selenium visible (사용자 요구사항 우선)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        chrome_bin = _chrome_binary_path()
        for use_profile in (True, False):
            driver = None
            try:
                opts_std = Options()
                opts_std.add_argument("--no-sandbox")
                opts_std.add_argument("--disable-dev-shm-usage")
                opts_std.add_argument("--window-size=1280,900")
                if chrome_bin:
                    opts_std.binary_location = chrome_bin
                if use_profile and os.path.isdir(user_data):
                    opts_std.add_argument(f"--user-data-dir={user_data}")
                    opts_std.add_argument("--profile-directory=Default")

                driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts_std)
                driver.set_page_load_timeout(timeout)
                driver.get("about:blank")
                _navigate_via_omnibox(driver, url)
                _t3.sleep(3)

                labels = list(_LOGIN_LABELS)
                if click_button and click_button.lower() not in labels:
                    labels.insert(0, click_button.lower())
                _click_labels_4(driver, labels, wait_sec=0)

                if manual_wait_sec > 0:
                    logging.info("[CHROME-VIS-STD] 물리적 클릭/로그인 대기 %ss: %s", manual_wait_sec, url)
                    _t3.sleep(manual_wait_sec)

                text = _extract_page_text_4(driver)
                if len(text.strip()) > 100:
                    logging.info("[CHROME-VIS-STD] %s 성공 (%d chars, profile=%s)", url, len(text), use_profile)
                    return text
            except Exception as e:
                if use_profile:
                    logging.debug("[CHROME-VIS-STD] 프로파일 오류(%s) — 임시 프로파일 재시도", e)
                    continue
                logging.warning("[CHROME-VIS-STD] %s 오류: %s", url, e)
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
    except Exception as e:
        logging.warning("[CHROME-VIS-STD] 초기화 오류: %s", e)

    # 2) uc visible 보조 경로
    try:
        import undetected_chromedriver as uc
        _cv = _get_chrome_major()
        _uc_kw = {"version_main": _cv} if _cv else {}
        for use_profile in (True, False):
            driver = None
            try:
                opts = uc.ChromeOptions()
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                opts.add_argument("--window-size=1280,900")
                if use_profile and os.path.isdir(user_data):
                    opts.add_argument(f"--user-data-dir={user_data}")
                    opts.add_argument("--profile-directory=Default")
                driver = uc.Chrome(options=opts, headless=False, **_uc_kw)
                driver.set_page_load_timeout(timeout)
                driver.get("about:blank")
                _navigate_via_omnibox(driver, url)
                _t3.sleep(3)
                labels = list(_LOGIN_LABELS)
                if click_button and click_button.lower() not in labels:
                    labels.insert(0, click_button.lower())
                _click_labels_4(driver, labels, wait_sec=0)
                if manual_wait_sec > 0:
                    logging.info("[CHROME-VIS-UC] 물리적 클릭/로그인 대기 %ss: %s", manual_wait_sec, url)
                    _t3.sleep(manual_wait_sec)
                text = _extract_page_text_4(driver)
                if len(text.strip()) > 100:
                    logging.info("[CHROME-VIS-UC] %s 성공 (%d chars, profile=%s)", url, len(text), use_profile)
                    return text
            except Exception as e:
                if use_profile:
                    continue
                logging.warning("[CHROME-VIS-UC] %s 오류: %s", url, e)
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
    except Exception:
        pass
    return ""


def _fetch_via_gui_automation_4(
    url: str,
    click_button: str | None = None,
    manual_wait_sec: int = 25,
) -> str:
    """정식 Chrome 창을 열고 키보드/마우스 이벤트로 주소창 입력 후 화면 텍스트를 복사한다.
    PAD 스타일 GUI 상호작용 우선 경로."""
    import subprocess
    import time as _t

    chrome_bin = _get_chrome_binary_path()
    if not chrome_bin:
        return ""

    try:
        import pyautogui
        import pyperclip
        import pygetwindow as gw
    except Exception as e:
        logging.warning("[GUI-AUTO] pyautogui/pyperclip/pygetwindow import 실패: %s", e)
        return ""

    try:
        subprocess.Popen([chrome_bin])
        _t.sleep(2.5)

        try:
            chrome_windows = [w for w in gw.getAllTitles() if "Chrome" in w]
            if chrome_windows:
                win = gw.getWindowsWithTitle(chrome_windows[-1])[0]
                win.activate()
                _t.sleep(0.5)
        except Exception:
            pass

        pyautogui.hotkey("ctrl", "l")
        _t.sleep(0.2)
        pyautogui.write(url, interval=0.015)
        pyautogui.press("enter")
        _t.sleep(3)

        if manual_wait_sec > 0:
            logging.info(
                "[GUI-AUTO] %ss 동안 수동 조작 가능 — 필요하면 '%s' 버튼을 직접 눌러주세요: %s",
                manual_wait_sec,
                click_button or "login",
                url,
            )
            _t.sleep(manual_wait_sec)

        pyautogui.hotkey("ctrl", "a")
        _t.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        _t.sleep(0.5)
        text = (pyperclip.paste() or "").strip()
        if len(text) > 120:
            logging.info("[GUI-AUTO] 텍스트 수집 성공 (%d chars)", len(text))
            return text[:_MAX_PAGE_CHARS]
    except Exception as e:
        logging.warning("[GUI-AUTO] 실패: %s", e)

    return ""


def _chrome_click_and_extract_4(driver, click_button, wait_sec: int = 2) -> str:
    """공통 클릭+텍스트추출 헬퍼"""
    import time as _t
    from selenium.webdriver.common.by import By
    _t.sleep(wait_sec)
    if click_button:
        try:
            xpath = (
                "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                "'abcdefghijklmnopqrstuvwxyz')," + repr(click_button.lower()) + ")]"
            )
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed() and el.tag_name in ("a", "button"):
                    el.click()
                    _t.sleep(2)
                    break
        except Exception:
            pass
    soup = BeautifulSoup(driver.page_source, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()
    area = (
        soup.find("main")
        or soup.find(id=re.compile(r"(main|content|primary)", re.I))
        or soup.find("article")
        or soup.body
        or soup
    )
    text = area.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:_MAX_PAGE_CHARS]


def fetch_url_chrome(url: str, click_button: str | None = None, timeout: int = 25) -> str:
    """Cloudflare/봇감지 우회 우선(undetected-chromedriver), 실패 시 일반 selenium 폴백.
    JS 렌더링 포털 페이지 스크레이핑용."""
    import time as _t

    # ── 0차: GUI 직접 상호작용(PAD 스타일) 우선 ─────────────────────────
    manual_wait = int(os.getenv("CHROME_MANUAL_WAIT_SEC", "25"))
    if os.getenv("USE_GUI_AUTOMATION", "true").lower() == "true":
        gui_text = _fetch_via_gui_automation_4(
            url,
            click_button=click_button,
            manual_wait_sec=manual_wait,
        )
        if gui_text:
            return gui_text

    # ── 1차: undetected-chromedriver (Cloudflare 우회) ─────────────────────
    try:
        import undetected_chromedriver as uc
        opts2 = uc.ChromeOptions()
        opts2.add_argument("--no-sandbox")
        opts2.add_argument("--disable-dev-shm-usage")
        _cv = _get_chrome_major()
        driver = uc.Chrome(options=opts2, headless=True, **({"version_main": _cv} if _cv else {}))
        try:
            driver.set_page_load_timeout(timeout)
            driver.get(url)
            _t.sleep(3)
            title = driver.title
            text = _chrome_click_and_extract_4(driver, click_button, wait_sec=0)
            if not _is_cloudflare_blocked(title, text) and len(text.strip()) > 200:
                logging.info("[CHROME-UC] %s 성공 (%d chars)", url, len(text))
                return text
            logging.warning("[CHROME-UC] %s — CF감지 or 빈응답, selenium 폴백", url)
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    except ImportError:
        pass
    except Exception as e:
        logging.warning("[CHROME-UC] %s 오류: %s — selenium 폴백", url, e)

    # ── 2차: 일반 selenium headless ChromeDriver ───────────────────────────
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts,
        )
        try:
            driver.set_page_load_timeout(timeout)
            driver.get(url)
            text = _chrome_click_and_extract_4(driver, click_button)
            title = driver.title or ""
            if not _is_cloudflare_blocked(title, text) and len(text.strip()) > 200:
                logging.info("[CHROME-STD] %s 성공 (%d chars)", url, len(text))
                return text
            logging.warning("[CHROME-STD] %s — 차단/빈응답 감지, visible fallback", url)
        finally:
            driver.quit()
    except ImportError:
        logging.warning("[CHROME] selenium 미설치 — 3차 visible fallback 시도")
    except Exception as e:
        logging.warning("[CHROME-STD] %s 오류: %s — visible fallback", url, e)

    # ── 3차: 실제 Chrome 창(headless=False) + 사용자 프로파일 + 버튼 클릭 ─────
    logging.info("[CHROME-VIS] %s — 실제 창으로 시도 (수동 클릭 대기 포함)", url)
    text = _fetch_visible_chrome_4(
        url,
        click_button=click_button,
        timeout=timeout,
        manual_wait_sec=manual_wait,
    )
    if text:
        return text

    logging.warning("[CHROME] %s — 모든 방법 실패", url)
    return ""


def fetch_university_pages(uni_key: str) -> list[dict]:
    """레지스트리에서 해당 대학의 모든 공식 URL + 포털을 스크래핑하여 결과 리스트 반환."""
    meta = UNIVERSITY_REGISTRY.get(uni_key, {})
    if not meta:
        return []

    results = []

    # 1) 공식 정보 페이지 (httpx)
    for url in meta.get("urls", []):
        text, final_url = fetch_url(url)
        if text.strip():
            results.append({
                "university": meta["name"],
                "url":        final_url,
                "text":       text,
                "source":     "official",
            })
        time.sleep(0.4)

    # 2) 포털 페이지 — use_chrome=True인 경우 Selenium 사용
    for portal in meta.get("portals", []):
        p_url = portal.get("url", "")
        if not p_url:
            continue
        if portal.get("use_chrome"):
            text = fetch_url_chrome(p_url, click_button=portal.get("click_button"))
        else:
            text, _ = fetch_url(p_url)
        if text.strip():
            results.append({
                "university": meta["name"],
                "url":        p_url,
                "text":       text,
                "source":     "portal_chrome" if portal.get("use_chrome") else "portal",
            })
        time.sleep(0.4)

    return results


def detect_universities_in_query(query: str) -> list[str]:
    """쿼리에서 언급된 대학 키 목록을 반환한다.
    특정 대학이 없으면 이미 지원 완료한 대학 전체를 기본 반환.
    Cornell은 최우선(첫 번째)으로 배치한다."""
    q = query.lower()
    found = [
        key for key, meta in UNIVERSITY_REGISTRY.items()
        if any(kw in q for kw in meta["keywords"])
    ]
    if not found:
        found = list(APPLIED_UNIVERSITIES)

    # cornell 최우선 배치
    if TOP_PRIORITY in found:
        found.remove(TOP_PRIORITY)
        found.insert(0, TOP_PRIORITY)

    return found


# ══════════════════════════════════════════════════════════════════════════════
# 🗄️  RAG 검색 (Qdrant — 사용자 개인 자료)
# ══════════════════════════════════════════════════════════════════════════════
def _build_rag_index() -> VectorStoreIndex:
    qclient       = qdrant_client.QdrantClient(url=QDRANT_URL) if QDRANT_URL else qdrant_client.QdrantClient(path=QDRANT_PATH)
    vector_store  = QdrantVectorStore(client=qclient, collection_name="omni_persona_v3")
    storage_ctx   = StorageContext.from_defaults(vector_store=vector_store)
    embed_model   = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)
    return VectorStoreIndex.from_vector_store(
        vector_store=vector_store,
        storage_context=storage_ctx,
        embed_model=embed_model,
    )


def retrieve_user_context(index: VectorStoreIndex, query: str) -> tuple[str, list[str]]:
    """사용자 개인 자료에서 관련 청크를 검색한다."""
    retriever = index.as_retriever(similarity_top_k=TOP_K)
    try:
        nodes = retriever.retrieve(query)
    except Exception as e:
        logging.warning(f"[RAG] 검색 오류: {e}")
        return "", []

    texts, paths = [], []
    for node in nodes:
        t = node.get_content()
        p = (node.metadata or {}).get("file_path", "")
        if t: texts.append(t)
        if p: paths.append(os.path.abspath(p))

    merged = "\n\n".join(texts)[:MAX_CONTEXT_CHARS]
    return merged, sorted(set(paths))


# ══════════════════════════════════════════════════════════════════════════════
# 🧠  프롬프트 빌더
# ══════════════════════════════════════════════════════════════════════════════
_SYSTEM_PROMPT = textwrap.dedent("""
You are a **US University Transfer Admission Fact-Check Agent**.
Your sole mission: deliver 100% verified facts about transfer requirements.
Zero hallucination. Zero guessing. Every claim must be backed by a direct quote.

## Absolute Rules (never violate)
1. **Transfer ≠ Freshman**: Reference ONLY "Transfer (International)" pages.
   Citing freshman data as transfer data is a critical error — mark it explicitly.
2. **Official Source Only**: Trust ONLY content served from official university domains
   (e.g., admissions.cornell.edu, admission.stanford.edu, admissions.upenn.edu).
   Blog, Reddit, or consulting site data must be cross-checked with official sources.
3. **Mandatory Quotation**: For every requirement, paste the EXACT sentence(s) from
   the official page inside `>` blockquotes before interpreting them.
4. **No Speculation**: If data is absent or ambiguous, write:
   > "Data unavailable — please confirm directly with the admissions office."
   Never write "it is likely", "probably", or "should be".
5. **Precision Mode**: Think step-by-step. Temperature = 0, Top-p = maximum precision.

## Student Profile (always check against this)
- Current school : Korea University (고려대학교), Artificial Intelligence major
- High school    : Hansung Science High School (한성과학고) — elite STEM school
- GPA            : 3.95 / 4.5 (≈ 3.56 / 4.0 on US scale)
- TOEFL iBT      : 85
- Duolingo (DET) : not yet taken / unknown
- SAT / ACT      : NOT TAKEN
- Credits earned : ~60 (completed 1+ year — typically qualifies for SAT waiver)
- Applied already: Cornell (TOP PRIORITY ★), Stanford, NYU, UPenn

## Checklist — run for EVERY university
1. **SAT/ACT Requirement**
   - Is it waived for transfer students with 30+ credit hours (1+ year)?
   - Quote the exact waiver policy sentence.
2. **English Proficiency**
   - Does TOEFL iBT 85 meet the minimum score requirement?
   - Is Duolingo (DET) accepted? If yes, what is the minimum DET score?
   - If TOEFL 85 is BELOW minimum, flag with ❌ WARNING.
3. **Application Deadline (Fall 2026)**
   - What is the exact Fall 2026 transfer deadline?
   - If already passed, is Late Application / Spring 2027 transfer available?
4. **Holistic Review Policy**
   - Does the admissions process explicitly allow a strong STEM academic background
     (science high school + top Korean university AI program) to offset lower English scores?
   - Quote any flexibility language.
5. **Required Documents**
   - List every document required for international transfer applicants.

## Output Format (strict — always use this structure)

### [University Name] — Transfer Fact Sheet
**결론 (Conclusion)**: `Yes — eligible` / `No — ineligible` / `Late Only` / `Deadline Passed / Unknown`

| 항목 | 내용 |
|------|------|
| SAT/ACT 필요 여부 | Required / Waived for 30+ credits / Test-Optional |
| TOEFL 85 충족 여부 | ✅ Meets minimum / ❌ Below minimum (required: XX) |
| 듀오링고(DET) 허용 | Yes (min: XX) / No / Unknown |
| 마감일 (Fall 2026) | YYYY-MM-DD or Passed / Unknown |
| 필요 서류 | Transcripts, Essay, Recommendations, ... |

**공식 문구 인용 (Official Quotes)**:
> (exact sentence from official .edu page)
— *Source: [URL]*

**⚠️ 경고 (Warnings)**:
(List every conflict between the student profile and the university's requirements,
 each prefixed with 🔴)

---
""").strip()


def _build_user_message(
    query:        str,
    web_sections: list[dict],
    rag_context:  str,
    source_paths: list[str],
) -> str:
    parts = [f"## 사용자 질문\n{query}\n"]

    if web_sections:
        parts.append("## 공식 대학 웹사이트 데이터 (Official .edu Sources)")
        for section in web_sections:
            parts.append(
                f"### {section['university']}\n"
                f"**출처 URL**: {section['url']}\n\n"
                f"{section['text']}\n"
            )

    if rag_context.strip():
        parts.append("## 사용자 개인 자료 (Personal Documents via RAG)")
        parts.append(rag_context)
        if source_paths:
            parts.append(
                "\n**개인 자료 파일 경로:**\n"
                + "\n".join(f"- {p}" for p in source_paths)
            )

    if not web_sections and not rag_context.strip():
        parts.append(
            "⚠️ 수집된 데이터 없음: 웹 접근에 실패했거나 개인 자료 인덱스가 비어 있습니다.\n"
            "입학처에 직접 문의하세요."
        )

    parts.append(
        "\n## 지시사항\n"
        "위 데이터를 기반으로 체크리스트 5개 항목을 모두 분석하고 "
        "정해진 Fact Sheet 형식으로 답변하세요.\n"
        "- 모든 요건은 공식 인용구(>blockquote)와 함께 제시하세요.\n"
        "- 학생 프로필(TOEFL 85, SAT 없음)과 충돌하는 요건은 반드시 🔴 경고로 표시하세요.\n"
        "- 데이터가 없는 항목은 '데이터 없음 — 입학처 확인 필요'라고 명시하세요."
    )

    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 🎯  어드바이저 프롬프트 & 메시지 빌더
# ══════════════════════════════════════════════════════════════════════════════
_ADVISOR_SYSTEM_PROMPT = textwrap.dedent("""
You are **Alex**, a senior US transfer admissions consultant with 15 years of experience
placing 1,000+ international students at Ivy League and top-25 universities.
You speak Korean fluently and always respond in **Korean**.

## Your Expertise
- Deep knowledge of actual (unofficial) admission patterns, not just written policies
- Understanding of how Korean elite academic backgrounds are perceived by US admissions offices
- Holistic evaluation: GPA trajectory, major fit, narrative strength, extracurriculars
- Strategic advice: where to apply, when to apply, how to frame weaknesses

## Core Principles
1. **Be a real advisor, not a robot**: Give candid, experience-based opinions.
   If TOEFL 85 is genuinely risky, say so directly — but also explain WHY and what to do.
2. **Data-grounded analysis**: Base probability estimates on the provided fact sheet data
   and the student's actual profile. Never fabricate statistics.
3. **Strengths before weaknesses**: Always identify what makes this student competitive.
4. **Actionable recommendations**: Every opinion must end with a concrete next step.
5. **Acknowledge uncertainty**: If admission data is unavailable, say so,
   but still give strategic context based on comparable schools.

## Student Profile (memorize this)
- 현재 학교  : 고려대학교 인공지능학과 (Korea University, AI major)
  - 고려대 AI학과는 국내 최상위 CS 프로그램 중 하나, 미국 대학원 진학률 높음
- 출신 고교  : 한성과학고등학교 (전국 최상위 과학고 — 수학·과학 경쟁력 증명됨)
- GPA       : 3.95 / 4.5 (미국 환산 ≈ 3.56 / 4.0 WES 기준)
  - ⚠️ 4.5 만점 스케일은 미국 4.0 기준보다 높아 보임 → 반드시 WES 환산 명시
- TOEFL iBT : 85점 (많은 명문대 최저 100점 — 핵심 약점)
- SAT/ACT   : 미응시 (1년 이상 이수 → 대부분 편입생 면제 대상)
- 이수 학점  : ~60학점 (1년 이상) → SAT 면제 대부분 충족
- 지원 완료  : Cornell ★최우선, Stanford, NYU, UPenn

## Analysis Framework (항상 이 순서로 분석)
1. **이 학생의 강점 (Strengths)**: 한성과고 + 고대AI + 고GPA의 경쟁력
2. **핵심 리스크 (Critical Risks)**: TOEFL 85의 실질적 영향
3. **합격 가능성 평가 (Realistic Assessment)**: Reach / Target / Safety 분류
   - 수치 제시 시 반드시 근거 명시 (예: 평균 합격 TOEFL, 합격자 GPA 범위)
4. **전략적 제안 (Strategic Advice)**: 구체적 액션 플랜
5. **주의사항 (Watch-outs)**: 놓치기 쉬운 함정

## Output Format
답변은 한국어로, 아래 구조를 따르되 자연스러운 어드바이저 말투로 작성.

### 🎯 [대학명] — 어드바이저 전략 분석

**한 줄 요약**: (이 학생에게 이 대학이 어떤 의미인지 한 문장)

#### ✅ 이 학생의 강점
(구체적으로 — 한성과고, 고대AI, GPA, 전공 관련성)

#### 🔴 핵심 리스크 & 현실적 평가
(TOEFL 부족, 실제 합격 가능성, Reach/Target/Safety)

#### 💡 전략적 제안
(지금 당장 할 수 있는 구체적 액션 3가지)

#### ⚠️ 주의사항
(놓치면 안 되는 것들)
---
""").strip()


def _build_advisor_message(
    query:        str,
    web_sections: list[dict],
    rag_context:  str,
    source_paths: list[str],
) -> str:
    """어드바이저 모드용 프롬프트 — 팩트 데이터를 전략 분석 재료로 활용."""
    parts = [f"## 학생 질문\n{query}\n"]

    if web_sections:
        parts.append("## 공식 요건 데이터 (팩트체크 에이전트가 수집한 원본 데이터)")
        for section in web_sections:
            parts.append(
                f"### {section['university']}\n"
                f"출처: {section['url']}\n\n"
                f"{section['text']}\n"
            )

    if rag_context.strip():
        parts.append("## 학생 개인 자료 (RAG — 준비 현황, 스펙, 메모 등)")
        parts.append(rag_context)
    
    if not web_sections and not rag_context.strip():
        parts.append(
            "⚠️ 수집된 데이터 없음 — 아래 프로필 정보만으로 전략 분석을 진행합니다.\n"
            "(웹 데이터 없이 분석하므로 요건 수치는 반드시 입학처에서 재확인하세요)"
        )

    parts.append(
        "\n## 분석 지시사항\n"
        "위 데이터와 학생 프로필을 바탕으로:\n"
        "1) 이 학생이 해당 대학(들)에 얼마나 경쟁력 있는지 솔직하게 평가하라.\n"
        "2) TOEFL 85의 실질적 영향을 명확히 분석하라 (합격자 평균과 비교).\n"
        "3) 한성과고 + 고대AI + 3.95GPA 조합이 어떤 서사(narrative)를 만드는지 설명하라.\n"
        "4) Reach/Target/Safety 분류를 Fact 데이터 근거와 함께 제시하라.\n"
        "5) 지금 당장 실행 가능한 전략 3가지를 구체적으로 제안하라."
    )

    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 🚀  핵심 파이프라인
# ══════════════════════════════════════════════════════════════════════════════
_rag_index_cache: VectorStoreIndex | None = None


def _get_rag_index() -> "VectorStoreIndex | None":
    global _rag_index_cache
    if _rag_index_cache is None:
        try:
            print("🗄️  Qdrant RAG 인덱스 로딩 중...", flush=True)
            _rag_index_cache = _build_rag_index()
            print("✅ RAG 인덱스 준비 완료.", flush=True)
        except Exception as e:
            logging.warning(f"[RAG] 인덱스 로딩 실패 (계속 진행): {e}")
    return _rag_index_cache


def run_transfer_query(query: str, verbose: bool = True) -> str:
    """편입 팩트체크 파이프라인을 실행하고 마크다운 결과 문자열을 반환한다."""

    # ── 1) 대학 감지 ─────────────────────────────────────────────────────────
    uni_keys = detect_universities_in_query(query)
    if verbose:
        names = ", ".join(
            UNIVERSITY_REGISTRY.get(k, {}).get("name", k) for k in uni_keys
        )
        print(f"\n🔍 검색 대상 대학: {names}", flush=True)

    # ── 2) 공식 .edu 스크래핑 ────────────────────────────────────────────────
    web_sections: list[dict] = []
    for key in uni_keys:
        uni_name = UNIVERSITY_REGISTRY.get(key, {}).get("name", key)
        if verbose:
            print(f"   🌐 {uni_name} 공식 페이지 접근 중...", flush=True)
        web_sections.extend(fetch_university_pages(key))

    if verbose:
        print(f"   ✅ 웹 수집 완료: {len(web_sections)}개 페이지", flush=True)

    # ── 3) RAG 검색 (사용자 개인 자료) ──────────────────────────────────────
    rag_context, source_paths = "", []
    idx = _get_rag_index()
    if idx is not None:
        if verbose:
            print("   🗂️  개인 자료 RAG 검색 중...", flush=True)
        rag_context, source_paths = retrieve_user_context(idx, query)
        if verbose:
            print(f"   ✅ RAG 완료: {len(source_paths)}개 파일 참조", flush=True)

    # ── 4) 프롬프트 조립 ─────────────────────────────────────────────────────
    user_msg = _build_user_message(query, web_sections, rag_context, source_paths)

    # ── 5) LLM 추론 (temperature=0, top_p=1 — 정확도 최우선) ────────────────
    if verbose:
        print(f"\n🧠  [{CHAT_MODEL}] 팩트체크 분석 중...", flush=True)

    try:
        llm_client = ollama.Client(host=LOCAL_OLLAMA_URL)
        res = llm_client.chat(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            options={
                "temperature": 0,
                "top_p":       1,
                "num_ctx":     16384,
            },
            keep_alive=0,
        )
        answer: str = res.get("message", {}).get("content", "").strip()
        if not answer:
            answer = "❌ LLM이 응답을 반환하지 않았습니다. 모델 상태를 확인하세요."
    except Exception as e:
        answer = (
            f"❌ LLM 추론 실패: {e}\n\n"
            "입학처에 직접 이메일로 문의하세요."
        )

    # ── 6) 메타 푸터 ─────────────────────────────────────────────────────────
    footer_lines = [
        "\n\n---",
        f"**검색 대학**: {', '.join(UNIVERSITY_REGISTRY.get(k, {}).get('name', k) for k in uni_keys)}",
        f"**웹 수집 페이지 수**: {len(web_sections)}",
        f"**RAG 참조 파일 수**: {len(source_paths)}",
    ]
    if source_paths:
        footer_lines.append("\n**개인 자료 경로 (상위 5개)**:")
        footer_lines.extend(f"  - {p}" for p in source_paths[:5])

    return answer + "\n".join(footer_lines)


def run_advisor_query(query: str, verbose: bool = True) -> str:
    """어드바이저 파이프라인: 팩트 수집 후 전략적 합격 분석을 반환한다."""

    # ── 1) 대학 감지 ─────────────────────────────────────────────────────────
    uni_keys = detect_universities_in_query(query)
    if verbose:
        names = ", ".join(UNIVERSITY_REGISTRY.get(k, {}).get("name", k) for k in uni_keys)
        print(f"\n🎯 어드바이저 분석 대상: {names}", flush=True)

    # ── 2) 공식 .edu 스크래핑 ────────────────────────────────────────────────
    web_sections: list[dict] = []
    for key in uni_keys:
        uni_name = UNIVERSITY_REGISTRY.get(key, {}).get("name", key)
        if verbose:
            print(f"   🌐 {uni_name} 요건 수집 중...", flush=True)
        web_sections.extend(fetch_university_pages(key))
    if verbose:
        print(f"   ✅ 웹 수집 완료: {len(web_sections)}개 페이지", flush=True)

    # ── 3) RAG 검색 (개인 자료 — 활동, 수상, 에세이 소재 등) ────────────────
    rag_context, source_paths = "", []
    idx = _get_rag_index()
    if idx is not None:
        if verbose:
            print("   🗂️  개인 자료 RAG 검색 중 (활동 내역, 수상 등)...", flush=True)
        # 어드바이저 모드는 학생의 강점 자료가 중요 → 더 많이 검색
        retriever = idx.as_retriever(similarity_top_k=min(TOP_K * 2, 12))
        try:
            nodes = retriever.retrieve(query)
            texts, paths = [], []
            for node in nodes:
                t = node.get_content()
                p = (node.metadata or {}).get("file_path", "")
                if t: texts.append(t)
                if p: paths.append(os.path.abspath(p))
            rag_context = "\n\n".join(texts)[:MAX_CONTEXT_CHARS]
            source_paths = sorted(set(paths))
        except Exception as e:
            logging.warning(f"[RAG-advisor] {e}")
        if verbose:
            print(f"   ✅ RAG 완료: {len(source_paths)}개 파일 참조", flush=True)

    # ── 4) 프롬프트 조립 (어드바이저 전용) ──────────────────────────────────
    user_msg = _build_advisor_message(query, web_sections, rag_context, source_paths)

    # ── 5) LLM 추론 (temperature=0.15 — 약간의 자연스러움 허용) ─────────────
    if verbose:
        print(f"\n🎯  [{ADVISOR_MODEL}] 어드바이저 분석 중...", flush=True)

    try:
        llm_client = ollama.Client(host=LOCAL_OLLAMA_URL)
        res = llm_client.chat(
            model=ADVISOR_MODEL,
            messages=[
                {"role": "system", "content": _ADVISOR_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            options={
                "temperature": 0.15,   # 팩트체크와 달리 약간의 유연성 허용
                "top_p":       0.9,
                "num_ctx":     16384,
            },
            keep_alive=0,
        )
        answer: str = res.get("message", {}).get("content", "").strip()
        if not answer:
            answer = "❌ 어드바이저 응답 없음 — 모델 상태를 확인하세요."
    except Exception as e:
        answer = f"❌ 어드바이저 LLM 오류: {e}"

    # ── 6) 메타 푸터 ─────────────────────────────────────────────────────────
    footer_lines = [
        "\n\n---",
        f"**분석 모델**: `{ADVISOR_MODEL}` (temperature=0.15)",
        f"**분석 대학**: {', '.join(UNIVERSITY_REGISTRY.get(k, {}).get('name', k) for k in uni_keys)}",
        f"**웹 수집**: {len(web_sections)}페이지  |  **RAG 참조**: {len(source_paths)}파일",
        "> ⚠️ 어드바이저 분석은 전략적 의견입니다. 공식 요건은 팩트체크 탭에서 확인하세요.",
    ]
    return answer + "\n".join(footer_lines)


# ══════════════════════════════════════════════════════════════════════════════
# 🖥️  CLI 인터페이스
# ══════════════════════════════════════════════════════════════════════════════
def _print_banner() -> None:
    sep = "=" * 72
    print(sep)
    print("  🎓  미국 명문대 편입 팩트체크 에이전트  v1.0")
    print(f"  ★ 최우선: Cornell  |  지원 완료: Stanford · NYU · UPenn")
    print(f"  학생 프로필: {USER_PROFILE['school']} | GPA {USER_PROFILE['gpa']}")
    print(f"               TOEFL iBT {USER_PROFILE['toefl']}  |  SAT {USER_PROFILE['sat']}")
    print(sep)
    print("  질문 예시:")
    print("    코넬 편입 마감일과 필요 서류 알려줘")
    print("    SAT 없이 지원 가능한 학교 리스트")
    print("    토플 85로 스탠포드 편입 가능해?")
    print("    듀오링고 허용하는 상위권 대학은?")
    print("    Georgia Tech 국제 편입 요건")
    print("  종료: exit")
    print(sep)


if __name__ == "__main__":
    _print_banner()

    while True:
        try:
            query = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not query:
            continue
        if query.lower() in ("exit", "quit", "종료", "q"):
            print("종료합니다.")
            break

        result = run_transfer_query(query, verbose=True)
        print("\n" + result + "\n")
