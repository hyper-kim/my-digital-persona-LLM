import sys, os, tempfile, ollama
fp    = sys.argv[1]
model = sys.argv[2]
dpi   = int(sys.argv[3]) if len(sys.argv) > 3 else 150
ext   = fp.rsplit('.', 1)[-1].lower()
prompt = "describe this image briefly in english"
client  = ollama.Client()
results = []
temps   = []
try:
    r = client.chat(model=model,
                    messages=[{'role': 'user', 'content': prompt, 'images': [fp]}],
                    keep_alive=-1, options={'num_ctx': 4096, 'temperature': 0})
    _txt = getattr(r.message, 'content', '') or getattr(r.message, 'thinking', '') or ''
    results.append(_txt)
finally:
    if os.path.exists(fp):
        os.unlink(fp)
print('\n\n'.join(results))
