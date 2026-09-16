import ollama, base64, urllib.request

client = ollama.Client()

# 테스트 1: 순수 텍스트 한국어 질문
print("=== TEST 1: 텍스트 한국어 ===")
r = client.chat(model='llava:7b', messages=[{
    'role': 'user',
    'content': '안녕하세요. 이 문장을 한국어로 읽을 수 있나요? "대한민국 수도는 서울입니다." 위 문장의 의미를 영어로 설명해주세요.'
}], options={'temperature': 0})
print("RESPONSE:", r.message.content[:400])
print()

# 테스트 2: 한국어 텍스트가 포함된 실제 이미지로 OCR 테스트
# 간단한 한글 텍스트 PNG를 직접 만들기 (흰 배경에 "한국어" 글자)
try:
    from PIL import Image, ImageDraw, ImageFont
    import io
    img = Image.new('RGB', (400, 100), color='white')
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), "안녕하세요\n대학교 성적: 4.0/4.5\nGPA 환산: 3.51", fill='black')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode()
    
    print("=== TEST 2: 한글 이미지 OCR ===")
    r2 = client.chat(model='llava:7b', messages=[{
        'role': 'user',
        'content': 'Read ALL text in this image exactly as written. Output the exact text.',
        'images': [b64]
    }], options={'temperature': 0})
    print("RESPONSE:", r2.message.content[:400])
    print()
except ImportError:
    print("PIL 없음, 텍스트 테스트만 진행")

# 테스트 3: 실제 수식/표가 있는 이미지 simul (작은 회색 이미지)
import struct, zlib

def make_test_png():
    """간단한 800x200 흰 이미지 생성 (PIL 없이)"""
    w, h = 100, 50
    raw = b''
    for y in range(h):
        raw += b'\x00' + b'\xff' * (w * 3)
    compressed = zlib.compress(raw)
    
    def chunk(name, data):
        c = name + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', compressed)
    png += chunk(b'IEND', b'')
    return png

b64_blank = base64.b64encode(make_test_png()).decode()
print("=== TEST 3: 빈 흰 이미지 OCR (기준) ===")
r3 = client.chat(model='llava:7b', messages=[{
    'role': 'user',
    'content': 'What text do you see in this image?',
    'images': [b64_blank]
}], options={'temperature': 0})
print("RESPONSE:", r3.message.content[:200])
