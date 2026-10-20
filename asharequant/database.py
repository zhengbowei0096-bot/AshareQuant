"""Transactional, idempotent SQLite mirror of verified immutable snapshots."""
import json
import sqlite3
from .core import read_snapshot, stock_config, now, write_json
import pandas as pd


def sync_database(root, cfg, watchlist, end, cal):
    path = root / 'data' / 'market.sqlite3'
    start = pd.Timestamp(end) - pd.DateOffset(years=5)
    results = []
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE IF NOT EXISTS daily_bars (symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, turnover REAL, hfq_open REAL, hfq_high REAL, hfq_low REAL, hfq_close REAL, close_adjustment_ratio REAL, suspension_status TEXT NOT NULL, resumption_status TEXT NOT NULL, limit_up REAL, limit_down REAL, limit_status TEXT NOT NULL, missing_fields TEXT NOT NULL, source TEXT NOT NULL, snapshot_version TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(symbol,date))')
        db.execute('CREATE TABLE IF NOT EXISTS observations (symbol TEXT, date TEXT, status TEXT, evidence TEXT, PRIMARY KEY(symbol,date))')
        db.execute('CREATE TABLE IF NOT EXISTS imports (symbol TEXT, snapshot_version TEXT, imported_at TEXT, metadata TEXT, PRIMARY KEY(symbol,snapshot_version))')
        for item in watchlist.itertuples():
            symbol = item.symbol
            snap = read_snapshot(root, symbol)
            if snap is None:
                raise ValueError(f'{symbol}: no verified snapshot')
            meta = snap['meta']
            raw, hfq = snap['raw'], snap['hfq']
            selected = raw.date.between(start, end)
            rows = []
            for r, h in zip(raw[selected].itertuples(), hfq[selected].itertuples()):
                rows.append((symbol, str(r.date.date()), r.open, r.high, r.low, r.close,
                             r.volume, r.amount, r.turnover, h.open, h.high, h.low, h.close,
                             h.close/r.close, 'traded' if r.volume > 0 else 'unknown',
                             'unknown', None, None, 'unknown',
                             'resumption_status,limit_up,limit_down,limit_status,official_adjustment_factor,corporate_actions',
                             meta['source'], meta['version'], meta['fetched_at']))
            if not rows:
                raise ValueError(f'{symbol}: no rows in five-year window')
            listing = cfg.get('listing_dates', {}).get(symbol)
            expected_start = max(start, pd.Timestamp(listing)) if listing else start
            expected = pd.DatetimeIndex(cal)
            expected = expected[(expected >= expected_start) & (expected <= end)]
            actual = set(raw.date)
            events = stock_config(cfg, symbol).get('stock_suspensions', [])
            observations = []
            for day in expected:
                if day in actual:
                    status, evidence = 'bar_available', meta['version']
                else:
                    event = next((e for e in events if pd.Timestamp(e['start']) <= day <= pd.Timestamp(e['end'])), None)
                    status = 'verified_suspension' if event else 'unexplained_missing'
                    evidence = json.dumps(event, ensure_ascii=False) if event else 'No inference of suspension from missing bar'
                observations.append((symbol, str(day.date()), status, evidence))
            db.executemany('INSERT INTO daily_bars VALUES (' + ','.join(['?']*23) + ') ON CONFLICT(symbol,date) DO UPDATE SET ' + ','.join(f'{c}=excluded.{c}' for c in ['open','high','low','close','volume','amount','turnover','hfq_open','hfq_high','hfq_low','hfq_close','close_adjustment_ratio','suspension_status','resumption_status','limit_up','limit_down','limit_status','missing_fields','source','snapshot_version','fetched_at']), rows)
            db.executemany('INSERT OR REPLACE INTO observations VALUES (?,?,?,?)', observations)
            db.execute('INSERT OR IGNORE INTO imports VALUES (?,?,?,?)', (symbol, meta['version'], now().isoformat(), json.dumps(meta, ensure_ascii=False)))
            unknown = sum(o[2] == 'unexplained_missing' for o in observations)
            results.append(dict(symbol=symbol, rows=len(rows), first=rows[0][1], last=rows[-1][1], unexplained_missing=unknown, listing_date_verified=bool(listing), snapshot_version=meta['version']))
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise ValueError(integrity)
    result = dict(database=str(path), requested_start=str(start.date()), end=str(pd.Timestamp(end).date()), integrity=integrity, symbols=results,
                  limitations=['Close adjustment ratio is derived, not an official factor or corporate action record.', 'Limit prices and resumption flags unavailable: NULL/unknown; never assume 10%.', 'Current watchlist is not a point-in-time historical universe.', 'Source availability does not establish redistribution or commercial licensing.'])
    write_json(root / 'reports' / 'stage1_database.json', result)
    return result
