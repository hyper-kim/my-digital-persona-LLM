import sqlite3

conn = sqlite3.connect('processed_files.db')
cur = conn.cursor()

# ====== failed_files 상세 분석 ======
cols = [r[1] for r in cur.execute('PRAGMA table_info(failed_files)').fetchall()]
print('failed_files 컬럼:', cols)

# 전체 실패 건수
total_fail = cur.execute('SELECT COUNT(*) FROM failed_files').fetchone()[0]
print(f'전체 실패: {total_fail}건')

# 한성과고 실패 파일
hansung_fail = cur.execute(
    "SELECT file_path, error, attempt_count FROM failed_files WHERE file_path LIKE ? OR file_path LIKE ?",
    ('%한성%', '%과학고%')
).fetchall()
print(f'\n=== 한성과고 실패 파일: {len(hansung_fail)}건 ===')
for r in hansung_fail:
    print(f'  [{r[2]}회시도] ...{r[0][-70:]}')
    if r[1]: print(f'    err: {str(r[1])[:120]}')

# 한성과고 성공 카운트
hansung_ok = cur.execute(
    "SELECT COUNT(*) FROM processed WHERE file_path LIKE ? OR file_path LIKE ?",
    ('%한성%', '%과학고%')
).fetchone()[0]
print(f'\n=== 한성과고 처리 완료: {hansung_ok}건 ===')

# PDF만 필터
hansung_pdf_ok = cur.execute(
    "SELECT COUNT(*) FROM processed WHERE (file_path LIKE ? OR file_path LIKE ?) AND file_path LIKE ?",
    ('%한성%', '%과학고%', '%.pdf')
).fetchone()[0]
hansung_pdf_fail = cur.execute(
    "SELECT file_path, error FROM failed_files WHERE (file_path LIKE ? OR file_path LIKE ?) AND file_path LIKE ?",
    ('%한성%', '%과학고%', '%.pdf')
).fetchall()
print(f'\n=== PDF 처리완료: {hansung_pdf_ok}건, 실패: {len(hansung_pdf_fail)}건 ===')
for r in hansung_pdf_fail:
    print(f'  {r[0][-80:]}')
    if r[1]: print(f'    err: {str(r[1])[:120]}')

conn.close()
exit()

# 아래는 사용 안 함


# 테이블명 확인
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print('=== 테이블 목록 ===')
for t in tables:
    cnt = cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'  {t}: {cnt}건')

# 실제 테이블명
tbl = tables[0] if tables else None
if not tbl:
    print('DB 비어있음'); conn.close(); exit()

# 컬럼 확인
cols = [r[1] for r in cur.execute(f'PRAGMA table_info({tbl})').fetchall()]
print(f'\n=== {tbl} 컬럼: {cols} ===')

# 경로 컬럼 추정
path_col = next((c for c in cols if 'path' in c.lower() or 'file' in c.lower()), cols[0])
status_col = next((c for c in cols if 'status' in c.lower()), None)
err_col = next((c for c in cols if 'error' in c.lower() or 'msg' in c.lower()), None)

print(f'path_col={path_col}, status_col={status_col}, err_col={err_col}')

# 한성과고 파일 상태
if status_col:
    rows = cur.execute(f"""
        SELECT {status_col}, COUNT(*) FROM {tbl}
        WHERE {path_col} LIKE '%한성%' OR {path_col} LIKE '%Hansung%' OR {path_col} LIKE '%과학고%'
        GROUP BY {status_col}
    """).fetchall()
    print('\n=== 한성과고 파일 상태 ===')
    if not rows: print('  (검색 결과 없음)')
    total = 0
    for r in rows:
        print(f'  {r[0]}: {r[1]}건')
        total += r[1]
    print(f'  합계: {total}건')

    # 실패 파일
    err_filter = "status IN ('failed','error','FAILED','ERROR')"
    fail_rows = cur.execute(f"""
        SELECT {path_col}, {status_col}{', ' + err_col if err_col else ''}
        FROM {tbl}
        WHERE ({path_col} LIKE '%한성%' OR {path_col} LIKE '%Hansung%' OR {path_col} LIKE '%과학고%')
          AND {err_filter}
        LIMIT 30
    """).fetchall()
    print(f'\n=== 실패 파일 ({len(fail_rows)}건) ===')
    for r in fail_rows:
        print(f'  [{r[1]}] ...{r[0][-70:]}')
        if err_col and r[2]: print(f'      err: {str(r[2])[:100]}')
else:
    rows = cur.execute(f"""
        SELECT {path_col} FROM {tbl}
        WHERE {path_col} LIKE '%한성%' OR {path_col} LIKE '%Hansung%' OR {path_col} LIKE '%과학고%'
        LIMIT 20
    """).fetchall()
    print(f'\n한성 관련 {len(rows)}건:')
    for r in rows: print(f'  {r[0][-80:]}')

conn.close()
