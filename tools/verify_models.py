from pathlib import Path
import json,sqlite3,numpy as np,pandas as pd,re,subprocess,sys
from asharequant.forecast import forecast_one,DEFAULTS
from asharequant.core import read_snapshot,cutoff
r=Path.cwd();p=json.load(open(r/'reports/model_comparison/latest/comparison.json'));cfg={**DEFAULTS,**json.load(open(r/'forecast_config.json'))};cal=pd.DatetimeIndex(pd.to_datetime(pd.read_csv(r/'data/calendar.csv').trade_date))
expected=p['specification']['family_size']
assert not p['errors'] and len(p['results'])==expected and len(p['shadow_forecasts'])==expected
assert all(f['training_last_label_end']<f['test_start'] for x in p['results'] for f in x['folds'])
snap=read_snapshot(r,'600031');matched=0
for x in p['shadow_forecasts']:
 if x['symbol']=='600031':
  y,_=forecast_one(snap,cal,cutoff(),x['horizon'],cfg,model_name=x['model'])
  assert np.isclose(y['predicted_return'],x['predicted_return'],rtol=1e-9,atol=1e-10),(x,y['predicted_return'])
  matched+=1
with sqlite3.connect(r/'data/market.sqlite3') as db:
 count,mature=db.execute('SELECT COUNT(*),SUM(actual IS NOT NULL) FROM model_shadow WHERE spec=? AND origin_date=?',(p['specification']['id'],p['asof'])).fetchone()
 assert count==expected and 0<=mature<=count
 integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
test_run=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],capture_output=True,text=True)
log=test_run.stderr;assert test_run.returncode==0,log
result={'asof':p['asof'],'comparisons':len(p['results']),'paired_predictions':expected*cfg['validation_days'],'current_spec_shadow_predictions':count,'matured_prospective_predictions':mature,'promoted':len(p['active']),'forecast_adapter_matches':matched,'tests':int(re.search(r'Ran (\d+) tests',log)[1]),'tests_passed':True,'database_integrity':integrity,'no_label_overlap':True,'dependencies':p['specification']['dependencies']}
(r/'reports/model_comparison/latest/verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));(r/'reports/model_comparison/latest/tests.txt').write_text(log)
print(json.dumps(result,ensure_ascii=False,indent=2))
