import sys, ollama, json
fp = sys.argv[1]
client = ollama.Client()
r = client.chat(model='qwen3-vl:8b', messages=[{'role':'user','content':'describe in 10 words','images':[fp]}], options={'num_ctx':512,'temperature':0})
print(json.dumps(r, indent=2, default=str)[:500])
