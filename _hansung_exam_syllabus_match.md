# 한성과학고 실시험 자료 기반 — 편입 Required 과목 충족 분석

> **생성일**: 2026-03-31 (완전 재작성)  
> **데이터 소스**: 아래 3가지 폴더만 사용 (실제로 치른 시험·수강 증거)
> 1. `한성과고/시험지 및 종이 필기/` — 실제 시험지, 필기 스캔본, 수업 프린트
> 2. `한성과고/원노트/` 내 파일명에 `T` 포함 파일 (선생 수업 노트 = 정규 수강 증거)
> 3. `한성과고/삼성노트/수학/성T/` — 성T 선생 Stewart Calculus 원서 공부 기록
>
> **VM GPU CV 처리 현황**: 2026-03-31 전체 재처리 개시  
> - qwen3-vl:8b, LaTeX 수식·그래프·회로도 완전 추출 모드 (`FORCE_VM_ALL_FILES=true`, `SKIP_LOCAL_OCR=true`)  
> - DB·Qdrant 초기화 후 한성과고 폴더 10,140건 전부 재임베딩 중
>
> **분석 목적**: **편입 합격 심사 기준** — 각 프로그램 Required 선수과목 충족 여부  
> **지원 프로그램**: NYU Tandon CS / UPenn SEAS AI / Cornell CAS / Purdue CE / UIUC CS+X / Northwestern LAS·공대 / UChicago

---

## 📂 실제 시험·수업 자료 증거 인벤토리

### 1. 시험지 및 종이 필기 폴더

| 파일명 | 시기 | 확인된 과목 |
|---|---|---|
| `1학년 수학 필기.pdf` | 고1 | 수학 필기 |
| `1학년 시험1.pdf`, `1학년 시험20001.pdf` | 고1 | 고1 정기시험 |
| `고화 2-1 중간 4 5.pdf` | 고2-1학기 중간 | **고급화학** 4·5단원 시험 |
| `2-2 고생 중간 누락본.pdf` | 고2-2학기 중간 | **고급생명과학** 시험 |
| `2-2 영어1 기말 8p.pdf` | 고2-2학기 기말 | 영어1 시험 |
| `2학년 시험.pdf`, `2학년 B4 시험지.pdf` | 고2 | 고2 정기시험 |
| `3학년 시험.pdf`, `시험지.pdf`, `b4 시험지.pdf` | 고3 | 고3 정기시험 |
| `61 Inverse functions_240401.pdf` | 고3 (2024.04) | **AP Calculus** Lesson 61 — 역함수·역함수 미분 |
| `62 Derivatives of logarithmic_240401.pdf` | 고3 (2024.04) | **AP Calculus** Lesson 62 — 로그함수 미분 |
| `63 Indeterminate forms_240415.pdf` | 고3 (2024.04) | **AP Calculus** Lesson 63 — 부정형·로피탈 |
| `64 Derivatives of inverse trig_240415.pdf` | 고3 (2024.04) | **AP Calculus** Lesson 64 — 역삼각함수 미분 |
| `65 Integrals involving inverse_240418.pdf` | 고3 (2024.04) | **AP Calculus** Lesson 65 — 역삼각함수 적분 |
| `66 Natural logarithm integral_240418.pdf` | 고3 (2024.04) | **AP Calculus** Lesson 66 — 자연로그 적분 |
| `67 Exponential growth decay_240418.pdf` | 고3 (2024.04) | **AP Calculus** Lesson 67 — 지수 성장·감쇠 |
| `AP 미적분학 C-1(6장 합본_240421).pdf` | 고3 (2024.04) | **AP Calculus** 6장 합본 — 적분 심화 **(Qdrant 실텍스트 확인)** |
| `AP 미적분학 C-1(6장 합본_240425).pdf` | 고3 (2024.04) | **AP Calculus** 6장 합본 정답 **(Qdrant 실텍스트 확인)** |
| `SCAN_20260322_*.pdf` (×4) | **한성과고 시험지** (2026년 스캔) | 한성과고 시험지 — VM CV OCR 재처리 중 |
| `img20260323_*.pdf` (×2) | **한성과고 시험지** (2026년 스캔) | 한성과고 시험지 — VM CV OCR 재처리 중 |
| `Untitled_20260328_*.pdf` (×8, 193-224 MB) | **한성과고 필기** (2026년 스캔) | 손필기·수식 대량 스캔 — VM CV OCR 재처리 중 |

