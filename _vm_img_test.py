import sys, ollama
fp = sys.argv[1]
client = ollama.Client()
r = client.chat(model='qwen3-vl:8b', messages=[{'role':'user','content':'describe','images':[fp]}], options={'num_ctx':512,'temperature':0})
print('OK:', r['message']['content'][:100])
