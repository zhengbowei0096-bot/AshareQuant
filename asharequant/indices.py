"""Market index ingestion and fixed-specification exploratory feature comparison."""
from pathlib import Path
import hashlib
import json
import sqlite3
import time
import logging
import numpy as np
import pandas as pd
from .core import now, write_json, write_csv, read_snapshot, read_watchlist
from .data import network_job

INDEXES = {'sh000001':'上证指数','sz399001':'深证成指','sh000300':'沪深300','sh000905':'中证500'}
PRICE_COLS = ['open','high','low','close']


def validate_index(frame,start,end,expected=None):
    required=['date',*PRICE_COLS,'amount']
    if frame is None or frame.empty or not set(required).issubset(frame.columns):
        raise ValueError('指数返回为空或字段缺失')
    f=frame[required].copy()
    f['date']=pd.to_datetime(f.date,errors='raise')
    if f.date.isna().any() or f.date.duplicated().any() or not f.date.eq(f.date.dt.normalize()).all():
        raise ValueError('指数日期无效或重复')
    if not f.date.between(start,end).all():
        raise ValueError('指数日期超出区间')
    for col in [*PRICE_COLS,'amount']:
        f[col]=pd.to_numeric(f[col],errors='raise')
        if not np.isfinite(f[col]).all(): raise ValueError('指数数值缺失或非有限')
    if (f[PRICE_COLS]<=0).any().any() or (f.amount<0).any(): raise ValueError('指数点位或源成交量异常')
    if ((f.high < f[['open','close','low']].max(axis=1)) | (f.low > f[['open','close','high']].min(axis=1))).any():
        raise ValueError('指数OHLC关系异常')
    f=f.sort_values('date').reset_index(drop=True)
    if expected is not None:
        expected=pd.DatetimeIndex(expected)
        if not pd.DatetimeIndex(f.date).equals(expected): raise ValueError('指数交易日期缺失或日历外数据')
    return f


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS market_index_bars (symbol TEXT NOT NULL,date TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,source_volume_lots REAL,amount_cny REAL,source TEXT,fetched_at TEXT,version TEXT,PRIMARY KEY(symbol,date))')
    db.execute('CREATE TABLE IF NOT EXISTS market_index_versions (symbol TEXT,version TEXT,metadata TEXT,PRIMARY KEY(symbol,version))')


def upsert(db,symbol,frame,meta):
    schema(db)
    rows=[(symbol,str(r.date.date()),r.open,r.high,r.low,r.close,r.amount,None,'tencent_index',meta['fetched_at'],meta['version']) for r in frame.itertuples()]
    db.executemany('INSERT INTO market_index_bars VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,source_volume_lots=excluded.source_volume_lots,amount_cny=excluded.amount_cny,source=excluded.source,fetched_at=excluded.fetched_at,version=excluded.version',rows)
    db.execute('INSERT OR REPLACE INTO market_index_versions VALUES (?,?,?)',(symbol,meta['version'],json.dumps(meta,ensure_ascii=False)))


def load_index(root,symbol,start,end):
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        f=pd.read_sql_query('SELECT date,open,high,low,close,source_volume_lots AS amount FROM market_index_bars WHERE symbol=? AND date>=? AND date<=? ORDER BY date',db,params=(symbol,str(pd.Timestamp(start).date()),str(pd.Timestamp(end).date())))
    f['date']=pd.to_datetime(f.date)
    return f


def update_indices(root,cfg,end,cal):
    start=pd.Timestamp(end)-pd.DateOffset(years=5)
    expected=pd.DatetimeIndex(cal)
    expected=expected[(expected>=start)&(expected<=end)]
    statuses=[]
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        schema(db)
    for symbol,name in INDEXES.items():
        for attempt in range(cfg['attempts']):
            try:
                old=load_index(root,symbol,start,end)
                with sqlite3.connect(root/'data/market.sqlite3') as db:
                    record=db.execute('SELECT metadata FROM market_index_versions WHERE symbol=? ORDER BY json_extract(metadata,\'$.fetched_at\') DESC LIMIT 1',(symbol,)).fetchone()
                previous=json.loads(record[0]) if record else None
                full=not previous or old.empty or (now()-pd.Timestamp(previous['full_refresh_at']).to_pydatetime()).days>=cfg['full_refresh_days']
                request_start=start if full else max(start,old.date.max()-pd.Timedelta(days=cfg['overlap_days']))
                logging.info('更新市场指数 %s %s %s',symbol,name,'full' if full else 'overlap')
                incoming=network_job(root,cfg,kind='index',symbol=symbol,start=str(request_start.date()),end=str(end.date()))
                incoming=validate_index(incoming,request_start,end)
                if not full:
                    old_overlap=old.loc[old.date>=request_start,'date']
                    if not set(old_overlap).issubset(set(incoming.date)): raise ValueError('增量指数遗漏已有日期')
                    merged=pd.concat([old[old.date<request_start],incoming],ignore_index=True)
                else: merged=incoming
                merged=validate_index(merged,start,end,expected)
                raw_text=merged.to_csv(index=False,date_format='%Y-%m-%d')
                version=hashlib.sha256(raw_text.encode()).hexdigest()
                fetched=now().isoformat()
                meta=dict(symbol=symbol,name=name,version=version,fetched_at=fetched,
                          full_refresh_at=fetched if full else previous['full_refresh_at'],mode='full' if full else 'overlap',
                          start=str(start.date()),end=str(end.date()),first=str(merged.date.min().date()),last=str(merged.date.max().date()),rows=len(merged),
                          source='AKShare stock_zh_index_daily_tx / Tencent',price_units='index_points',
                          source_volume_units='lots per AKShare documentation; unused in model',
                          missing_fields=['amount_cny'],adjustment_note='Provider qfq endpoint; index price level, not total return or stock adjustment factor',
                          documentation='https://github.com/akfamily/akshare/blob/main/docs/data/index/index.md')
                folder=root/'data'/'indices'/symbol/version
                folder.mkdir(parents=True,exist_ok=True)
                # Content-addressed canonical table; fetch metadata lives in SQLite/report.
                path=folder/'prices.csv'
                if not path.exists(): path.write_text(raw_text)
                if hashlib.sha256(path.read_bytes()).hexdigest()!=version: raise ValueError('指数归档哈希冲突')
                with sqlite3.connect(root/'data/market.sqlite3') as db:
                    upsert(db,symbol,merged,meta)
                statuses.append(dict(ok=True,**meta));break
            except Exception as exc:
                logging.warning('指数%s下载失败，第%d次：%s',symbol,attempt+1,str(exc))
                if attempt+1==cfg['attempts']:statuses.append(dict(symbol=symbol,name=name,ok=False,error=str(exc)))
                else:time.sleep(cfg['retry_seconds'])
        time.sleep(cfg['request_spacing_seconds'])
    result=dict(generated_at=now().isoformat(),statuses=statuses,success=all(s['ok'] for s in statuses),
                primary_forecast_model_uses_indices=False)
    write_json(root/'reports/index_data.json',result)
    return result


