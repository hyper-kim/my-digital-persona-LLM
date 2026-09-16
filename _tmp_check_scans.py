import os

try:
    import fitz
    USE_FITZ = True
except ImportError:
    import pdfplumber
    USE_FITZ = False

folder = r'G:\내 드라이브\해외 대학 편입 정리\한성과고\시험지 및 종이 필기'
targets = [f for f in os.listdir(folder)
           if any(x in f for x in ['SCAN_2026', 'img2026', 'Untitled_2026'])]

print(f'대상 파일 {len(targets)}개')

for fname in sorted(targets):
    fpath = os.path.join(folder, fname)
    print(f'\n=== {fname} ===')
    try:
        if USE_FITZ:
            doc = fitz.open(fpath)
            txt = ''
            for pg in doc:
                txt += pg.get_text()
            doc.close()
        else:
            with pdfplumber.open(fpath) as pdf:
                txt = '\n'.join(p.extract_text() or '' for p in pdf.pages[:3])
        txt = txt.strip()
        if txt:
            print(txt[:600])
        else:
            print('[텍스트 레이어 없음 - 이미지 스캔 PDF]')
    except Exception as e:
        print(f'[에러] {e}')
