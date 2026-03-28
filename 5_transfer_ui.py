"""
5_transfer_ui.py — 미국 명문대 편입 통합 UI (7_unified_agent 연동)
=================================================================
두 탭 모두 7_unified_agent.stream_unified() 기반 스트리밍 챗봇.
 · [팩트체크]   — 검색 키워드 → gemma3:12b (temp=0)
 · [어드바이저] — 분석 키워드 → qwen3:14b  (temp=0.6)
 · Intent 라우팅은 7_unified_agent 내부가 자동 처리

실행: python 5_transfer_ui.py
브라우저: http://127.0.0.1:7862
"""

import sys
import os
import textwrap

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(override=False)

import gradio as gr
import importlib

# ── 통합 에이전트 임포트 ──────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ua = importlib.import_module("7_unified_agent")
ea = importlib.import_module("8_essay_agent")
em = importlib.import_module("9_email_agent")
# ══════════════════════════════════════════════════════════════════════════════
# 빠른 질문 프리셋
# ══════════════════════════════════════════════════════════════════════════════

FACTCHECK_QUESTIONS = [
    ("★ 코넬 마감·서류",      "Cornell 편입 Fall 2026 마감일, 필요 서류, SAT 면제 요건"),
    ("★ 코넬 TOEFL·DET",     "Cornell 국제 편입 TOEFL 최저 + Duolingo 허용 여부"),
    ("★ 4개 학교 요건 비교",  "Cornell·Stanford·NYU·UPenn 편입 요건 한눈에 비교"),
    ("SAT 면제 대학",         "SAT 없이 지원 가능한 편입 대학 목록"),
    ("듀오링고 허용 여부",    "Duolingo DET를 허용하는 상위권 편입 대학"),
    ("TOEFL 85 충분한가",     "TOEFL 85로 지원 가능한 대학과 불가능한 대학"),
]

ADVISOR_QUESTIONS = [
    ("★ 코넬 합격확률",       "내 스펙(TOEFL 85, GPA 3.95, SAT 없음)으로 Cornell 편입 합격가능성 분석"),
    ("★ 4개 학교 우선순위",   "Cornell·Stanford·NYU·UPenn 중 내 스펙에 가장 맞는 학교 순위 전략"),
    ("전체 대학 합격가능성",  "등록된 모든 편입 대학 합격가능성 순위 및 추천 어드바이저"),
    ("TOEFL 85 리스크",       "TOEFL 85가 top 25 편입에서 실질적 장벽인지, 한성과고+AI로 커버 가능한지 전략 조언"),
    ("Reach/Target/Safety",  "내 스펙 기준 Reach / Target / Safety 대학 분류 + 지원 전략"),
    ("추가 지원 전략",         "Fall 2026 마감 이후 Spring 2027 또는 추가 지원 전략 어드바이저"),
]

# ══════════════════════════════════════════════════════════════════════════════
# UI 전역 설정
# ══════════════════════════════════════════════════════════════════════════════

_THEME = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="slate",
    neutral_hue="gray",
    font=[gr.themes.GoogleFont("Noto Sans KR"), "sans-serif"],
)
_CSS = """
.profile-box { background: #f7fafc; border-radius: 8px; padding: 12px; }
footer { display: none !important; }
"""

# ══════════════════════════════════════════════════════════════════════════════
# 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def health_check() -> str:
    lines = [ua.check_system(), "", ea.check_system(), "", em.check_system()]
    return "\n".join(lines)


def _profile_md() -> str:
    p = ua.USER_PROFILE
    return textwrap.dedent(f"""
    ### 👤 내 프로필
    | 항목 | 값 |
    |------|-----|
    | 학교 | {p['school']} |
    | 전공 | {p['major']} |
    | GPA | **{p['gpa']}** |
    | 고등학교 | {p['highschool']} |
    | TOEFL iBT | **{p['toefl']}** |
    | SAT/ACT | 🔴 **{p['sat']}** |
    | 이수 학점 | {p['credits']} |

    ### 지원 대학 ★
    - 🥇 Cornell
    - Stanford
    - NYU
    - UPenn
    """).strip()


# ══════════════════════════════════════════════════════════════════════════════
# 스트리밍 챗봇 핸들러
# ══════════════════════════════════════════════════════════════════════════════

