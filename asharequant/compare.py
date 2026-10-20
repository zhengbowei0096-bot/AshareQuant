"""Compare preserved Eastmoney baseline against normalized Tencent observations."""
from pathlib import Path
import logging

import numpy as np
import pandas as pd

from .core import read_watchlist, read_snapshot, write_json


def compare_frames(em, tx):
    left = em.rename(columns={'日期':'date','开盘':'open','收盘':'close','最高':'high','最低':'low',
                              '成交量':'volume','成交额':'amount','换手率':'turnover'}).copy()
    left['date'] = pd.to_datetime(left['date'], errors='raise')
    left['volume'] = pd.to_numeric(left['volume'], errors='raise') * 100
    left['turnover'] = pd.to_numeric(left['turnover'], errors='raise') / 100
    fields = ['open','close','high','low','volume','amount','turnover']
    for field in fields:
        left[field] = pd.to_numeric(left[field], errors='raise')
        if not np.isfinite(left[field]).all():
            raise ValueError(f'基线{field}存在无效数值')
    merged = left[['date',*fields]].merge(tx[['date',*fields]], on='date',
                                           suffixes=('_em','_tx'), validate='one_to_one')
    if merged.empty:
        raise ValueError('没有共同日期')
    differences = {}
    for field in fields:
        a, b = merged[field+'_em'], merged[field+'_tx']
        tolerance = {'volume':100, 'turnover':.00011, 'amount':1}.get(field,.011)
        allowed = np.maximum(tolerance, np.maximum(a.abs(),b.abs()) * 1e-7) if field == 'amount' else tolerance
        diff = (a-b).abs()
        differences[field] = {'max_absolute_difference':float(diff.max()),
                              'outside_tolerance':int((diff > allowed).sum())}
    return {'common_dates':len(merged), 'baseline_dates':len(left), 'tencent_dates':len(tx),
            'baseline_dates_absent_from_tencent':len(set(left.date)-set(tx.date)),
            'fields':differences, 'note':'仅比较共同历史区间；基线文件不是每日更新的第二来源。'}


def main():
    root = Path(__file__).resolve().parents[1]
    results = []
    failures = 0
    for row in read_watchlist(root).itertuples():
        baseline = root/'data'/f'{row.symbol}_daily_raw.csv'
        if not baseline.exists():
            print(row.symbol, '无独立来源基线，跳过跨来源比较')
            continue
        try:
            snapshot = read_snapshot(root,row.symbol)
            if snapshot is None:
                raise ValueError('没有腾讯标准快照')
            result = compare_frames(pd.read_csv(baseline), snapshot['raw'])
            good = result['baseline_dates_absent_from_tencent'] == 0 and all(
                v['outside_tolerance'] == 0 for v in result['fields'].values())
            failures += not good
            results.append({'symbol':row.symbol, 'ok':good, **result})
            print(row.symbol, '比较通过' if good else '存在待核查差异', result)
        except Exception as exc:
            failures += 1
            results.append({'symbol':row.symbol,'ok':False,'error':str(exc)})
    write_json(root/'reports/source_comparison.json',results)
    return 2 if failures else 0
