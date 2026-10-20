"""Automatic read-only CSV intake; local exports never become authenticated broker facts."""
from pathlib import Path
import csv,io,json,hashlib,sqlite3,math
import pandas as pd
from ..core import now,write_json

ALIASES={'symbol':['证券代码','股票代码'],'date':['日期','成交日期','交易日期'],'time':['成交时间'],
 'fill_id':['成交编号','成交序号'],'order_id':['委托编号','合同编号'],'side':['买卖标志','买卖方向','操作'],
 'shares':['成交数量'],'price':['成交价格','成交均价'],'fees':['总费用','费用合计'],
 'nav':['总资产','资产总值'],'cash':['资金余额','现金余额'],'available_cash':['可用资金'],'external_flow':['净入金']}


def normalize(kind,row):
    mapped={str(k).strip().lstrip('\ufeff'):str(v).strip() for k,v in row.items() if k is not None}
    for key,aliases in ALIASES.items():
        values=[mapped[x] for x in [key]+aliases if mapped.get(x)]
        if len(set(values))>1:raise ValueError('AMBIGUOUS_COLUMN:'+key)
        if values:mapped[key]=values[0]
    def number(key,required=True):
        if not mapped.get(key):
            if required:raise ValueError('MISSING:'+key)
            return None
        value=float(mapped[key].replace(',',''))
        if not math.isfinite(value):raise ValueError('NONFINITE:'+key)
        return value
    date=mapped.get('date')
    if not date:raise ValueError('MISSING:date')
    if date.isdigit() and len(date)==8:date=f'{date[:4]}-{date[4:6]}-{date[6:]}'
    day=pd.Timestamp(date)
    if pd.isna(day) or day.date()>now().date():raise ValueError('FUTURE_OR_INVALID_DATE')
    value=dict(date=str(day.date()),timestamp=None,time_precision='DAY_ONLY',broker='华福证券',verified=False,source_status='LOCAL_EXPORT_UNAUTHENTICATED')
    if mapped.get('time'):
        ts=pd.Timestamp(str(day.date())+' '+mapped['time'])
        if ts.tzinfo is None:ts=ts.tz_localize('Asia/Shanghai')
        if ts>pd.Timestamp(now()):raise ValueError('FUTURE_TIMESTAMP')
        value.update(timestamp=ts.isoformat(),time_precision='PROVIDED_TIME_BEIJING')
    if kind=='nav':
        nav=number('nav');cash=number('cash',False);available=number('available_cash',False)
        if nav<0 or cash is not None and (cash<0 or cash>nav+.011) or available is not None and (available<0 or cash is None or available>cash+.011):raise ValueError('INVALID_ACCOUNT_VALUES')
        value.update(nav=nav,cash=cash,available_cash=available,external_flow=number('external_flow',False))
        natural='nav|'+value['date']
    elif kind=='fills':
        code=mapped.get('symbol','').lstrip("'=").rstrip('"');code=code.split('.')[0] if '.' in code else code
        if not code.isdigit() or len(code)>6:raise ValueError('INVALID_SYMBOL')
        code=code.zfill(6);qty=number('shares');price=number('price');fees=number('fees',False)
        side={'买入':'BUY','证券买入':'BUY','卖出':'SELL','证券卖出':'SELL','BUY':'BUY','SELL':'SELL'}.get(mapped.get('side'))
        if side is None or qty<=0 or qty!=int(qty) or price<=0 or fees is not None and fees<0:raise ValueError('INVALID_FILL')
        if not mapped.get('fill_id'):raise ValueError('MISSING_STABLE_FILL_ID')
        value.update(symbol=code,shares=int(qty),price=price,fees=fees,side=side,fill_id=mapped['fill_id'],order_id=mapped.get('order_id'))
        natural='fill|'+value['date']+'|'+value['fill_id']
    else:raise ValueError('UNKNOWN_DATASET')
    return natural,value