def _new_ctx():
    """새 대화 컨텍스트 생성"""
    return ua.ConversationContext()


def stream_chat(query: str, history: list, ctx):
    """stream_unified()를 Gradio chatbot에 연결하는 래퍼"""
    if not query.strip():
        yield history, "", ctx
        return

    if ctx is None:
        ctx = _new_ctx()

    # 히스토리에 유저 메시지 + 빈 어시스턴트 슬롯 추가
    history = history + [{"role": "user", "content": query}]
    history = history + [{"role": "assistant", "content": ""}]
    yield history, "", ctx

    for token in ua.stream_unified(query, ctx):
        history[-1]["content"] += token
        yield history, "", ctx


def clear_chat(ctx):
    return [], "대기 중...", _new_ctx()


# ══════════════════════════════════════════════════════════════════════════════
# ✍️  에세이 핸들러
# ══════════════════════════════════════════════════════════════════════════════

def stream_essay_ui(school: str, prompt_key: str, story: str, existing: str, mode: str):
    if not story.strip() and mode == "draft":
        yield "❗ 담고 싶은 이야기를 입력해주세요."
        return
    if mode in ("revise", "critique") and not existing.strip():
        yield "❗ revise/critique 모드에서는 기존 에세이를 붙여넣어 주세요."
        return
    req = ea.EssayRequest(
        user_instruction=story or "에세이를 비평해주세요",
        school=school if school != "선택" else None,
        prompt_key=prompt_key if prompt_key != "선택" else None,
        mode=mode,
        existing_essay=existing or None,
    )
    result = ""
    for token in ea.stream_essay(req):
        result += token
        yield result


# ══════════════════════════════════════════════════════════════════════════════
# ✉️  이메일 핸들러
# ══════════════════════════════════════════════════════════════════════════════

_last_draft: dict = {"subject": "", "body": "", "to": "", "type": "", "provider": "auto"}


def stream_email_ui(email_type: str, to_addr: str, school: str, recipient: str, topic: str, ctx_extra: str, provider: str):
    if not topic.strip():
        yield "❗ 이메일 주제/목적을 입력해주세요."
        return
    resolved_to = to_addr.strip()
    if not resolved_to and school and school != "선택":
        resolved_to = em.ADMISSIONS_EMAILS.get(school.lower(), "")
    req = em.EmailRequest(
        email_type=email_type,
        to_address=resolved_to or "(수신자 미정)",
        topic=topic,
        recipient_name=recipient,
        school_key=school if school != "선택" else "",
        extra_context=ctx_extra,
    )
    result = ""
    for token in em.stream_compose(req):
        result += token
        yield result
    subject, body = em.extract_subject_and_body(result)
    _last_draft.update({
        "subject": subject,
        "body": body,
        "to": resolved_to,
        "type": email_type,
        "provider": provider or "auto",
    })


def do_send_email(provider: str = "auto") -> str:
    if not _last_draft.get("to") or "@" not in _last_draft.get("to", ""):
        return "❌ 수신자 이메일 주소를 맨 위 입력칸에 입력하세요."
    if not _last_draft.get("subject"):
        return "❌ 먼저 위 입력칸 채운 다음 'AI 초안 작성' 클릭하세요."
    result = em.send_email(
        to_address=_last_draft["to"],
        subject=_last_draft["subject"],
        body=_last_draft["body"],
        email_type=_last_draft["type"],
        provider=provider or _last_draft.get("provider", "auto"),
    )
    if result["success"]:
        return (
            f"✅ 발송 완료\nTo: {result['to']}\n제목: {result['subject']}"
            f"\n계정: {result.get('provider','auto')}\n시간: {result['sent_at']}"
        )
    return f"❌ 발송 실패: {result['error']}"


def load_inbox(from_filter: str = "", provider: str = "auto") -> str:
    msgs = em.read_inbox(10, from_filter, provider=provider)
    return em.format_inbox(msgs)


def load_important_inbox_summary(provider: str = "gmail") -> str:
    return em.summarize_important_inbox(n=40, provider=provider)


def load_email_history() -> str:
    return em.format_history(em.get_send_history(20))