> ⚠️ SCAN/img/Untitled 파일: 날짜는 2026년(스캔 날짜)이지, **내용은 모두 한성과고 시험지·필기**.  
> VM GPU qwen3-vl:8b CV 파이프라인이 LaTeX 수식·그래프·회로도 완전 추출로 재처리 완료 후 내용 확인 가능.

---

### 2. 원노트 — T(교사명) 포함 파일 목록

| 파일명 | 학년-학기 | 과목 | 교사 수 |
|---|---|---|---|
| `역학.pdf` | 고1 | 역학 (Newton 역학, 운동량·에너지) | — |
| `전자기학.pdf` | 고1 | 전자기학 (전기장·자기장·회로) | — |
| `화학.pdf` | 고1 | 화학 기초 | — |
| `현대물리파동.pdf` | 고1 | 현대물리·파동 (양자역학 입문) | — |
| `생물필기.pdf` | 고1 | 생물 필기 | — |
| `수학 명제증명.pdf` | 고1 | 수학 — 논리·증명 | — |
| `수학 순열조합.pdf` | 고1 | 수학 — 순열·조합·확률 기초 | — |
| `고급물리학 T *.pdf` (×3 파일) | 고2 | **고급물리학** — Halliday 수준 역학·전자기·현대물리 | T 3인 |
| `고급화학 T *.pdf` (×3 파일) | 고2 | **고급화학** — 원소·반응속도·열화학·전기화학 | T 3인 |
| `고급생명과학 T *.pdf` (×3 파일) | 고2 | **고급생명과학** — 세포·유전·진화 | T 3인 |
| `고급지구과학 T *.pdf` (×4 파일) | 고2 | 고급지구과학 | T 4인 |
| `미적분 T *.pdf` (×3 파일) | 고2 | **미적분** — 극한·미분·기초 적분 정규 수업 | T 3인 |
| `심화수학2 T *.pdf` (×2 파일) | 고2 | **심화수학2** — 수열·급수·복소수·행렬 | T 2인 |
| `확통 T *.pdf` (×2 파일) | 고2 | **확률과 통계** — 확률·분포·통계적 추정 | T 2인 |
| `Halliday 과외.pdf` | 고2 | **Halliday Physics** 과외 (대학 일반물리 수준) | — |
| `AP Calculus T *.pdf` (×4 파일) | 고3 | **AP Calculus BC** 정규 수업 — 극한·미적분·급수·벡터 | T 4인 |
| `AP Calculus 방학.pdf` | 고3 방학 | AP Calc 방학 집중 | — |
| `물리학실험 T *.pdf` (×2 파일) | 고3 | **물리학실험** — 실험 보고서·데이터 분석 | T 2인 |
| `화학실험 T *.pdf` (×3 파일) | 고3 | **화학실험** — 실험 보고서 | T 3인 |
| `생명과학실험 T *.pdf` (×2 파일) | 고3 | 생명과학실험 | T 2인 |

---

### 3. 삼성노트 — 수학/성T 폴더

| 파일명 | 내용 | 의미 |
|---|---|---|
| `Stewart Calculus Ch1-4.pdf` | 원서 Ch1(함수·극한) ~ Ch4(미분 응용) | Stewart Calc 1학기분 직접 공부 |
| `Stewart Calculus Ch11.pdf` × 2회독 | Ch11 무한급수·Taylor/Maclaurin | 급수 파트 2회독 |
| `성T 증명 2회독.pdf` | ε-δ 증명, 미적분 기본정리 증명, 극한 엄밀 증명 | 수학적 엄밀성 증거 (대학원 입학 수준) |
| `AP Calculus 암기집.pdf` | AP Calc BC 공식·정리 모음 | AP Calc BC 체계적 준비 증거 |

