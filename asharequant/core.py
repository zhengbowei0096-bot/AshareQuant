from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid

import numpy as np
import pandas as pd

TZ = ZoneInfo('Asia/Shanghai')
FIELDS = ['open', 'close', 'high', 'low', 'volume', 'turnover', 'amount']
PREFIXES = ('600', '601', '603', '605', '000', '001', '002', '003')


def now():
    return datetime.now(TZ)


def cutoff(at=None):
    at = (at or now()).astimezone(TZ)
    # Conservative publication buffer. Intraday/incomplete bars are never used.
    return pd.Timestamp(at.date() if at.hour >= 18 else at.date() - timedelta(days=1))


def load_config(root):
    cfg = json.loads((root / 'config.json').read_text())
    if not 20 <= cfg['pool_min'] <= cfg['pool_target'] <= cfg['pool_max'] <= 50:
        raise ValueError('观察池参数必须满足 20 ≤ min ≤ target ≤ max ≤ 50')
    if not 1 < cfg['fast_window'] < cfg['slow_window'] < cfg['min_history']:
        raise ValueError('均线窗口或最少历史长度无效')
    if not 0.5 <= cfg['train_fraction'] <= 0.85:
        raise ValueError('训练集比例应在0.5至0.85之间')
    for key in ('commission_bps', 'slippage_bps', 'sell_tax_bps'):
        if not 0 <= cfg[key] < 1000:
            raise ValueError(f'{key} 参数无效')
    for key in ('worker_timeout_seconds', 'full_refresh_days', 'overlap_days', 'attempts'):
        if cfg[key] <= 0:
            raise ValueError(f'{key} 必须大于0')
    return cfg


def read_watchlist(root):
    df = pd.read_csv(root / 'watchlist.csv', dtype=str, keep_default_na=False)
    if not {'symbol', 'name', 'enabled'}.issubset(df.columns):
        raise ValueError('名单必须包含 symbol,name,enabled')
    for col in df.columns:
        df[col] = df[col].str.strip()
    if df['symbol'].duplicated().any():
        raise ValueError('名单存在重复代码')
    if not df['enabled'].isin(['0', '1']).all():
        raise ValueError('enabled只能是0或1')
    for row in df.itertuples():
        if not re.fullmatch(r'[0-9]{6}', row.symbol) or not row.symbol.startswith(PREFIXES):
            raise ValueError(f'非支持的沪深主板代码：{row.symbol}')
        if not row.name or any(t in row.name.upper() for t in ('ST', '退')):
            raise ValueError(f'名称缺失或风险警示股票不纳入：{row.symbol}')
    active = df[df.enabled == '1'].copy()
    if active.empty or len(active) > 50:
        raise ValueError('启用名单应为1至50只；未满20只会报告观察池不足')
    return active


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, obj):
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False, default=str))


def write_csv(path, df):
    atomic_text(path, '\ufeff' + df.to_csv(index=False, date_format='%Y-%m-%d'))


@contextmanager
def run_lock(root):
    path = root / 'logs' / 'update.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一次更新正在运行，请稍后再试') from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def validate(df, start, end):
    if df is None or df.empty:
        raise ValueError('返回数据为空')
    if not {'date', *FIELDS}.issubset(df.columns):
        raise ValueError('行情字段缺失')
    df = df[['date', *FIELDS]].copy()
    df['date'] = pd.to_datetime(df['date'], errors='raise')
    if (df.date.isna().any() or df.date.duplicated().any()
            or not df.date.eq(df.date.dt.normalize()).all()):
        raise ValueError('日期缺失、重复或含盘中时间')
    if not df.date.between(pd.Timestamp(start), pd.Timestamp(end)).all():
        raise ValueError('返回日期超出请求范围')
    for col in FIELDS:
        df[col] = pd.to_numeric(df[col], errors='raise')
        if not np.isfinite(df[col]).all():
            raise ValueError(f'{col}存在非有限数值')
    if (df[['open', 'close', 'high', 'low']] <= 0).any().any():
        raise ValueError('价格必须为正')
    if (df[['volume', 'turnover', 'amount']] < 0).any().any():
        raise ValueError('成交数据不能为负')
    if ((df.high < df[['open', 'close', 'low']].max(axis=1) - 1e-8)
            | (df.low > df[['open', 'close', 'high']].min(axis=1) + 1e-8)).any():
        raise ValueError('OHLC价格关系异常')
    return df.sort_values('date').reset_index(drop=True)


