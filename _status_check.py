import sys, os, sqlite3
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

c = sqlite3.connect('processed_files.db')

rows = c.execute('SELECT file_path FROM processed ORDER BY rowid DESC LIMIT 5').fetchall()
print('=== 최근 처리 파일 ===')
for r in rows:
    print(' ', r[0][-100:])

total     = c.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
hansung   = c.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%한성과고%'").fetchone()[0]
pdf_h     = c.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%한성과고%' AND lower(file_path) LIKE '%.pdf'").fetchone()[0]
haewoe    = c.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%해외%' AND file_path NOT LIKE '%한성과고%'").fetchone()[0]

print(f'\n=== 처리 현황 ===')
print(f'전체: {total:,}')
print(f'한성과고: {hansung:,}  (PDF: {pdf_h})')
print(f'해외대학(한성과고 제외): {haewoe:,}')

# 한성과고 내 파일 타입 분류
exts = c.execute("""
    SELECT lower(substr(file_path, instr(file_path,'.',-1)+1)) as ext, COUNT(*)
    FROM processed
    WHERE file_path LIKE '%한성과고%'
    GROUP BY ext ORDER BY COUNT(*) DESC LIMIT 15
""").fetchall()
print('\n=== 한성과고 파일 타입 ===')
for e, cnt in exts:
    print(f'  .{e}: {cnt}')