---

## 🎓 대학별 Required 과목 충족 분석

### 공통 충족 현황

| 선수 영역 | 증거 파일 | 강도 |
|---|---|---|
| **Calculus 1** (극한·미분·기초 적분) | 미적분 T × 3, Stewart Ch1-4, AP Calc T × 4, 성T 증명 2회독 | ★★★★★ |
| **Calculus 2** (적분 기법·급수·Taylor) | AP Calc T × 4, Stewart Ch11 × 2회독, AP Calc 합본_240421, AP Calc 방학 | ★★★★★ |
| **Physics 1** (역학·파동·열) | 역학.pdf, 고급물리학 T × 3, Halliday 과외, 물리학실험 T × 2 | ★★★★★ |
| **Physics 2** (전자기·광학·현대물리) | 전자기학.pdf, 현대물리파동.pdf, 고급물리학 T × 3, Halliday 과외 | ★★★★★ |
| **General Chemistry** | 화학.pdf, 고급화학 T × 3, 고화 2-1 중간 시험, 화학실험 T × 3 | ★★★★★ |
| **Probability & Statistics** | 확통 T × 2 | ★★★☆☆ |
| **Calculus 3** (다변수·벡터해석) | 심화수학2 T × 2 + AP Calc BC (벡터 일부) | ★★★☆☆ (부분) |
| **Biology** | 생물필기.pdf, 고급생명과학 T × 3, 생명과학실험 T × 2 | ★★★★☆ |
| **Circuit Analysis** | 전자기학.pdf + 스캔 OCR 완료 후 확인 | ★★★☆☆ → 갱신 예정 |
| **Linear Algebra** | 없음 (고등 정규과정 없음) | ❌ |
| **Discrete Math** | 수학 명제증명.pdf (논리·증명 부분) | ★★☆☆☆ |

---

### ① NYU Tandon — CS (Transfer)

| Required 과목 | 충족 | 근거 |
|---|---|---|
| Calculus 1 | ✅ | 미적분 T × 3, Stewart Ch1-4, 성T 증명 2회독 |
| Calculus 2 | ✅ | AP Calc T × 4, Stewart Ch11 × 2회독, AP Calc 합본 |
| Physics 1 | ✅ | 고급물리학 T × 3, Halliday 과외, 역학.pdf |
| Physics 2 | ✅ | 고급물리학 T × 3, 전자기학.pdf, 현대물리파동.pdf |
| General Chemistry | ✅ | 고급화학 T × 3, 고화 2-1 중간 시험 |
| Intro to CS / Programming | ⚠️ | 없음 — 고려대 AI학과 학점 필요 |
| Discrete Math | ⚠️ | 명제증명.pdf 부분 충족 |
| Linear Algebra | ❌ | 고등 과정 없음 — 고려대 수강 필요 |
| Data Structures | ❌ | 없음 |

**충족: 5/9** — 이공계 기초(Cal·Phy·Chem) 완전 충족. Linear Algebra·CS는 고려대 학점으로 보완.

---

### ② UPenn SEAS — AI (Transfer)

| Required 과목 | 충족 | 근거 |
|---|---|---|
| Calculus 1 | ✅ | 미적분 T × 3, Stewart Ch1-4, 성T 증명 2회독 |
| Calculus 2 | ✅ | AP Calc T × 4, Stewart Ch11 × 2회독 |
| Multivariable Calculus (Calc 3) | ⚠️ | 심화수학2·AP Calc BC (편미분·중적분 직접 증거 미흡) |
| Linear Algebra | ❌ | 없음 |
| Probability & Statistics | ✅ | 확통 T × 2 |
| Physics 1 | ✅ | 고급물리학 T × 3, Halliday 과외 |
| Intro to Programming | ⚠️ | 없음 |
| Data Structures / Algorithms | ❌ | 없음 |
| Intro to AI / ML | ❌ | 없음 — 고려대 AI학과 전공 과목으로 충당 |
| Technical Writing / English | ⚠️ | 영어1 기말 시험 부분 근거 |

