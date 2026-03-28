import sqlite3

conn = sqlite3.connect('processed_files.db')
total = conn.execute('SELECT COUNT(*) FROM processed').fetchone()[0]
hansung = conn.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%한성과고%'").fetchone()[0]
korea = conn.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%고려대학교%'").fetchone()[0]
takeout = conn.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%Takeout%'").fetchone()[0]
other = total - hansung - korea - takeout
print(f'=== DB 현황 ===')
print(f'전체: {total:,}건')
print(f'  한성과고: {hansung:,}건  (목표: 10,129건, 미처리: {10129-hansung:,}건)')
print(f'  고려대학교: {korea:,}건')
print(f'  Takeout: {takeout:,}건')
print(f'  기타: {other:,}건')
conn.close()
