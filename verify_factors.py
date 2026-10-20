from pathlib import Path
import json,sqlite3,subprocess,sys,hashlib
from asharequant.factors import connect,update,asof_records
from asharequant.forecast import FEATURES
r=Path.cwd()
def state():
 with connect(r) as c:
  return {table:c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['factor_snapshots','factor_fetches','factor_current']}
before=state();result=update(r);after=state()
assert before==after,(before,after)
assert result['success'],result['errors']
with connect(r) as c:
 entities=c.execute('select dataset,entity from factor_current').fetchall()
 blocked=all(not asof_records(r,d,e,'2026-10-19T07:00:00+00:00') for d,e in entities)
assert blocked
assert len(FEATURES)==9
run=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],capture_output=True,text=True)
assert run.returncode==0,run.stderr
(r/'reports/factor_tests.txt').write_text(run.stderr)
evidence={'before':before,'after':after,'idempotent_cached_update':before==after,'before_first_observation_blocked':blocked,'production_feature_count':len(FEATURES),'database_integrity':result['database_integrity'],'tests':run.stderr[-150:],'missing_daily_records':result['missing_daily_records']}
(r/'reports/factor_verification.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
print(json.dumps(evidence,ensure_ascii=False,indent=2))