**충족: 4/10** — 수학·물리 기초는 강력. CS·AI 계열 Missing은 고려대 AI학과 전공 학점이 핵심.

---

### ③ Cornell CAS (Math/CS 또는 Physics 방면)

| Required 과목 | 충족 | 근거 |
|---|---|---|
| Calculus 1 | ✅ | 미적분 T × 3, Stewart Ch1-4, 성T 증명 2회독 |
| Calculus 2 | ✅ | AP Calc T × 4, Stewart Ch11 × 2회독, AP Calc 합본 |
| Calculus 3 (Multivariable) | ⚠️ | 심화수학2, AP Calc BC 벡터 일부 |
| Physics 1 | ✅ | 고급물리학 T × 3, Halliday 과외, 물리학실험 T × 2 |
| Physics 2 | ✅ | 전자기학.pdf, 현대물리파동.pdf, 고급물리학 T × 3 |
| Chemistry | ✅ | 고급화학 T × 3, 화학실험 T × 3 |
| Linear Algebra | ❌ | 없음 |
| Intro CS / Programming | ⚠️ | 없음 |
| Discrete Math | ⚠️ | 명제증명 부분 |

**충족: 5/9** — Physics·Chemistry 완벽. AP Calc BC 실수강이 강점.

---

### ④ Purdue CE (Computer Engineering)

| Required 과목 | 충족 | 근거 |
|---|---|---|
| Calculus 1 | ✅ | 미적분 T × 3, Stewart Ch1-4 |
| Calculus 2 | ✅ | AP Calc T × 4, Stewart Ch11 × 2회독 |
| Calculus 3 | ⚠️ | 심화수학2 일부 |
| Differential Equations | ⚠️ | 직접 증거 없음 |
| Physics 1 | ✅ | 고급물리학 T × 3, Halliday 과외 |
| Physics 2 | ✅ | 전자기학.pdf, 고급물리학 T × 3 |
| Intro to Programming (C) | ❌ | 없음 |
| Circuit Analysis | ⚠️ → ✅? | 전자기학.pdf + 스캔 시험지 CV OCR 결과 후 결정 |
| General Chemistry | ✅ | 고급화학 T × 3, 화학실험 T × 3 |

**충족: 5/9** — 물리·화학 완벽. Circuit Analysis는 스캔 파일 CV OCR 후 ✅ 가능성 높음. C 언어·미분방정식이 약점.

---

### ⑤ UIUC CS+X

| Required 과목 | 충족 | 근거 |
|---|---|---|
| Calculus 1 | ✅ | 미적분 T × 3, Stewart Ch1-4, 성T 증명 2회독 |
| Calculus 2 | ✅ | AP Calc T × 4, Stewart Ch11 × 2회독 |
| Physics 1 | ✅ | 고급물리학 T × 3, Halliday 과외 |
| Statistics / Probability | ✅ | 확통 T × 2 |
| Linear Algebra / Discrete Math | ⚠️ | 명제증명(논리·집합), 심화수학2(행렬 일부) |
| Intro to CS / Programming | ❌ | 없음 |
| Data Structures | ❌ | 없음 |
| X 분야 과목 (Physics 선택 시) | ✅ | 고급물리학 T × 3, 물리학실험 T × 2 |
| Chemistry (X 분야에 따라) | ✅ | 고급화학 T × 3 |

**충족: 5/9** — X 분야를 **Physics**로 설정 시 기초과학 완벽. CS 기초(프로그래밍·자료구조)는 고려대 필수.

---

### ⑥ Northwestern — LAS 또는 McCormick Engineering

