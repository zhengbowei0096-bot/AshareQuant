"""Read-only market/account connections. Unknowns are never replaced with live facts."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import re
import sqlite3
import numpy as np
import pandas as pd
import requests
from .core import now, write_json, read_watchlist, read_snapshot, load_config, cutoff
from .data import calendar


def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def save_record(root,kind,payload):
    payload={**payload,'recorded_at':now().isoformat()}; key=digest(payload)
    write_json(root/'data/connections'/kind/(key+'.json'),payload)
    write_json(root/'data/connections'/kind/'latest.json',payload)
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS connection_records (hash TEXT PRIMARY KEY, kind TEXT, recorded_at TEXT, payload TEXT)')
        db.execute('INSERT OR IGNORE INTO connection_records VALUES (?,?,?,?)',(key,kind,payload['recorded_at'],json.dumps(payload,ensure_ascii=False,allow_nan=False)))
    return payload


def number(v):
    try:
        x=float(v)
        return x if np.isfinite(x) else None
    except (ValueError,TypeError): return None


def fetch_json(url,params):
    r=requests.get(url,params=params,timeout=(5,12)); r.raise_for_status(); return r.json()


def tencent_quotes(symbols):
    codes=[('sh' if s.startswith('6') else 'sz')+s for s in symbols]
    r=requests.get('https://qt.gtimg.cn/q='+','.join(codes),timeout=(5,12));r.raise_for_status()
    records={}
    for code,content in re.findall(r'v_([a-z]{2}[0-9]{6})="([^"]*)";',r.text):
        f=content.split('~')
        if len(f)<49 or f[2]!=code[2:]: raise ValueError('腾讯行情字段变更/代码不一致')
        asof=pd.Timestamp(datetime.strptime(f[30],'%Y%m%d%H%M%S'),tz='Asia/Shanghai').isoformat()
        records[f[2]]=dict(symbol=f[2],name=f[1],price=number(f[3]),previous_close=number(f[4]),asof=asof,
            bid=number(f[9]),ask=number(f[19]),bid_size_lots=number(f[10]),ask_size_lots=number(f[20]),
            limit_up=number(f[47]),limit_down=number(f[48]),source='Tencent qt.gtimg.cn',raw_fields=f)
    if set(records)!=set(symbols): raise ValueError('腾讯行情返回股票不完整')
    return records



def sina_quotes(symbols):
    codes=[('sh' if s.startswith('6') else 'sz')+s for s in symbols]
    r=requests.get('https://hq.sinajs.cn/list='+','.join(codes),headers={'Referer':'https://finance.sina.com.cn/'},timeout=(5,12))
    r.raise_for_status();r.encoding='gbk';result={}
    for code,content in re.findall(r'var hq_str_([a-z]{2}[0-9]{6})="([^"]*)";',r.text):
        f=content.split(',')
        if len(f)<32: raise ValueError('新浪行情字段不完整')
        ts=pd.Timestamp(f[30]+'T'+f[31],tz='Asia/Shanghai').isoformat()
        result[code[2:]]=dict(symbol=code[2:],name=f[0],price=number(f[3]),previous_close=number(f[2]),asof=ts,source='Sina hq.sinajs.cn',raw=f)
    if set(result)!=set(symbols): raise ValueError('新浪行情缺少股票')
    return result


def eastmoney_quote(symbol):
    raw=fetch_json('https://push2.eastmoney.com/api/qt/stock/get',dict(secid=('1.' if symbol.startswith('6') else '0.')+symbol,
        fltt=2,fields='f43,f57,f58,f60,f51,f52,f86,f19,f20,f39,f40'))
    d=raw.get('data')
    if not d or d.get('f57')!=symbol: raise ValueError('东方财富缺少行情或代码不符')
    ts=pd.Timestamp(d['f86'],unit='s',tz='UTC').tz_convert('Asia/Shanghai').isoformat()
    return dict(symbol=symbol,name=d['f58'],price=number(d.get('f43')),previous_close=number(d.get('f60')),
                limit_up=number(d.get('f51')),limit_down=number(d.get('f52')),asof=ts,source='Eastmoney push2',raw=d)


def suspensions(day):
    params=dict(reportName='RPT_CUSTOM_SUSPEND_DATA_INTERFACE',columns='ALL',pageSize=500,pageNumber=1,
                filter=f'(MARKET="全部")(DATETIME=\'{day}\')')
    raw=fetch_json('https://datacenter-web.eastmoney.com/api/data/v1/get',params)
    result=raw.get('result')
    if not isinstance(result,dict) or not isinstance(result.get('pages'),int): raise ValueError('停复牌响应不完整，不能当作无停牌')
    items=list(result.get('data') or [])
    for page in range(2,result['pages']+1):
        p=fetch_json('https://datacenter-web.eastmoney.com/api/data/v1/get',{**params,'pageNumber':page})
        if not p.get('result') or not isinstance(p['result'].get('data'),list): raise ValueError('停复牌分页不完整')
        items+=p['result']['data']
    return dict(date=day,source='Eastmoney RPT_CUSTOM_SUSPEND_DATA_INTERFACE',complete=True,items=items,fetched_at=now().isoformat())


def merge_quote(t,e,susp,expected_day,at,seasoned):
    """Two-vendor consistency + current time + dated suspension list, conditional only."""
    q={k:v for k,v in t.items() if k!='raw_fields'}
    q.update(kind='vendor_snapshot',status='unknown',status_source=None,status_asof=None,session_verified=False,
             ordinary_session=None,risk_warning=any(x in t['name'].upper() for x in ('ST','退')),limit_regime='unknown',checks=[])
    age=(pd.Timestamp(at)-pd.Timestamp(t['asof'])).total_seconds()
    if age<0: q['checks'].append('FUTURE_QUOTE')
    if e is None: q['checks'].append('SECOND_SOURCE_UNAVAILABLE')
    else:
        if e['name']!=t['name'] or pd.Timestamp(e['asof']).date()!=pd.Timestamp(t['asof']).date(): q['checks'].append('SOURCE_IDENTITY_OR_DATE_CONFLICT')
        fields=('price','previous_close','limit_up','limit_down') if 'limit_up' in e else ('price','previous_close')
        q['limit_source']=t['source'] if 'limit_up' not in e else t['source']+' + '+e['source']
        for field in fields:
            if t.get(field) is None or e.get(field) is None or abs(t[field]-e[field])>.011: q['checks'].append('SOURCE_CONFLICT:'+field)
        q['crosscheck']=e
    same_day=str(pd.Timestamp(t['asof']).date())==expected_day
    q['ordinary_session']=bool(seasoned and not q['risk_warning']) if seasoned else None
    if q['ordinary_session'] and t['previous_close'] and t['limit_up'] and t['limit_down']:
        if abs(t['limit_up']-round(t['previous_close']*1.1,2))<=.011 and abs(t['limit_down']-round(t['previous_close']*.9,2))<=.011:
            q['limit_regime']='ordinary_10pct'
    status_complete=susp is not None and susp.get('complete') and susp['date']==expected_day
    listed=status_complete and any(x.get('SECURITY_CODE')==t['symbol'] for x in susp['items'])
    if listed: q.update(status='suspended_or_event',status_source=susp['source'],status_asof=susp['fetched_at'])
    fresh=0<=age<=60 and same_day and e and 0<=(pd.Timestamp(at)-pd.Timestamp(e['asof'])).total_seconds()<=60
    if fresh and not q['checks'] and status_complete and not listed and q.get('bid',0) and q.get('ask',0):
        q.update(kind='live',status='trading',status_source='Dual vendor fresh quotes + dated suspension list (conditional evidence)',
                 status_asof=t['asof'],session_verified=True)
    elif not listed: q['checks'].append('NOT_CURRENT_VERIFIED_TRADING')
    return q


def refresh_market(root):
    root=Path(root); base=load_config(root); at=now(); cal,_=calendar(root,base,cutoff(),offline=True)
    market_cutoff=pd.Timestamp(at.date()) if (at.hour,at.minute)>=(9,15) else pd.Timestamp(at.date())-pd.Timedelta(days=1)
    days=cal[cal<=market_cutoff]; day=str(days.max().date())
    symbols=read_watchlist(root).symbol.tolist(); errors=[]; qs={}; tx={}; em={}; susp=None
    try: tx=tencent_quotes(symbols)
    except Exception as exc: errors.append('Tencent: '+str(exc)[:250])
    try: susp=suspensions(day); save_record(root,'suspensions',susp)
    except Exception as exc: errors.append('suspensions: '+str(exc)[:250])
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks={pool.submit(eastmoney_quote,s):s for s in symbols}
        for task in as_completed(tasks):
            s=tasks[task]
            try: em[s]=task.result()
            except Exception as exc: errors.append(s+' Eastmoney: '+str(exc)[:200])
    fallback_symbols=[s for s in symbols if s not in em]
    if fallback_symbols:
        try: em.update(sina_quotes(fallback_symbols))
        except Exception as exc: errors.append('Sina fallback: '+str(exc)[:200])
    for s,t in tx.items():
        snap=read_snapshot(root,s)
        seasoned=snap is not None and len(snap['raw'][snap['raw'].date<pd.Timestamp(day)])>=6
        qs[s]=merge_quote(t,em.get(s),susp,day,at.isoformat(),seasoned)
    payload=dict(source='public_vendor_connections',market_date=day,requested_at=at.isoformat(),quotes=qs,
                 raw_tencent=tx,errors=errors,secondary_source_symbols=len(em),fallback_symbols=[s for s in fallback_symbols if s in em],connected_symbols=len(qs),verified_live_symbols=sum(q['status']=='trading' for q in qs.values()))
    return save_record(root,'market',payload)


def market_environment(root,asof):
    from .indices import load_index
    try:
        frame=load_index(root,'sh000300',pd.Timestamp(asof)-pd.Timedelta(days=400),pd.Timestamp(asof))
        if len(frame)<60 or str(frame.date.max().date())!=asof: raise ValueError('沪深300数据过期/不足60日')
        c=frame.close; ma20=float(c.tail(20).mean()); ma60=float(c.tail(60).mean()); vol=float(c.pct_change().tail(20).std()*np.sqrt(252))
        state='risk_off' if c.iloc[-1]<ma60 or vol>.35 else 'risk_on' if c.iloc[-1]>ma20>ma60 else 'neutral'
        return dict(state=state,source='market_index_bars/sh000300 Tencent',asof=asof,close=float(c.iloc[-1]),ma20=ma20,ma60=ma60,
                    volatility20_annual=vol,method='fixed_trend_vol_v1',note='固定研究规则，不是已验证收益信号')
    except Exception as exc: return dict(state='unknown',error=str(exc))


def validate_account(a):
    if a.get('broker')!='Example Broker': raise ValueError('当前账户适配要求Example Broker标识')
    for k in ('cash','available_cash'):
        if number(a.get(k)) is None or a[k]<0: raise ValueError('账户现金缺失或无效')
    if a['available_cash']>a['cash']: raise ValueError('可用资金超过现金')
    for code,p in a['positions'].items():
        if not re.fullmatch(r'\d{6}',code): raise ValueError('代码格式无效')
        for k in ('shares','sellable'):
            if not isinstance(p.get(k),int) or isinstance(p[k],bool) or p[k]<0: raise ValueError('数量须非负整数')
        if p['sellable']>p['shares']: raise ValueError('可卖超过持仓')
    if a.get('asof'):
        ts=pd.Timestamp(a['asof'])
        if ts.tzinfo is None or ts>pd.Timestamp(now()): raise ValueError('账户时间须含时区且不能是未来')
    return a


def import_account(root,path):
    a=validate_account(json.loads(Path(path).read_text()))
    a={k:a[k] for k in ('broker','cash','available_cash','positions','source','asof')}
    a.update(imported_at=now().isoformat(),input_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),mode='user_snapshot',live_connection=False)
    write_json(Path(root)/'data/decision_account.json',a)
    return save_record(Path(root),'account',a)


def import_fees(root,path):
    c=json.loads(Path(path).read_text())
    required=['commission_bps','minimum_commission','transfer_bps','exchange_bps','regulatory_bps','sell_tax_bps','slippage_bps']
    for k in required:
        if number(c.get(k)) is None or c[k]<0: raise ValueError('缺少费用项: '+k)
    if c['commission_bps']>30: raise ValueError('佣金超过A股范围，请核验单位bps')
    for k in ('commission_includes_regulatory','commission_includes_transfer'):
        if not isinstance(c.get(k),bool): raise ValueError('必须明确是否包含规费/过户费')
    if c.get('broker')!='Example Broker' or not c.get('evidence') or not c.get('effective_date'): raise ValueError('须提供券商、费用证据和生效日期')
    if pd.Timestamp(c['effective_date']).date()>now().date(): raise ValueError('费用生效日期尚未到达')
    overrides=c.get('fee_market_overrides',{})
    if overrides:
        if set(overrides)!={'SSE_MAIN_A','SZSE_MAIN_A'}: raise ValueError('须完整配置沪深主板费用')
        for value in overrides.values():
            if set(value)!={'commission_includes_transfer'} or not isinstance(value['commission_includes_transfer'],bool):
                raise ValueError('市场费用覆盖项无效')
    c.update(fees_verified=True,fee_source=c['evidence'],input_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
    return save_record(Path(root),'fees',c)


def readiness(root):
    root=Path(root); out={}
    for kind in ('market','account','fees','fee_observations','calibration','replay'):
        p=root/'data/connections'/kind/'latest.json'
        if not p.exists(): out[kind]=dict(status='MISSING'); continue
        x=json.loads(p.read_text())
        out[kind]=dict(status=x.get('status','SNAPSHOT_AVAILABLE'),recorded_at=x.get('recorded_at'))
        if kind=='account': out[kind].update(live_connection=x.get('live_connection',False),asof=x.get('asof'),positions=len(x.get('positions',{})))
        if kind=='market': out[kind].update(connected_symbols=x.get('connected_symbols'),verified_live_symbols=x.get('verified_live_symbols'),errors=x.get('errors'))
        if kind=='fee_observations': out[kind].update(commission_rate_unknown=x.get('commission_rate_unknown'),commission_upper_bound_bps=x.get('commission_upper_bound_bps'))
        if kind=='calibration': out[kind].update(validated=x.get('independent_validated'),holdout_blocks=x.get('holdout_blocks'))
    write_json(root/'reports/decisions/connections.json',out)
    return out


def import_fee_observation(root,path):
    d=json.loads(Path(path).read_text())
    required=['notional','net_cash','commission','stamp','transfer']
    if d.get('side')!='SELL' or any(number(d.get(k)) is None or d[k]<0 for k in required): raise ValueError('费用证据字段无效')
    if abs(d['notional']-sum(d[k] for k in ('commission','stamp','transfer'))-d['net_cash'])>.011: raise ValueError('交割单费用不平')
    if abs(d['stamp']-round(d['notional']*.0005,2))>.011 or abs(d['transfer']-round(d['notional']*.00001,2))>.011:
        raise ValueError('交割单税费与现行A股普通费用不同，需单独核验')
    allowed={k:d[k] for k in ('side','symbol','notional','net_cash','commission','stamp','transfer','source','trade_date')}
    allowed.update(status='OBSERVED_TRADE_ONLY',commission_rate_unknown=True,fees_verified=False,
                   commission_upper_bound_bps=d['commission']/d['notional']*10000,
                   note='仅证明这一笔实际费用；最低收费导致无法确定合同佣金率；不回放到当前持仓')
    return save_record(Path(root),'fee_observations',allowed)
