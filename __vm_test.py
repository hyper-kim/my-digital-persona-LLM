import ollama, sys
c = ollama.Client()
try:
    r = c.chat(model='qwen3-vl:8b',
               messages=[{'role':'user','content':'hi, respond in one word'}],
               keep_alive=-1,
               options={'num_ctx':512,'temperature':0})
    print('GPU_OK:', r['message']['content'][:80])
except Exception as e:
    print('FAIL:', e)
    sys.exit(1)
