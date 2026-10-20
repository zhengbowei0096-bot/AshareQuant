"""Supplemental BaoStock history; partial fields never certify execution feasibility.
Primary API reference: https://www.baostock.com/ (checked 2026-10-20).
"""
from pathlib import Path
import json,sys,socket,subprocess,tempfile,hashlib
import pandas as pd
from .core import now,cutoff,read_watchlist,read_snapshot,write_json


def normalize(symbol,data):
    rows=data.get('daily',[]);seen=set()
    for r in rows:
        if r['code']!=('sh.' if symbol.startswith('6') else 'sz.')+symbol:raise ValueError('SOURCE_CODE_MISMATCH')
        if r['date'] in seen:raise ValueError('DUPLICATE_SOURCE_DATE')
        seen.add(r['date'])
        if r['adjustflag']!='3' or r['tradestatus'] not in ('0','1') or r['isST'] not in ('0','1'):raise ValueError('SOURCE_STATE_UNKNOWN')
        values=[float(r[k]) for k in ('open','high','low','close')]
        import math
        if not all(math.isfinite(v) and v>0 for v in values):raise ValueError('INVALID_SOURCE_PRICE')
        if not values[2]<=min(values[0],values[3])<=max(values[0],values[3])<=values[1]:raise ValueError('INVALID_SOURCE_OHLC')
    return rows


def crosscheck(root,symbol,rows):
    snap=read_snapshot(Path(root),symbol)
    if not snap:return dict(status='NO_PRIMARY_SOURCE',matched=0)
    primary=snap['raw'].set_index('date');matched=0;bad=[]
    for r in rows:
        day=pd.Timestamp(r['date'])
        if day not in primary.index:continue
        matched+=1
        if any(abs(float(primary.loc[day,k])-float(r[k]))>.011 for k in ('open','high','low','close')):bad.append(r['date'])
    return dict(status='MISMATCH' if bad else 'MATCHED' if matched else 'NO_OVERLAP',matched=matched,mismatch_dates=bad[:30],primary_version=snap['meta']['version'])


def fetch(root,symbol,start,end):
    with tempfile.TemporaryDirectory(prefix='aq-public-') as directory:
        job=Path(directory)/'job.json';output=Path(directory)/'result.json'
        job.write_text(json.dumps(dict(symbol=symbol,start=start,end=end,output=str(output))))
        result=subprocess.run([sys.executable,'-m','asharequant.public_history',str(job)],cwd=root,capture_output=True,text=True,timeout=50)
        if result.returncode or not output.exists():raise RuntimeError('BAOSTOCK_WORKER_FAILED')
        return json.loads(output.read_text())


def refresh(root,force=False):
    root=Path(root);end=cutoff();start=end-pd.DateOffset(years=5);path=root/'data/history/baostock/latest.json'
    if path.exists() and not force:
        old=json.loads(path.read_text())
        if old['asof']==str(end.date()) and (pd.Timestamp(now())-pd.Timestamp(old['fetched_at'])).total_seconds()<21600:
            from .history_sources import verified
            try:
                for reference in old['references']:verified(root,reference)
                return {**old,'cached':True}
            except (ValueError,OSError):pass
    from .history_sources import _save
    references=[];checks={};errors=[];daily=0
    for item in read_watchlist(root).itertuples():
        try:
            data=fetch(root,item.symbol,str(start.date()),str(end.date()))
            if data.get('errors'):errors.extend(dict(symbol=item.symbol,**e) for e in data['errors'])
            rows=normalize(item.symbol,data)
            if not rows:raise ValueError('EMPTY_HISTORY')
            for dataset in ('daily','hfq','basic','factors','dividends'):
                if dataset in data:references.append(_save(root,'baostock_'+dataset,dict(symbol=item.symbol,start=str(start.date()),end=str(end.date()),fields=data.get('fields',{}).get(dataset)),data[dataset],'BaoStock'))
            daily+=len(rows);checks[item.symbol]=crosscheck(root,item.symbol,rows)
            checks[item.symbol]['latest_source_date']=max(r['date'] for r in rows)
            checks[item.symbol]['stale_for_requested_asof']=checks[item.symbol]['latest_source_date']!=str(end.date())
            if checks[item.symbol]['stale_for_requested_asof']:errors.append(dict(symbol=item.symbol,error='SOURCE_HISTORY_STALE'))
        except (ValueError,KeyError,OSError,RuntimeError,subprocess.TimeoutExpired) as exc:errors.append(dict(symbol=item.symbol,error=type(exc).__name__+':'+str(exc)[:160]))
    result=dict(provider='BaoStock',asof=str(end.date()),start=str(start.date()),fetched_at=now().isoformat(),symbols=len(checks),daily_rows=daily,references=references,crosschecks=checks,errors=errors,
        status='SUPPLEMENTAL_REAL_HISTORY_ONLY',formal_execution_eligible=False,
        blockers=['NO_CERTIFIED_HISTORICAL_LIMIT_PRICES','CORPORATE_ACTION_PAYMENT_AND_SHARE_LISTING_DATES_NOT_CERTIFIED','NO_COMPLETE_PIT_UNIVERSE'])
    write_json(root/'data/history/baostock/manifests'/(hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()+'.json'),result)
    write_json(path,result);write_json(root/'reports/public_history/latest.json',result);return result


def worker(job):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'vendor'))
    import baostock as bs
    socket.setdefaulttimeout(12)
    result={'errors':[],'fields':{}};symbol=job['symbol'];code=('sh.' if symbol.startswith('6') else 'sz.')+symbol
    login=bs.login()
    if login.error_code!='0':raise RuntimeError('BAOSTOCK_LOGIN_FAILED')
    def query(name,fn):
        try:
            response=fn()
            if response.error_code!='0':raise ValueError('API_REJECTED:'+response.error_code)
            frame=response.get_data();result['fields'][name]=list(frame.columns);result[name]=json.loads(frame.to_json(orient='records',force_ascii=False))
        except Exception as exc:result['errors'].append(dict(dataset=name,error=type(exc).__name__))
    try:
        fields='date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST'
        query('daily',lambda:bs.query_history_k_data_plus(code,fields,start_date=job['start'],end_date=job['end'],adjustflag='3'))
        query('hfq',lambda:bs.query_history_k_data_plus(code,'date,code,close',start_date=job['start'],end_date=job['end'],adjustflag='1'))
        query('basic',lambda:bs.query_stock_basic(code=code))
        query('factors',lambda:bs.query_adjust_factor(code=code,start_date=job['start'],end_date=job['end']))
        dividends=[]
        for year in range(int(job['start'][:4]),int(job['end'][:4])+1):
            query('dividend_year',lambda:bs.query_dividend_data(code,year=str(year),yearType='operate'))
            dividends+=result.pop('dividend_year',[])
        result['dividends']=dividends
        Path(job['output']).write_text(json.dumps(result,ensure_ascii=False))
    finally:bs.logout()

if __name__=='__main__':worker(json.loads(Path(sys.argv[1]).read_text()))