def market_features(bench,small,stock_close):
    """Only contemporaneous/past completed daily observations; no backfill."""
    b=bench.close.reindex(stock_close.index)
    s=small.close.reindex(stock_close.index)
    br=b.pct_change(fill_method=None)
    sr=stock_close.pct_change(fill_method=None)
    out=pd.DataFrame(index=stock_close.index)
    out['market_r1']=br
    out['market_r5']=b/b.shift(5)-1
    out['market_r20']=b/b.shift(20)-1
    out['market_vol20']=br.rolling(20).std()
    out['small_minus_large20']=s/s.shift(20)-b/b.shift(20)
    out['excess_return20']=stock_close/stock_close.shift(20)-b/b.shift(20)
    out['market_beta60']=sr.rolling(60).cov(br)/br.rolling(60).var()
    return out.replace([np.inf,-np.inf],np.nan)


def compare_indices(root,cfg,end,cal):
    from .forecast import DEFAULTS,FEATURES,features_and_labels,validate_forecast
    config={**DEFAULTS,**json.loads((root/'forecast_config.json').read_text())}
    start=pd.Timestamp(end)-pd.DateOffset(years=5)
    days=pd.DatetimeIndex(cal);days=days[(days>=start)&(days<=end)]
    indices={s:validate_index(load_index(root,s,start,end),start,end,days).set_index('date') for s in INDEXES}
    results=[];sample_frames=[];versions={}
    for item in read_watchlist(root).itertuples():
        snap=read_snapshot(root,item.symbol)
        if snap is None:raise ValueError('股票快照缺失')
        if snap['raw'].date.max()!=days.max():raise ValueError('股票行情过期')
        versions[item.symbol]=snap['meta']['version']
        bars=snap['hfq'];bars=bars[bars.date.between(start,end)]
        stock_days=days[days>=bars.date.min()]
        m=market_features(indices['sh000300'],indices['sh000905'],bars.set_index('date').close.reindex(stock_days))
        for h in (1,5,20):
            frame=features_and_labels(bars,stock_days,h).join(m)
            cols=FEATURES+list(m.columns)
            common=frame.dropna(subset=cols+['target','label_end'])
            baseline,bm=validate_forecast(common,h,config)
            challenger,cm=validate_forecast(common,h,config,feature_columns=cols)
            if not baseline.date.equals(challenger.date):raise ValueError('比较样本不一致')
            improvement=1-cm['mae']/bm['mae']
            results.append(dict(symbol=item.symbol,name=item.name,horizon=h,baseline_mae=bm['mae'],index_mae=cm['mae'],
                                relative_mae_improvement=improvement,baseline_direction=bm['direction_accuracy_nonoverlap'],
                                index_direction=cm['direction_accuracy_nonoverlap'],samples=len(challenger),folds=cm['folds']))
            challenger['baseline_predicted']=baseline.predicted
            challenger['symbol']=item.symbol;challenger['horizon']=h;sample_frames.append(challenger)
    summary={str(h):dict(improved=sum(r['relative_mae_improvement']>0 for r in results if r['horizon']==h),
                         total=sum(r['horizon']==h for r in results),median_mae_improvement=float(np.median([r['relative_mae_improvement'] for r in results if r['horizon']==h]))) for h in (1,5,20)}
    payload=dict(generated_at=now().isoformat(),asof=str(days.max().date()),summary=summary,results=results,
                 stock_versions=versions,model_config=config,features=cols,
                 index_hashes={s:hashlib.sha256(f.to_csv().encode()).hexdigest() for s,f in indices.items()},
                 status='exploratory_comparison_only_no_promotion',
                 limitations=['Same historical validation period previously inspected; not a new untouched test set.',
                              'Current selected universe; no historical universe reconstruction.',
                              'Price-index returns omit dividends while stock labels use adjusted prices.',
                              'No portfolio profit/cost validation or multiple-testing correction.'])
    write_json(root/'reports/index_comparison.json',payload)
    write_csv(root/'reports/index_comparison.csv',pd.DataFrame(results).drop(columns='folds'))
    write_csv(root/'reports/index_validation_predictions.csv',pd.concat(sample_frames,ignore_index=True))
    return payload
