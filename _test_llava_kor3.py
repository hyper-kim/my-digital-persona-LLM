import ollama, base64, subprocess, os

client = ollama.Client()

# 1단계: VM에서 한글 폰트 찾기 + PIL로 한글 이미지 생성
gen_script = '''
import PIL.Image, PIL.ImageDraw, PIL.ImageFont, base64, io, os, glob

# 한글 폰트 찾기
font_candidates = glob.glob('/usr/share/fonts/**/*.ttf', recursive=True)
korean_fonts = [f for f in font_candidates if any(k in f.lower() for k in ['nanum', 'noto', 'korean', 'cjk', 'gothic', 'dotum', 'gulim'])]
print("Korean fonts found:", korean_fonts[:5])

font = None
for fp in korean_fonts[:3]:
    try:
        font = PIL.ImageFont.truetype(fp, 24)
        print("Using font:", fp)
        break
    except:
        pass

if font is None:
    print("NO KOREAN FONT - using default")
    font = PIL.ImageFont.load_default()

# 한국어 텍스트 이미지 생성
text = "안녕하세요\\n대학 편입 지원서\\nGPA: 4.0/4.5\\n지원 대학: 코넬, 스탠퍼드"
img = PIL.Image.new("RGB", (500, 160), "white")
draw = PIL.ImageDraw.Draw(img)
draw.multiline_text((10, 10), text, fill="black", font=font)
buf = io.BytesIO()
img.save(buf, format='PNG')
b64 = base64.b64encode(buf.getvalue()).decode()
print("IMAGE_B64_START")
print(b64[:50] + "..." + b64[-20:])
print("IMAGE_B64_LEN:", len(b64))

# 저장
with open("/tmp/test_korean.png", "wb") as f:
    f.write(buf.getvalue())
print("Saved /tmp/test_korean.png")
'''

with open('/tmp/_gen_img.py', 'w') as f:
    f.write(gen_script)

# 이미지 생성 후 OCR 테스트
ocr_script = '''
import ollama, base64

client = ollama.Client()
with open("/tmp/test_korean.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

print("=== llava:7b Korean OCR Test ===")
r = client.chat(model="llava:7b", messages=[{
    "role": "user",
    "content": "이 이미지에 있는 텍스트를 정확히 읽어주세요. 모든 글자를 그대로 출력해주세요.",
    "images": [b64]
}], options={"temperature": 0})
print("Korean prompt:", repr(r.message.content[:500]))
print()

r2 = client.chat(model="llava:7b", messages=[{
    "role": "user",
    "content": "Read ALL text in this image exactly as written. Transcribe every character.",
    "images": [b64]
}], options={"temperature": 0})
print("English prompt:", repr(r2.message.content[:500]))
'''

with open('/tmp/_ocr_test.py', 'w') as f:
    f.write(ocr_script)

print("Scripts written. Run on VM:")
print("  python3 /tmp/_gen_img.py && python3 /tmp/_ocr_test.py")
