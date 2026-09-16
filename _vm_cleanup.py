import requests, subprocess, glob, os, time

# 1. qwen3-vl:8b VRAM 해제
try:
    r = requests.post('http://localhost:11434/api/generate',
                      json={'model': 'qwen3-vl:8b', 'keep_alive': 0}, timeout=15)
    print('qwen3-vl unload:', r.status_code)
except Exception as e:
    print('qwen3-vl unload err:', e)

time.sleep(3)

# 2. 임시파일 정리
removed = 0
for pat in ['/tmp/omni_*', '/tmp/omni_lo_*']:
    for f in glob.glob(pat):
        try:
            if os.path.isfile(f):
                os.unlink(f)
            elif os.path.isdir(f):
                import shutil; shutil.rmtree(f, ignore_errors=True)
            removed += 1
        except:
            pass
print(f'임시파일 {removed}개 삭제')

# 3. VRAM 현황
r2 = subprocess.run(['nvidia-smi','--query-gpu=utilization.gpu,memory.used,memory.free','--format=csv,noheader'],
                    capture_output=True, text=True)
print('GPU:', r2.stdout.strip())

# 4. ollama ps
r3 = subprocess.run(['ollama','ps'], capture_output=True, text=True)
print('ollama ps:', r3.stdout.strip())