# ══════════════════════════════════════════════════════════════════════════════
# Gradio UI
# ══════════════════════════════════════════════════════════════════════════════

def build_ui():
    with gr.Blocks(title="🎓 편입 통합 에이전트") as app:

        gr.Markdown(
            "# 🎓 미국 명문대 편입 — 팩트체크 + 어드바이저\n"
            f"**{ua.SEARCH_MODEL}** (팩트·temp=0) ＋ **{ua.ADVISOR_MODEL}** (전략·temp={ua.ADVISOR_TEMPERATURE})"
            " · Intent 자동 라우팅"
        )

        with gr.Tabs():

            # ──────────────────────────────────────────────────────────────
            # 탭 1: 팩트체크
            # ──────────────────────────────────────────────────────────────
            with gr.TabItem("📋 팩트체크"):
                ctx1 = gr.State(None)

                with gr.Row(equal_height=False):

                    # 사이드바
                    with gr.Column(scale=1, min_width=240):
                        gr.Markdown(_profile_md(), elem_classes=["profile-box"])
                        gr.Markdown("---")
                        health_btn = gr.Button("🔍 헬스체크", size="sm")
                        health_out = gr.Textbox(label="", lines=4, interactive=False)
                        health_btn.click(health_check, outputs=health_out)
                        app.load(health_check, outputs=health_out)

                    # 메인
                    with gr.Column(scale=3):
                        chatbot1 = gr.Chatbot(
                            label="팩트체크 결과", height=540,
                            render_markdown=True,
                        )

                        with gr.Row():
                            q1 = gr.Textbox(
                                placeholder="예) 코넬 Fall 2026 마감일과 필요 서류",
                                scale=5, lines=2, submit_btn=False,
                            )
                            with gr.Column(scale=1, min_width=110):
                                send1 = gr.Button("📨 질문", variant="primary", size="lg")
                                clear1 = gr.Button("🗑 초기화", size="lg")

                        status1 = gr.Textbox(value="대기 중...", interactive=False, show_label=False)

                        fq_buttons1 = []
                        with gr.Accordion("⚡ 빠른 질문 (클릭 → 바로 전송)", open=True):
                            for label, query_text in FACTCHECK_QUESTIONS:
                                btn = gr.Button(label, size="sm")
                                fq_buttons1.append((btn, query_text))

                        for btn, query_text in fq_buttons1:
                            btn.click(
                                fn=lambda qt=query_text: qt,
                                outputs=q1,
                            ).then(
                                fn=stream_chat,
                                inputs=[q1, chatbot1, ctx1],
                                outputs=[chatbot1, status1, ctx1],
                            )

                        send1.click(
                            fn=stream_chat,
                            inputs=[q1, chatbot1, ctx1],
                            outputs=[chatbot1, status1, ctx1],
                        )
                        q1.submit(
                            fn=stream_chat,
                            inputs=[q1, chatbot1, ctx1],
                            outputs=[chatbot1, status1, ctx1],
                        )
                        clear1.click(
                            fn=clear_chat,
                            inputs=[ctx1],
                            outputs=[chatbot1, status1, ctx1],
                        )

            # ──────────────────────────────────────────────────────────────
            # 탭 2: 어드바이저 (스트리밍 챗봇으로 업그레이드)
            # ──────────────────────────────────────────────────────────────
            with gr.TabItem("🎯 어드바이저"):
                ctx2 = gr.State(None)

                with gr.Row(equal_height=False):

                    # 사이드바
                    with gr.Column(scale=1, min_width=240):
                        gr.Markdown(_profile_md(), elem_classes=["profile-box"])
                        gr.Markdown("---")
                        gr.Markdown(
                            "**분석 기준:**\n"
                            "- GPA & 영어점수\n"
                            "- SAT 면제 여부\n"
                            "- Holistic 정책\n"
                            "- 메타정보 + RAG\n"
                            f"\n**모델:** `{ua.ADVISOR_MODEL}`  \n"
                            f"**온도:** `{ua.ADVISOR_TEMPERATURE}`"
                        )

                    # 메인
                    with gr.Column(scale=3):
                        chatbot2 = gr.Chatbot(
                            label="어드바이저 분석", height=540,
                            render_markdown=True,
                        )

                        with gr.Row():
                            q2 = gr.Textbox(
                                placeholder="예) 내 스펙으로 Cornell 합격가능성 분석해줘",
                                scale=5, lines=2, submit_btn=False,
                            )
                            with gr.Column(scale=1, min_width=110):
                                send2 = gr.Button("🎯 분석", variant="primary", size="lg")
                                clear2 = gr.Button("🗑 초기화", size="lg")

                        status2 = gr.Textbox(value="대기 중...", interactive=False, show_label=False)

                        # 빠른 질문 버튼
                        fq_buttons2 = []
                        with gr.Accordion("💡 빠른 분석 (클릭 → 바로 전송)", open=True):
                            for label, query_text in ADVISOR_QUESTIONS:
                                btn = gr.Button(label, size="sm")
                                fq_buttons2.append((btn, query_text))

                        for btn, query_text in fq_buttons2:
                            btn.click(
                                fn=lambda qt=query_text: qt,
                                outputs=q2,
                            ).then(
                                fn=stream_chat,
                                inputs=[q2, chatbot2, ctx2],
                                outputs=[chatbot2, status2, ctx2],
                            )

                        send2.click(
                            fn=stream_chat,
                            inputs=[q2, chatbot2, ctx2],
                            outputs=[chatbot2, status2, ctx2],
                        )
                        q2.submit(
                            fn=stream_chat,
                            inputs=[q2, chatbot2, ctx2],
                            outputs=[chatbot2, status2, ctx2],
                        )
                        clear2.click(
                            fn=clear_chat,
                            inputs=[ctx2],
                            outputs=[chatbot2, status2, ctx2],
                        )

            # ──────────────────────────────────────────────────────────────
            # 탭 3: 에세이 작성
            # ──────────────────────────────────────────────────────────────
            with gr.TabItem("✍️ 에세이 작성"):
                with gr.Row(equal_height=False):
                    with gr.Column(scale=1, min_width=240):
                        gr.Markdown(
                            f"### ✍️ 에세이 모델\n"
                            f"| | |\n|--|--|\n"
                            f"| 모델 | `{ea.ESSAY_MODEL}` |\n"
                            f"| 드래프트 온도 | `{ea.ESSAY_TEMPERATURE}` |\n"
                            f"| 수정 온도 | `{ea.REVISE_TEMPERATURE}` |\n\n"
                            f"**Common App** {len(ea.COMMON_APP_PROMPTS)}개 · "
                            f"**Supplemental** {sum(len(v) for v in ea.SUPPLEMENTAL_PROMPTS.values())}개",
                            elem_classes=["profile-box"],
                        )
                        gr.Markdown("---")
                        gr.Markdown(ea.list_prompts())

                    with gr.Column(scale=3):
                        with gr.Row():
                            essay_school = gr.Dropdown(
                                choices=["선택", "cornell", "stanford", "nyu", "upenn"],
                                value="선택", label="학교", scale=1,
                            )
                            essay_prompt_key = gr.Dropdown(
                                choices=["선택"] + list(ea.COMMON_APP_PROMPTS.keys()) + ["why", "community", "intellectual"],
                                value="선택", label="프롬프트", scale=2,
                            )
                            essay_mode = gr.Radio(
                                choices=["draft", "revise", "critique"],
                                value="draft", label="모드", scale=1,
                            )
                        essay_story = gr.Textbox(
                            label="포함할 이야기 / 핵심 아이디어",
                            placeholder="예) 의료 AI 연구 동아리 경험, Cornell의 특정 연구실 연결, 공부 카페 창업 이야기...",
                            lines=3,
                        )
                        essay_existing = gr.Textbox(
                            label="기존 에세이 (revise/critique 모드에서 붙여넣기)",
                            placeholder="기존에 작성한 에세이 붙여넣기...",
                            lines=8,
                        )
                        with gr.Row():
                            essay_run   = gr.Button("✍️ 에세이 작성", variant="primary", size="lg", scale=3)
                            essay_clear = gr.Button("🗑 초기화", size="lg", scale=1)
                        essay_out = gr.Textbox(label="에세이 출력", lines=22, max_lines=60, interactive=False)

                        with gr.Accordion("⚡ 빠른 시작 프리셋", open=False):
                            _essay_presets = [
                                ("Cornell Why Engineering", "cornell", "why",
                                 "Cornell CAS 또는 CoE에서 AI+의료 융합 연구. Technion 협력 연구실 연결."),
                                ("Stanford Intellectual Vitality", "stanford", "intellectual",
                                 "LLM 온도 한계 문제 발견 — 응용 관점에서 발견한 스케일링 문제와 연구."),
                                ("Common App Challenge", "선택", "challenge",
                                 "한성과고 수시 2위로 실패 후 재도전한 경험. 실패에서 배운 회복력과 성장."),
                            ]
                            for _lbl, _sch, _pk, _st in _essay_presets:
                                _btn = gr.Button(_lbl, size="sm")
                                _btn.click(
                                    fn=lambda s=_sch, pk=_pk, st=_st: (s, pk, st),
                                    outputs=[essay_school, essay_prompt_key, essay_story],
                                )

                        essay_run.click(
                            fn=stream_essay_ui,
                            inputs=[essay_school, essay_prompt_key, essay_story, essay_existing, essay_mode],
                            outputs=essay_out,
                        )
                        essay_clear.click(
                            fn=lambda: ("선택", "선택", "", "", ""),
                            outputs=[essay_school, essay_prompt_key, essay_story, essay_existing, essay_out],
                        )

            # ──────────────────────────────────────────────────────────────
            # 탭 4: 이메일 커뮤니케이션
            # ──────────────────────────────────────────────────────────────
            with gr.TabItem("✉️ 이메일 커뮤니케이션"):
                with gr.Tabs():

                    with gr.TabItem("✏️ 작성 + 발송"):
                        with gr.Row(equal_height=False):
                            with gr.Column(scale=1, min_width=220):
                                gr.Markdown(_profile_md(), elem_classes=["profile-box"])
                                gr.Markdown("---")
                                gr.Markdown(em.list_admissions_emails())
                                gr.Markdown("---")
                                gr.Textbox(
                                    label="자격증명 상태",
                                    lines=4, interactive=False,
                                    value=em.check_credentials(),
                                )

                            with gr.Column(scale=3):
                                with gr.Row():
                                    email_type_dd = gr.Dropdown(
                                        choices=list(em.EMAIL_TYPES.keys()),
                                        value="inquiry", label="이메일 유형", scale=1,
                                    )
                                    email_school_dd = gr.Dropdown(
                                        choices=["선택"] + list(em.ADMISSIONS_EMAILS.keys()),
                                        value="선택", label="대학", scale=1,
                                    )
                                    email_provider_dd = gr.Dropdown(
                                        choices=["auto", "naver_works", "naver", "gmail"],
                                        value="auto", label="발송 계정", scale=1,
                                    )
                                with gr.Row():
                                    email_to = gr.Textbox(
                                        label="수신자 이메일 (비워두면 대학 선택 시 자동 입력)",
                                        placeholder="admissions@cornell.edu", scale=2,
                                    )
                                    email_recipient = gr.Textbox(
                                        label="수신자 이름/호칭",
                                        placeholder="Prof. Chen / 김철수 선생님", scale=2,
                                    )
                                email_topic = gr.Textbox(
                                    label="이메일 주제 / 목적 (필수)",
                                    placeholder="예) TOEFL waiver 가능 여부 문의 / 편입 추천서 요청 / 영문 성적증명서 2부 요청",
                                    lines=2,
                                )
                                email_extra = gr.Textbox(
                                    label="추가 컨텍스트 (선택)",
                                    placeholder="예) 이전 담당자 답변 내용, 특이 사항, 이전 이메일 스레드...",
                                    lines=2,
                                )
                                with gr.Row():
                                    email_draft_btn = gr.Button("🤖 AI 초안 작성", variant="primary", size="lg", scale=3)
                                    email_send_btn  = gr.Button("📤 실제 발송", variant="stop", size="lg", scale=1)

                                email_draft_out = gr.Textbox(
                                    label="이메일 초안 미리보기 + 코치 노트",
                                    lines=18, max_lines=40, interactive=False,
                                )
                                send_result_out = gr.Textbox(
                                    label="발송 결과", lines=3, interactive=False,
                                )
                                gr.Markdown(
                                    "> ⚠️ **Gmail 앱 비밀번호 필요**: "
                                    "`GMAIL_USER` + `GMAIL_APP_PASSWORD` 환경변수 또는 `.env` 파일 설정  \n"
                                    "> 발급: https://myaccount.google.com/apppasswords"
                                )

                                with gr.Accordion("⚡ 빠른 입력 프리셋", open=True):
                                    _email_presets = [
                                        ("Cornell TOEFL waiver 문의", "inquiry", "cornell",
                                         "transfer_admissions@cornell.edu", "",
                                         "TOEFL waiver eligibility for Hansung Science HS graduate with AI major"),
                                        ("Stanford 일정 문의", "inquiry", "stanford",
                                         "admission@stanford.edu", "",
                                         "Transfer application timeline and interview process for Fall 2026"),
                                        ("교수님 추천서 요청", "rec_request", "선택",
                                         "", "Prof. [NAME]",
                                         "Cornell/Stanford 전후로 제출할 추천서 요청\n마감: 2026-04-01\n수업명: AI 기초론\n기억에 남는 프로젝트: 개인 LLM 업하 프로젝트"),
                                    ]
                                    for _lbl, _et, _sc, _to, _rn, _tp in _email_presets:
                                        _btn = gr.Button(_lbl, size="sm")
                                        _btn.click(
                                            fn=lambda et=_et, sc=_sc, t=_to, rn=_rn, tp=_tp: (et, sc, t, rn, tp),
                                            outputs=[email_type_dd, email_school_dd, email_to,
                                                     email_recipient, email_topic],
                                        )

                                email_draft_btn.click(
                                    fn=stream_email_ui,
                                    inputs=[email_type_dd, email_to, email_school_dd,
                                            email_recipient, email_topic, email_extra, email_provider_dd],
                                    outputs=email_draft_out,
                                )
                                email_send_btn.click(
                                    fn=do_send_email,
                                    inputs=[email_provider_dd],
                                    outputs=send_result_out,
                                )

                    with gr.TabItem("📬 수신함"):
                        inbox_provider = gr.Dropdown(
                            choices=["auto", "naver_works", "naver", "gmail"],
                            value="gmail",
                            label="조회 계정",
                        )
                        inbox_filter = gr.Textbox(
                            label="발신자 필터 (선택)",
                            placeholder="예) cornell.edu — 비워두면 전체",
                        )
                        with gr.Row():
                            inbox_refresh = gr.Button("🔄 수신함 새로고침", variant="secondary")
                            important_refresh = gr.Button("📌 중요 대학 메일 요약", variant="primary")
                        inbox_out = gr.Textbox(label="받은 이메일", lines=20, interactive=False)
                        important_out = gr.Textbox(
                            label="중요 메일 요약",
                            lines=16,
                            max_lines=30,
                            interactive=False,
                        )
                        inbox_refresh.click(fn=load_inbox, inputs=[inbox_filter, inbox_provider], outputs=inbox_out)
                        important_refresh.click(
                            fn=load_important_inbox_summary,
                            inputs=[inbox_provider],
                            outputs=important_out,
                        )

                    with gr.TabItem("📊 발송 이력"):
                        hist_refresh = gr.Button("🔄 발송 이력 새로고침")
                        hist_out = gr.Textbox(label="발송 이력", lines=20, interactive=False)
                        hist_refresh.click(fn=load_email_history, outputs=hist_out)

    return app


# ══════════════════════════════════════════════════════════════════════════════
# 진입점
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 64)
    print("  🎓 편입 통합 에이전트 UI 시작 (7_unified_agent 연동)")
    print(f"  검색 모델:    {ua.SEARCH_MODEL}  (temp=0)")
    print(f"  어드바이저:   {ua.ADVISOR_MODEL}  (temp={ua.ADVISOR_TEMPERATURE})")
    print("  URL: http://127.0.0.1:7862")
    print("=" * 64)

    _host = os.getenv("UI_HOST", "0.0.0.0")
    _port = int(os.getenv("UI_PORT", "7862"))

    app = build_ui()
    app.launch(
        server_name=_host,
        server_port=_port,
        inbrowser=True,
        show_error=True,
        theme=_THEME,
        css=_CSS,
    )