| Required 과목 | 충족 | 근거 |
|---|---|---|
| Calculus 1 | ✅ | 미적분 T × 3, Stewart Ch1-4 |
| Calculus 2 | ✅ | AP Calc T × 4, Stewart Ch11 × 2회독 |
| Physics 1 (GEN PHYS 135) | ✅ | 고급물리학 T × 3, Halliday 과외 |
| Physics 2 (GEN PHYS 136) | ✅ | 전자기학.pdf, 현대물리파동.pdf, 고급물리학 T × 3 |
| Chemistry | ✅ | 고급화학 T × 3, 화학실험 T × 3 |
| Statistics | ✅ | 확통 T × 2 |
| Intro CS / Programming | ❌ | 없음 |
| Linear Algebra | ❌ | 없음 |

**충족: 6/8** — LAS 기초과학·수학으로는 매우 강함. McCormick(공대) 트랙은 Programming·Linear Algebra 보완 필요.

---

### ⑦ UChicago — 편입

| Required 과목 | 충족 | 근거 |
|---|---|---|
| Calculus 1 | ✅ | 미적분 T × 3, Stewart Ch1-4, 성T 증명 2회독 |
| Calculus 2 | ✅ | AP Calc T × 4, Stewart Ch11 × 2회독 |
| Calculus 3 | ⚠️ | 심화수학2 (급수·수열), AP Calc BC (벡터 일부) |
| Physics (Mechanics) | ✅ | 역학.pdf, 고급물리학 T × 3, Halliday 과외 |
| Chemistry | ✅ | 고급화학 T × 3, 화학실험 T × 3 |
| Probability & Statistics | ✅ | 확통 T × 2 |
| Biological Science | ✅ | 고급생명과학 T × 3, 생명과학실험 T × 2 |
| Humanities / Writing | ⚠️ | 영어1 기말 부분 |
| Linear Algebra | ❌ | 없음 |

**충족: 6/9** — UChicago Core 기초과학 요건 폭이 가장 넓게 충족됨.

---

## 📊 대학별 충족률 요약

| 대학 | 프로그램 | 충족 | 주요 약점 (보완 경로) |
|---|---|---|---|
| **Northwestern LAS** | LAS 이공계 | 6/8 ✅ | Programming, Linear Algebra (고려대) |
| **UChicago** | Core+전공 | 6/9 ✅ | Linear Algebra, Calc 3 (고려대), Humanities |
| **NYU Tandon** | CS | 5/9 ✅ | Linear Algebra, CS, Discrete Math (고려대) |
| **Cornell CAS** | Math·Physics | 5/9 ✅ | Linear Algebra, Discrete Math, CS (고려대) |
| **Purdue CE** | Comp Eng | 5/9 ✅ | C 언어, 미분방정식, Circuit (CV OCR 후 확인) |
| **UIUC CS+X** | CS+Physics | 5/9 ✅ | Programming, Data Struct (고려대) |
| **UPenn SEAS** | AI | 4/10 ✅ | Calc 3, Linear Algebra, CS/AI 다수 (고려대) |

> **공통 절대 강점**: Calculus 1·2, Physics 1·2, Chemistry — 7개 대학 모두 완전 충족  
> **공통 약점**: Linear Algebra, Calculus 3 직접 증거 부족, Programming — 고려대 AI학과 전공 학점으로 전원 보완 가능

---

## 🔄 CV OCR 완료 후 업데이트 예정 항목

| 파일 | 기대 내용 | 영향 대학 |
|---|---|---|
| `SCAN_20260322_*.pdf` (×4) | BJT·증폭기 회로, 물리 시험 | Purdue CE Circuit ✅ 가능 |
| `img20260323_*.pdf` (×2) | 과학고급 시험 내용 | 각 과목 강도 상향 |
| `Untitled_20260328_*.pdf` (×8, 193-224MB) | 심화 수학·수식·그래프 대량 | Calc 3·Linear Algebra 부분 충족 가능 |

---

*본 분석은 지정된 3개 폴더`(시험지 및 종이 필기 / 원노트 T 파일 / 삼성노트 성T)`만을 근거로 함.  
각 대학 Required 이수 기준은 2024-2025 공시 기준이며 편입 사정관 재량에 따라 달라질 수 있음.  
Linear Algebra·CS 계열 Missing 항목은 고려대 AI학과 수강 기록(~60학점)으로 충당 예정.*
