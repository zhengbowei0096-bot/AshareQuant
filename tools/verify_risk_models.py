from pathlib import Path
import json,sqlite3,hashlib,subprocess,sys,re
r=Path.cwd();risk=json.loads((r/'reports/risk_models/latest/report.json').read_text());forecast=json.loads((r/'reports/forecasts/latest/forecast.json').read_text());decision=json.loads((r/'reports/decisions/latest/decision.json').read_text());account=json.loads((r/'data/decision_account.json').read_text())
assert not forecast['errors']
assert forecast['plan']['cash']==account['cash'] and decision['cash_after']==account['cash']
assert {x['symbol']:x['shares'] for x in forecast['plan']['current_holdings']}=={s:p['shares'] for s,p in account['positions'].items()}
assert not decision['account_assumed'] and decision['submitted_orders']==0
assert all(x['last_training_date']<x['origin'] for rows in risk['fold_audits'].values() for x in rows)
spec=hashlib.sha256(json.dumps(risk['specification'],sort_keys=True).encode()).hexdigest()
with sqlite3.connect(r/'data/market.sqlite3') as db:
 count,mature=db.execute('SELECT count(*),sum(actual IS NOT NULL) FROM risk_shadow WHERE spec=? AND origin=?',(spec,risk['asof'])).fetchone()
 assert count==len(risk['latest'])
 integrity=db.execute('pragma integrity_check').fetchone()[0]
test=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],capture_output=True,text=True);assert test.returncode==0,test.stderr
out={'asof':risk['asof'],'evaluated_groups':sum(x['status']=='evaluated' for x in risk['results']),'incomplete_groups':sum(x['status']!='evaluated' for x in risk['results']),'historical_passed':sum(x['historical_passed'] for x in risk['results']),'approved':risk['approved_count'],'latest_shadow_predictions':count,'matured_forward_predictions':mature,'training_dates_before_origins':True,'account_cash':account['cash'],'account_positions':{s:p['shares'] for s,p in account['positions'].items()},'reference_equity':forecast['plan']['equity'],'stock_weight':forecast['plan']['invested']/forecast['plan']['equity'],'risk_models_used':decision['risk_models']['used'],'submitted_orders':0,'tests':int(re.search(r'Ran (\d+) tests',test.stderr)[1]),'tests_passed':True,'database_integrity':integrity}
(r/'reports/risk_models/latest/verification.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));(r/'reports/risk_models/latest/tests.txt').write_text(test.stderr);print(json.dumps(out,ensure_ascii=False,indent=2))
