import ollama, base64, urllib.request

client = ollama.Client()

# 실제 한글 이미지 URL (Wikipedia 한글 샘플)
url = 'https://upload.wikimedia.org/wikipedia/commons/thumb/4/4c/Hangeul.svg/320px-Hangeul.svg.png'
try:
    data = urllib.request.urlopen(url, timeout=15).read()
    b64 = base64.b64encode(data).decode()
    print(f"Image downloaded: {len(data)} bytes")
    
    r = client.chat(model='llava:7b', messages=[{
        'role': 'user',
        'content': 'Read and transcribe ALL text visible in this image. Output the exact characters you see.',
        'images': [b64]
    }], options={'temperature': 0})
    print("RESPONSE:", r.message.content[:600])
except Exception as e:
    print("ERR:", e)

# 다른 한국어 텍스트 이미지
url2 = 'https://upload.wikimedia.org/wikipedia/commons/thumb/7/72/Korean_language_title.png/400px-Korean_language_title.png'
try:
    data2 = urllib.request.urlopen(url2, timeout=15).read()
    b64_2 = base64.b64encode(data2).decode()
    print(f"\nImage2 downloaded: {len(data2)} bytes")
    
    r2 = client.chat(model='llava:7b', messages=[{
        'role': 'user',
        'content': 'What Korean text do you see? Read it exactly.',
        'images': [b64_2]
    }], options={'temperature': 0})
    print("RESPONSE2:", r2.message.content[:600])
except Exception as e2:
    print("ERR2:", e2)