def adapt_tencent(df, symbol):
    # AKShare 1.19.1 skips its shares conversion for sz000xxx stocks (index prefix
    # collision). These main-board stocks therefore still return lots; correct here.
    import akshare as ak
    if ak.__version__ != '1.19.1':
        raise ValueError('当前单位适配仅验证AKShare 1.19.1；升级后需复核')
    df = df.copy()
    if symbol.startswith('000'):
        df['volume'] = pd.to_numeric(df['volume'], errors='raise') * 100
    return df


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_snapshot(root, symbol):
    base = root / 'data' / 'normalized' / symbol
    pointer = base / 'latest.json'
    if not pointer.exists():
        return None
    meta = json.loads(pointer.read_text())
    version = meta['version']
    if not re.fullmatch(r'[a-f0-9]{32}', version):
        raise ValueError('无效数据版本路径')
    folder = base / 'versions' / version
    result = {'meta': meta}
    for kind in ('raw', 'hfq'):
        path = folder / f'{kind}.csv'
        if digest(path) != meta['sha256'][kind]:
            raise ValueError(f'{symbol} {kind}文件校验失败')
        result[kind] = validate(pd.read_csv(path), meta['start'], meta['end'])
    if not result['raw'].date.equals(result['hfq'].date):
        raise ValueError('不复权与后复权日期不一致')
    return result


def commit_snapshot(root, symbol, raw, hfq, metadata):
    if not raw.date.equals(hfq.date):
        raise ValueError('不复权与后复权日期不一致，拒绝保存')
    base = root / 'data' / 'normalized' / symbol
    version = uuid.uuid4().hex
    folder = base / 'versions' / version
    folder.mkdir(parents=True)
    hashes = {}
    for kind, frame in [('raw', raw), ('hfq', hfq)]:
        write_csv(folder / f'{kind}.csv', frame)
        hashes[kind] = digest(folder / f'{kind}.csv')
    meta = {**metadata, 'symbol': symbol, 'version': version, 'sha256': hashes,
            'units': {'prices': 'CNY', 'volume': 'shares', 'amount': 'CNY', 'turnover': 'fraction'},
            'source': 'tencent', 'adjustments': ['none', 'hfq'], 'akshare_version': '1.19.1'}
    write_json(folder / 'metadata.json', meta)
    # Only publish after both files are durable. An interrupted write leaves the
    # old manifest intact; unreferenced versions never become active.
    write_json(base / 'latest.json', meta)
    return meta


def quality(raw, calendar, cfg, end):
    cal = pd.DatetimeIndex(calendar).sort_values().unique()
    start = pd.Timestamp(cfg['start_date'])
    if len(cal) == 0 or cal.min() > start or cal.max() < end:
        raise ValueError('交易日历未覆盖请求区间，不能判定完整性')
    expected = cal[(cal >= start) & (cal <= end)]
    if len(expected) == 0:
        raise ValueError('目标区间没有交易日')
    actual = pd.DatetimeIndex(raw.date)
    observed_missing = expected.difference(actual)
    known = pd.DatetimeIndex([])
    for event in cfg.get('stock_suspensions', []):
        known = known.union(cal[(cal >= pd.Timestamp(event['start'])) & (cal <= pd.Timestamp(event['end']))])
    explained = observed_missing.intersection(known)
    conflict = actual.intersection(known)
    missing = observed_missing.difference(known)
    extra = actual.difference(expected)
    internal = missing[missing >= actual.min()]
    leading = missing[missing < actual.min()]
    latest = expected.max()
    return {
        'rows': len(raw), 'first_date': str(actual.min().date()),
        'last_date': str(actual.max().date()), 'expected_latest': str(latest.date()),
        'missing_count': len(missing), 'observed_missing_count': len(observed_missing),
        'known_suspension_count': len(explained),
        'known_suspension_dates': [str(d.date()) for d in explained],
        'suspension_conflict_count': len(conflict),
        'leading_uncovered_count': len(leading),
        'internal_missing_count': len(internal), 'unexpected_count': len(extra),
        'missing_dates': [str(d.date()) for d in missing[:100]],
        'fresh': bool(actual.max() == latest),
        'calendar_complete': bool(len(missing) == 0 and len(extra) == 0 and len(conflict) == 0),
        'note': '缺口需核实停牌、上市时间或下载遗漏；不自动填充或当作零收益。',
    }


def stock_config(cfg, symbol):
    result = dict(cfg)
    result['stock_suspensions'] = cfg.get('verified_suspensions', {}).get(symbol, [])
    listing = cfg.get('listing_dates', {}).get(symbol)
    if listing:
        result['start_date'] = str(max(pd.Timestamp(cfg['start_date']), pd.Timestamp(listing)).date())
    return result
