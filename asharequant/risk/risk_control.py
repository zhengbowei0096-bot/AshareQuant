"""Independent sell-only PAPER risk planning; never promotes models or sends orders."""
from pathlib import Path
import json,math,sqlite3,csv,html,hashlib
import pandas as pd
from ..core import now,cutoff,write_json,atomic_text,load_config,read_watchlist
from ..decision import DEFAULTS,load_fee_config,sell_quantity,costs,rule_for,validate_config
from ..paper_account import get,evidence

def build(positions,cash,bars,states,sectors,c):
    validate_config(c)
    if not math.isfinite(cash) or cash<0:raise ValueError('INVALID_CASH')
    if not c.get('fees_verified'):raise ValueError('FEES_UNVERIFIED')
    for s,p in positions.items():
        if not rule_for(s) or s not in sectors or s not in bars or s not in states:raise ValueError('POSITION_DATA_MISSING:'+s)
        if type(p['shares']) is not int or p['shares']<0 or type(p['sellable']) is not int or not 0<=p['sellable']<=p['shares']:raise ValueError('INVALID_POSITION:'+s)
        if not math.isfinite(bars[s]['close']) or bars[s]['close']<=0:raise ValueError('PRICE_INVALID:'+s)
    amounts={s:p['shares']*bars[s]['close'] for s,p in positions.items()};equity=cash+sum(amounts.values())
    if equity<=0:raise ValueError('INVALID_EQUITY')
    # Cost reserve keeps the policy target conservative; these are caps, not optima.
    base=equity*(1-c['max_cost_fraction'])
    targets={s:min(v,base*c['max_stock']) for s,v in amounts.items()}
    for sector in set(sectors[s] for s in positions):
        members=[s for s in positions if sectors[s]==sector];total=sum(targets[s] for s in members)
        scale=min(1,base*c['max_sector']/total) if total else 1
        for s in members:targets[s]*=scale
    total=sum(targets.values());scale=min(1,base*c['max_invested']/total) if total else 1
    targets={s:v*scale for s,v in targets.items()}
    desired={s:max(0,p['shares']-int(targets[s]/bars[s]['close'])//100*100) for s,p in positions.items()}
    total_sell=sum(desired[s]*bars[s]['close'] for s in positions);phase=min(1,equity*c['max_turnover']/total_sell) if total_sell else 1
    rows=[];cash_after=cash;turnover=fees=0.
    for s,p in sorted(positions.items()):
        bar=bars[s];state=states[s];price=bar['close'];flags=[]
        if state.get('status')!='trading':flags.append('SUSPENDED')
        if state.get('corporate_action_status')!='none':flags.append('CORPORATE_ACTION_REVIEW_REQUIRED')
        if state.get('ordinary_session') is not True:flags.append('SPECIAL_SESSION_UNVERIFIED')
        if not state.get('source'):flags.append('STATUS_SOURCE_MISSING')
        lo=state.get('limit_down');hi=state.get('limit_up')
        if not isinstance(lo,(int,float)) or not isinstance(hi,(int,float)) or not lo<price<hi:flags.append('LIMIT_LOCK_OR_UNKNOWN')
        volume=bar.get('volume')
        if not isinstance(volume,(int,float)) or not math.isfinite(volume) or volume<=0:flags.append('VOLUME_UNVERIFIED')
        qty=sell_quantity(int(desired[s]*phase)//100*100,p['sellable'],p['shares'])
        if phase==1:qty=sell_quantity(desired[s],p['sellable'],p['shares'])
        if volume and not flags:qty=min(qty,int(volume*.01)//100*100)
        if desired[s]>p['sellable']:flags.append('PARTIAL_T_PLUS_1_SELLABLE_CAP')
        if any(x!='PARTIAL_T_PLUS_1_SELLABLE_CAP' for x in flags):qty=0
        fill=math.floor(price*(1-c['slippage_bps']/10000)*100)/100
        if qty and not lo<fill<hi:qty=0;flags.append('SLIPPAGE_CROSSES_LIMIT')
        while qty:
            notional=qty*fill;fee=costs(notional,'SELL',{**c,'slippage_bps':0},s)['total']
            if turnover+qty*price<=equity*c['max_turnover']+1e-8 and fees+fee<=equity*c['max_cost_fraction']+1e-8:break
            qty=sell_quantity(max(0,qty-100),p['sellable'],p['shares'])
        if qty and qty*fill<c['minimum_trade_value']:qty=0;flags.append('BELOW_MINIMUM_TRADE_VALUE')
        fee=costs(qty*fill,'SELL',{**c,'slippage_bps':0},s)['total'] if qty else 0.
        cash_after+=qty*fill-fee;turnover+=qty*price;fees+=fee
        after=p['shares']-qty
        rows.append(dict(symbol=s,sector=sectors[s],current_shares=p['shares'],sellable=p['sellable'],current_weight=amounts[s]/equity,current_amount=amounts[s],price=price,price_source=bar.get('source'),price_fetched_at=bar.get('fetched_at'),price_type='已完成交易日收盘价',risk_cap_amount=targets[s],risk_cap_weight=targets[s]/equity,desired_reduction_shares=desired[s],candidate_sell_shares=qty,action='REDUCE' if qty else 'NO_TRADE' if desired[s] else 'HOLD',scenario_sell_price=fill,estimated_fee=fee,scenario_remaining_shares=after,remaining_excess_shares=max(0,desired[s]-qty),scenario_cash_after=cash_after,blockers=flags,executable=False))
    end_equity=cash_after+sum(x['scenario_remaining_shares']*x['price'] for x in rows)
    for x in rows:x['scenario_weight']=x['scenario_remaining_shares']*x['price']/end_equity
    return dict(equity=equity,cash_before=cash,scenario_cash_after=cash_after,scenario_equity=end_equity,estimated_fees=fees,reference_turnover=turnover/equity,rows=rows,remaining_risk=any(x['remaining_excess_shares'] for x in rows),submitted_orders=0,ledger_mutated=False)

def run(root):
    root=Path(root);stamp=now();day=str(cutoff(stamp).date());result=dict(generated_at=stamp.isoformat(),asof=day,mode='PAPER_RISK_PLAN_ONLY',status='BLOCKED_DATA',submitted_orders=0,rows=[],blockers=[],model_gate_required=False,ledger_mutated=False)
    try:
        c={**DEFAULTS,**json.loads((root/'decision_config.json').read_text())};load_fee_config(root,c)
        from ..research_paper import enabled
        research=enabled(root) and (root/'data/research_paper.sqlite3').exists()
        account_path=root/'data'/('research_paper.sqlite3' if research else 'paper_account.sqlite3')
        with sqlite3.connect('file:'+str(account_path)+'?mode=ro',uri=True) as db:
            nav=get(db,'nav');meta=get(db,'meta')['account']
        if day not in nav and research and meta['anchor_session']==day:
            nav[day]=dict(positions=meta['initial_positions'],cash=meta['initial_cash'],equity=meta['initial_equity'])
        if day not in nav:raise ValueError('PAPER_VALUATION_NOT_CURRENT')
        account=nav[day];positions=account['positions'];o=evidence(root,day,set(positions))
        watch=read_watchlist(root);sectors={r.symbol:r.sector for r in watch.itertuples()};names={r.symbol:r.name for r in watch.itertuples()}
        from ..data import calendar
        sessions,_=calendar(root,load_config(root),pd.Timestamp(day),offline=True);future=sessions[sessions>pd.Timestamp(day)]
        if not len(future):raise ValueError('NEXT_SESSION_UNKNOWN')
        result.update(build(positions,account['cash'],o['bars'],o['states'],sectors,c));result.update(status='CONDITIONAL_RISK_PLAN',plan_session=str(future[0].date()),account_source='研究模拟账本已结算持仓' if research else '原模拟账本已结算持仓',account_valuation_date=day,source_proof=o['proof'],fee_config=c,account_sha256=hashlib.sha256(json.dumps(account,sort_keys=True).encode()).hexdigest())
        for row in result['rows']:row['name']=names[row['symbol']];row['price_session']=day
    except (ValueError,KeyError,OSError,sqlite3.Error) as exc:result['blockers']=[str(exc)]
    result['conditions']=['这是按既定上限生成的风险约束方案，不是最优收益组合，不代表已成交。','仅考虑减仓；不因未验证上涨概率产生买入或扩大风险。','下一交易日须重新核实价格、T+1可卖数量、停牌、涨跌停、成交量及公司行为；重算股数、费用和现金。','参考成交价为收盘价扣不利滑点的情景假设，不能证明次日可成交。','当日换手率、费用或成交量限制可能使风险超限分多次处理；风险目标与本次候选股数分别列出。','本方案独立保存，不覆盖收益策略已冻结计划，不修改模拟成交及净值账本。']
    folder=root/'reports/risk_control'/stamp.strftime('%Y%m%d-%H%M%S-%f');folder.mkdir(parents=True)
    write_json(folder/'report.json',result)
    with (folder/'risk_plan.csv').open('w',encoding='utf-8-sig',newline='') as f:
        cols=list(result['rows'][0]) if result['rows'] else ['symbol','candidate_sell_shares','blockers'];w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(result['rows'])
    e=html.escape;table=''
    for x in result['rows']:
        vals=[x['symbol'],x['name'],f"{x['current_weight']:.2%}",x['current_shares'],f"{x['price']:.2f}",x['price_session'],x['price_source'],f"{x['risk_cap_weight']:.2%}",x['candidate_sell_shares'],x['scenario_remaining_shares'],f"{x['scenario_weight']:.2%}",f"{x['estimated_fee']:.2f}",f"{x['scenario_cash_after']:.2f}",x['remaining_excess_shares'],'、'.join(x['blockers']) or '次日复核后才可模拟成交']
        table+='<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in vals)+'</tr>'
    page='<meta charset="utf-8"><title>独立风控模拟方案</title><style>body{font:14px/1.5 system-ui;margin:20px}table{border-collapse:collapse}td,th{padding:6px 8px;vertical-align:top;border-bottom:1px solid #ddd;white-space:nowrap}.scroll{overflow:auto}</style><h1>独立风控模拟调仓方案</h1><p>状态：'+e(result['status'])+'；参考数据日：'+day+'；计划交易日：'+e(str(result.get('plan_session','待核实')))+'</p><p>'+e('；'.join(result['blockers']))+'</p><ul>'+''.join('<li>'+e(x)+'</li>' for x in result['conditions'])+'</ul><div class="scroll"><table><tr>'+''.join('<th>'+x+'</th>' for x in ['代码','名称','当前仓位','现有股数','参考收盘价','价格日期','来源','风控上限目标（非最优）','本次候选卖出股数','情景剩余股数','情景仓位','预计费用','情景现金','仍待降低股数','限制'])+'</tr>'+table+'</table></div><p><a href="risk_plan.csv">方案CSV</a> · <a href="report.json">完整来源与限制</a></p>'
    atomic_text(folder/'index.html',page)
    latest=root/'reports/risk_control/latest';latest.mkdir(exist_ok=True)
    for name in ('report.json','risk_plan.csv','index.html'):atomic_text(latest/name,(folder/name).read_text())
    from ..validation_readiness import run as readiness
    readiness(root)
    return result