def parse(kind,raw):
    for encoding in ('utf-8-sig','gb18030'):
        try:text=raw.decode(encoding);break
        except UnicodeDecodeError:continue
    else:raise ValueError('UNKNOWN_CSV_ENCODING')
    reader=csv.DictReader(io.StringIO(text));result=[]
    if not reader.fieldnames:raise ValueError('MISSING_CSV_HEADER')
    for row in reader:
        if any(v and str(v).strip() for v in row.values()):result.append(normalize(kind,row))
    if not result:raise ValueError('EMPTY_EXPORT_NOT_ACCOUNT_FACT')
    if len({k for k,_ in result})!=len(result):raise ValueError('DUPLICATE_EXPORT_IDS')
    return result


def ingest(root):
    root=Path(root);inbox=root/'data/broker_inbox';errors=[];added=0;processed=0
    for kind in ('nav','fills'):(inbox/kind).mkdir(parents=True,exist_ok=True)
    (root/'data').mkdir(exist_ok=True)
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS broker_observations (id TEXT PRIMARY KEY,body TEXT NOT NULL,sha256 TEXT NOT NULL,source_sha256 TEXT NOT NULL,observed_at TEXT NOT NULL)')
        for action in ('UPDATE','DELETE'):db.execute(f'CREATE TRIGGER IF NOT EXISTS broker_obs_{action} BEFORE {action} ON broker_observations BEGIN SELECT RAISE(ABORT,"immutable broker observations"); END')
        db.commit()
        source_config=root/'broker_sources.json'
        sources=json.loads(source_config.read_text()) if source_config.exists() else {}
        for kind in ('nav','fills'):
            directories=[inbox/kind]
            for value in sources.get(kind+'_directories',[]):
                directory=Path(value)
                if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
                    errors.append(dict(file='authorized-directory',error='INVALID_AUTHORIZED_SOURCE_DIRECTORY'));continue
                directories.append(directory)
            candidates=[(directory,p) for directory in directories for p in sorted(directory.glob('*.csv'))]
            for directory,path in candidates:
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):raise ValueError('INBOX_PATH_ESCAPE')
                    if path.stat().st_size>10_000_000:raise ValueError('EXPORT_TOO_LARGE')
                    raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest();records=parse(kind,raw)
                    archive=root/'data/broker_records/raw'/(sha+'.csv');archive.parent.mkdir(parents=True,exist_ok=True)
                    if archive.exists() and archive.read_bytes()!=raw:raise ValueError('SOURCE_ARCHIVE_CHANGED')
                    archive.write_bytes(raw);archive.chmod(0o600)
                    new=0
                    with db:
                        for key,value in records:
                            body=json.dumps(value,sort_keys=True,ensure_ascii=False);checksum=hashlib.sha256(body.encode()).hexdigest()
                            old=db.execute('SELECT sha256 FROM broker_observations WHERE id=?',(key,)).fetchone()
                            if old and old[0]!=checksum:raise ValueError('CONFLICTING_EXPORT_REQUIRES_REVIEW')
                            if old:continue
                            db.execute('INSERT INTO broker_observations VALUES(?,?,?,?,?)',(key,body,checksum,sha,now().isoformat()));new+=1
                    added+=new;processed+=1
                except Exception as exc:errors.append(dict(file=path.name,error=str(exc)[:160]))
        counts=dict(nav=0,fills=0)
        for key, in db.execute('SELECT id FROM broker_observations'):counts['nav' if key.startswith('nav|') else 'fills']+=1
    result=dict(generated_at=now().isoformat(),status='LOCAL_EXPORTS_UNVERIFIED' if sum(counts.values()) else 'NO_REAL_BROKER_EXPORTS',counts=counts,added=added,processed_files=processed,errors=errors,
        live_connection=False,broker_verified=False,account_updated=False,submitted_orders=0,
        blockers=['AUTHENTICATED_READONLY_BROKER_FEED_NOT_CONFIGURED','ORDER_ARRIVAL_PRICE_NOT_AVAILABLE'])
    write_json(root/'reports/broker_records/latest.json',result);return result
