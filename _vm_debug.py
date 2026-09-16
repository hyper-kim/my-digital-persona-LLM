import json, base64, httpx, os

fp = '/tmp/_test_img.jpg'
with open(fp, 'rb') as f:
    b64 = base64.b64encode(f.read()).decode()

for model in ['llava:7b', 'qwen3-vl:8b']:
    print(f"\n=== {model} ===")
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": "describe this image briefly", "images": [b64]}],
        "options": {"num_ctx": 4096, "temperature": 0}
    }
    resp = httpx.post('http://localhost:11434/api/chat', json=payload, timeout=300)
    data = resp.json()
    msg = data.get('message', {})
    content = msg.get('content', '') or ''
    thinking = msg.get('thinking', '') or ''
    print('done:', data.get('done'), '|', data.get('done_reason'))
    print('content:', repr(content[:300]) if content else 'EMPTY')
    print('thinking:', repr(thinking[:100]) if thinking else 'EMPTY')
