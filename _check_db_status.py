import sqlite3
c = sqlite3.connect('processed_files.db')
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('tables:', tables)
schema = c.execute("PRAGMA table_info(processed)").fetchall()
print('processed schema:', schema)
schema2 = c.execute("PRAGMA table_info(failed_files)").fetchall()
print('failed_files schema:', schema2)
total = c.execute("SELECT COUNT(1) FROM processed").fetchone()[0]
print('total processed:', total)
total_f = c.execute("SELECT COUNT(1) FROM failed_files").fetchone()[0]
print('total failed:', total_f)
