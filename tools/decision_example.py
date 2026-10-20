"""Synthetic quantities only. Does not read market data or write account/DB records."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
from asharequant.decision import DEFAULTS, candidate_plan

at='2026-10-19T10:00:00+08:00'
quote=dict(price=10.,source='SYNTHETIC_TEST_ONLY',asof=at,kind='live',status='trading',status_source='SYNTHETIC_TEST_ONLY',status_asof=at,
           risk_warning=False,ordinary_session=True,limit_regime='ordinary_10pct',limit_down=9.,limit_up=11.,session_verified=True)
rows=[dict(symbol='600000',name='合成测试股票',sector='合成行业',rule='SSE_MAIN_A',quote=quote,synthetic=True)]
a=dict(cash=150000.,available_cash=150000.,positions={},source='SYNTHETIC_TEST_ONLY',asof=at)
c={**DEFAULTS,'fees_verified':True,'fee_source':'SYNTHETIC_TEST_ONLY','market_environment':'risk_on'}
r=candidate_plan(rows,{'600000':.10},a,c,at)
r['notice']='全部行情、状态、账户和目标是合成测试，不是实时数据或真实交易信号'
print(json.dumps(r,ensure_ascii=False,indent=2))
