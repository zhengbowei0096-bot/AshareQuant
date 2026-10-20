from pathlib import Path
import hashlib
import json
import sqlite3
import subprocess
root=Path(__file__).resolve().parents[1]
import sys
py=sys.executable
def state():
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        rows=db.execute('SELECT * FROM daily_bars ORDER BY symbol,date').fetchall()
        return {'rows':len(rows),'content_sha256':hashlib.sha256(json.dumps(rows).encode()).hexdigest(),'imports':db.execute('SELECT count(*) FROM imports').fetchone()[0],'symbols':db.execute('SELECT count(DISTINCT symbol) FROM daily_bars').fetchone()[0],'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],'unknown_limit_rows':db.execute('SELECT count(*) FROM daily_bars WHERE limit_up IS NULL AND limit_down IS NULL').fetchone()[0],'unexplained_missing':db.execute("SELECT count(*) FROM observations WHERE status='unexplained_missing'").fetchone()[0]}
before=state()
r=subprocess.run([py,'-m','asharequant.cli','database'],cwd=root,capture_output=True,text=True)
after=state()
tests=subprocess.run([py,'-m','unittest','discover','-s','tests','-v'],cwd=root,capture_output=True,text=True)
(root/'reports/stage1-tests.txt').write_text(tests.stdout+tests.stderr)
result={'before':before,'after':after,'idempotent':before==after,'database_command_exit':r.returncode,'tests_exit':tests.returncode}
(root/'reports/stage1-verification.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
if before!=after or r.returncode or tests.returncode: raise SystemExit(1)
