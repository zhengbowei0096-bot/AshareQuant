from datetime import datetime
from pathlib import Path
import json
import logging
import subprocess
import sys
import tempfile
import time

import pandas as pd

from .core import (TZ, now, atomic_text, write_json, write_csv, validate,
                   read_snapshot, commit_snapshot, quality, stock_config)


def network_job(root, cfg, **params):
    with tempfile.TemporaryDirectory(prefix='asharequant-') as directory:
        job_path = Path(directory) / 'job.json'
        out = Path(directory) / 'response.csv'
        job_path.write_text(json.dumps({**params, 'output': str(out)}))
        result = subprocess.run(
            [sys.executable, '-m', 'asharequant.worker', str(job_path)],
            cwd=root, capture_output=True, text=True,
            timeout=cfg['worker_timeout_seconds'],
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-1500:] or '下载进程失败')
        return pd.read_csv(out, dtype={'symbol': str, 'quote_time': str}) if params.get('kind') == 'quotes' else pd.read_csv(out)


def calendar(root, cfg, end, offline=False):
    path = root / 'data' / 'calendar.csv'
    meta_path = root / 'data' / 'calendar.json'
    error = None
    if not offline:
        try:
            df = network_job(root, cfg, kind='calendar')
            dates = pd.to_datetime(df['trade_date'], errors='raise')
            if dates.isna().any() or dates.duplicated().any():
                raise ValueError('交易日历存在无效或重复日期')
            if dates.min() > pd.Timestamp(cfg['start_date']) or dates.max() < end:
                raise ValueError('日历接口返回范围不足')
            write_csv(path, pd.DataFrame({'trade_date': dates.sort_values()}))
            write_json(meta_path, {'fetched_at': now().isoformat(), 'source': 'sina'})
        except Exception as exc:
            error = str(exc)
            logging.warning('交易日历下载失败，检查缓存：%s', error[-250:])
    if not path.exists() or not meta_path.exists():
        raise RuntimeError('没有可用的交易日历缓存；联网成功后再运行')
    meta = json.loads(meta_path.read_text())
    age = (now() - datetime.fromisoformat(meta['fetched_at'])).total_seconds() / 86400
    if age < 0:
        raise ValueError('交易日历获取时间位于未来')
    if age > cfg['calendar_max_age_days']:
        raise ValueError('交易日历缓存过期，请联网刷新')
    dates = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(path)['trade_date'], errors='raise'))
    if dates.hasnans or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('交易日历缓存损坏')
    if dates.min() > pd.Timestamp(cfg['start_date']) or dates.max() < end:
        raise ValueError('交易日历缓存覆盖范围不足')
    return dates, {**meta, 'age_days': round(age, 3), 'cached_fallback': bool(error),
                   'offline': offline}


def merge_increment(old, incoming, start, end):
    # Refresh the overlap, but never erase an existing observation when an
    # upstream response silently omits it.
    covered_old = old.loc[old.date >= pd.Timestamp(start), 'date']
    if not set(covered_old).issubset(set(incoming.date)):
        raise ValueError('增量返回缺少已有日期，拒绝替换')
    result = pd.concat([old[old.date < pd.Timestamp(start)], incoming])
    return validate(result, old.date.min(), end)


def update_symbol(root, cfg, symbol, end, cal, force_full=False):
    cfg = stock_config(cfg, symbol)
    old = read_snapshot(root, symbol)
    full = force_full or old is None
    if old:
        full_age = (now() - datetime.fromisoformat(old['meta']['full_refresh_at'])).days
        full = full or full_age >= cfg['full_refresh_days']
    start = pd.Timestamp(cfg['start_date'])
    if old and not full:
        start = max(start, old['raw'].date.max() - pd.Timedelta(days=cfg['overlap_days']))
    raw = validate(network_job(root, cfg, kind='prices', symbol=symbol,
                              start=str(start.date()), end=str(end.date()), adjust=''), start, end)
    time.sleep(cfg['request_spacing_seconds'])
    # Adjusted history is always fully refreshed: corporate actions can revise
    # the entire series. Never splice adjustment regimes from different fetches.
    hfq = validate(network_job(root, cfg, kind='prices', symbol=symbol,
                              start=cfg['start_date'], end=str(end.date()), adjust='hfq'),
                   cfg['start_date'], end)
    if old:
        if full:
            if not set(old['raw'].date).issubset(set(raw.date)):
                raise ValueError('全量返回少于已有日期，保留旧版')
        else:
            raw = merge_increment(old['raw'], raw, start, end)
    if not raw.date.equals(hfq.date):
        raise ValueError('不复权与后复权日期不一致')
    q = quality(raw, cal, cfg, end)
    if q['unexpected_count'] or q['suspension_conflict_count']:
        raise ValueError('存在日历外数据，拒绝保存')
    if not q['fresh']:
        raise ValueError('最新交易日缺失，保留旧版；需核实停牌或数据延迟')
    if old and q['internal_missing_count'] > old['meta']['quality']['internal_missing_count']:
        raise ValueError('历史日期缺口增加，保留旧版')
    metadata = {
        'start': cfg['start_date'], 'end': str(end.date()),
        'fetched_at': now().isoformat(),
        'full_refresh_at': now().isoformat() if full else old['meta']['full_refresh_at'],
        'refresh_mode': 'full' if full else 'overlap', 'quality': q,
    }
    return commit_snapshot(root, symbol, raw, hfq, metadata)


def update_all(root, cfg, watchlist, end, cal, full=False):
    statuses = []
    for row in watchlist.itertuples():
        error = None
        for attempt in range(1, cfg['attempts'] + 1):
            try:
                logging.info('更新 %s %s，第%d次', row.symbol, row.name, attempt)
                meta = update_symbol(root, cfg, row.symbol, end, cal, full)
                statuses.append({'symbol': row.symbol, 'name': row.name, 'ok': True,
                                 'version': meta['version'], 'quality': meta['quality']})
                break
            except Exception as exc:
                error = str(exc)
                logging.warning('%s 更新失败：%s', row.symbol, error[-250:])
                if attempt < cfg['attempts']:
                    time.sleep(cfg['retry_seconds'] * attempt)
        else:
            statuses.append({'symbol': row.symbol, 'name': row.name,
                             'ok': False, 'error': error})
        time.sleep(cfg['request_spacing_seconds'])
    return statuses


def quotes(root, cfg, symbols, end, cal, offline=False):
    path = root / 'data' / 'quotes.csv'
    if not offline:
        df = network_job(root, cfg, kind='quotes', symbols=symbols)
        write_csv(path, df)
    if not path.exists():
        raise ValueError('没有可用的最新名称快照')
    df = pd.read_csv(path, dtype={'symbol': str, 'quote_time': str})
    if df.symbol.duplicated().any() or not set(symbols).issubset(set(df.symbol)):
        raise ValueError('名称快照缺少股票或存在重复')
    times = pd.to_datetime(df.quote_time, format='%Y%m%d%H%M%S', errors='raise')
    expected = pd.DatetimeIndex(cal)[pd.DatetimeIndex(cal) <= end].max()
    if times.isna().any() or (times.dt.normalize() < expected).any():
        raise ValueError('名称快照已过期，不能判断最新风险警示名称')
    if (times.dt.normalize() > pd.Timestamp(now().date())).any():
        raise ValueError('名称快照日期超前')
    return {row.symbol: {'name': row.name, 'quote_time': row.quote_time,
                        'pe': row.pe, 'market_cap_100m': row.market_cap_100m}
            for row in df.itertuples()}
