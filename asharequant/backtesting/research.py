"""Fixed-rule exploratory research, deliberately separate from execution."""
import math
import numpy as np
import pandas as pd

from ..core import quality, read_snapshot, stock_config


def indicators(hfq, cfg):
    close = hfq.close
    return pd.DataFrame({
        'fast': close.rolling(cfg['fast_window']).mean(),
        'slow': close.rolling(cfg['slow_window']).mean(),
        'momentum60': close / close.shift(60) - 1,
        'volatility60': close.pct_change(fill_method=None).rolling(60).std() * np.sqrt(252),
    })


def simulate(raw, hfq, cfg, start_index=0, end_index=None, calendar_dates=None):
    """Fractional adjusted-price units, long/cash, signal[t-1] -> open[t].

    This is not a broker simulator: no lots/minimum fees, dividend taxes or exact
    ex-date cash ledger. Adjusted units are a total-return research proxy.
    """
    end_index = end_index or len(hfq)
    ind = indicators(hfq, cfg)
    desired = (ind.fast > ind.slow).shift(1, fill_value=False)
    cash = float(cfg['initial_capital'])
    units = 0.0
    trades = []
    curve = []
    buy_cost = (cfg['commission_bps'] + cfg['slippage_bps']) / 10000
    sell_cost = (cfg['commission_bps'] + cfg['slippage_bps'] + cfg['sell_tax_bps']) / 10000
    for i in range(start_index, end_index):
        # Block entries/exits close to main-board 10% limits, and zero volume.
        # Corporate actions can make raw ratios misleading, so this is only a
        # conservative approximation; results are not executable performance.
        ratio = raw.open.iloc[i] / raw.close.iloc[i - 1] - 1 if i else 0.0
        liquid = raw.volume.iloc[i] > 0
        want = bool(desired.iloc[i])
        price = float(hfq.open.iloc[i])
        if want and units == 0 and liquid and ratio < 0.098:
            units = cash / (price * (1 + buy_cost))
            fee = units * price * buy_cost
            cash = 0.0
            trades.append({'date': hfq.date.iloc[i], 'side': 'buy',
                           'adjusted_price': price, 'cost_CNY': fee})
        elif not want and units > 0 and liquid and ratio > -0.098:
            proceeds = units * price
            fee = proceeds * sell_cost
            cash = proceeds - fee
            units = 0.0
            trades.append({'date': hfq.date.iloc[i], 'side': 'sell',
                           'adjusted_price': price, 'cost_CNY': fee})
        curve.append({'date': hfq.date.iloc[i],
                      'equity': cash + units * float(hfq.close.iloc[i]),
                      'invested': units > 0})
    frame = pd.DataFrame(curve)
    if frame.empty:
        raise ValueError('回测区间为空')
    if calendar_dates is not None:
        valuation_dates = pd.DatetimeIndex(calendar_dates)
        valuation_dates = valuation_dates[(valuation_dates >= frame.date.min()) & (valuation_dates <= frame.date.max())]
        # Only the valuation ledger carries forward through verified suspensions;
        # no synthetic OHLC/volume bars and no orders are created for those days.
        frame = frame.set_index('date').reindex(valuation_dates).ffill().rename_axis('date').reset_index()
    # Charge liquidation cost on a marked terminal valuation; not a real fill.
    if units:
        frame.loc[frame.index[-1], 'equity'] *= 1 - sell_cost
    values = np.r_[cfg['initial_capital'], frame.equity.to_numpy()]
    returns = values[1:] / values[:-1] - 1
    drawdowns = values / np.maximum.accumulate(values) - 1
    n = len(frame)
    total = values[-1] / values[0] - 1
    ann_vol = float(np.std(returns, ddof=1) * np.sqrt(252)) if n > 1 else 0.0
    sharpe = float(np.mean(returns) * 252 / ann_vol) if ann_vol > 0 else None
    stats = {
        'start': str(frame.date.iloc[0].date()), 'end': str(frame.date.iloc[-1].date()),
        'sessions': n, 'total_return': float(total),
        'annualized_return': float((1 + total) ** (252 / n) - 1),
        'max_drawdown': float(drawdowns.min()), 'annualized_volatility': ann_vol,
        'sharpe_zero_rf': sharpe, 'order_count': len(trades),
        'exposure': float(frame.invested.mean()),
        'terminal_exit_cost_applied': bool(units > 0),
        'buy_hold_proxy': float(
            hfq.close.iloc[end_index - 1] / hfq.open.iloc[start_index]
            * (1 - sell_cost) / (1 + buy_cost) - 1),
    }
    return stats, frame, pd.DataFrame(trades, columns=['date', 'side', 'adjusted_price', 'cost_CNY'])


def evaluate(root, cfg, watchlist, cal, end, statuses, quote_map=None):
    failed = {s['symbol'] for s in statuses if not s['ok']}
    candidates = []
    research = {}
    issues = []
    for row in watchlist.itertuples():
        try:
            snapshot = read_snapshot(root, row.symbol)
            if snapshot is None:
                raise ValueError('没有已验证的行情快照')
            raw, hfq = snapshot['raw'], snapshot['hfq']
            q = quality(raw, cal, stock_config(cfg, row.symbol), end)
            reasons = []
            live = (quote_map or {}).get(row.symbol)
            if not live:
                reasons.append('最新证券名称快照不可用')
            elif any(token in live['name'].upper() for token in ('ST', '退')):
                reasons.append('最新证券名称含风险警示')
            elif live['name'] != row.name:
                reasons.append('证券名称变化，需人工确认')
            if row.symbol in failed:
                reasons.append('本次联网更新失败，使用旧版仅供查看')
            if not q['fresh']:
                reasons.append('最新交易日缺失')
            if q['internal_missing_count'] or q['unexpected_count'] or q['suspension_conflict_count']:
                reasons.append('交易日期缺口需核查')
            if len(raw) < cfg['min_history']:
                reasons.append('历史长度不足')
            # Observed trading history proves at least this much listing age;
            # never claim the first downloaded date is the actual IPO date.
            listing = getattr(row, 'listing_date', '')
            listing_ok = (end - raw.date.min()).days >= cfg['min_listing_days']
            if listing:
                listed = pd.Timestamp(listing)
                listing_ok = listed <= raw.date.min() and (end - listed).days >= cfg['min_listing_days']
            if not listing_ok:
                reasons.append('可验证的上市交易历史不足一年')
            mean_amount = float(raw.amount.tail(20).mean())
            if mean_amount < cfg['min_average_amount']:
                reasons.append('20日平均成交额不足')
            if raw.close.iloc[-1] < cfg['min_price']:
                reasons.append('价格低于观察门槛')
            if raw.volume.iloc[-1] <= 0:
                reasons.append('最新交易日无成交')
            if len(raw) < cfg['slow_window'] + 61:
                raise ValueError('数据不足以计算研究指标')
            ind = indicators(hfq, cfg).iloc[-1]
            mom = float(ind.momentum60)
            vol = float(ind.volatility60)
            score = mom - 0.5 * vol
            candidate = {
                'symbol': row.symbol, 'name': row.name,
                'last_date': q['last_date'], 'close': float(raw.close.iloc[-1]),
                'average_amount20': mean_amount, 'momentum60': mom,
                'volatility60': vol, 'score': score,
                'signal': '趋势观察' if ind.fast > ind.slow else '现金观察',
                'eligible': not reasons, 'reason': '；'.join(reasons),
                'quality': q, 'version': snapshot['meta']['version'],
                'name_snapshot': live, 'sector': getattr(row, 'sector', ''),
                'origin': getattr(row, 'origin', ''), 'rationale': getattr(row, 'rationale', ''),
            }
            candidates.append(candidate)
            # Never silently backtest across suspensions/missing sessions or
            # stale data, or accept an unverified pre-listing gap as complete.
            if q['fresh'] and q['calendar_complete'] and len(raw) >= cfg['min_history']:
                split = int(len(raw) * cfg['train_fraction'])
                if split < cfg['slow_window'] + 1 or len(raw) - split < 60:
                    raise ValueError('训练或留出验证区间过短')
                stats, curve, trades = simulate(raw, hfq, cfg, calendar_dates=cal)
                train, _, _ = simulate(raw, hfq, cfg, 0, split, calendar_dates=cal)
                holdout, _, _ = simulate(raw, hfq, cfg, split, len(raw), calendar_dates=cal)
                research[row.symbol] = {
                    'full': stats, 'train': train, 'holdout': holdout,
                    'curve': curve, 'trades': trades,
                    'split_date': str(raw.date.iloc[split].date()),
                }
            else:
                issues.append({'symbol': row.symbol, 'error': '日期未完整核验或历史不足，不生成回测'})
        except Exception as exc:
            issues.append({'symbol': row.symbol, 'error': str(exc)})
    candidates.sort(key=lambda x: (-x['score'], x['symbol']))
    eligible = [c for c in candidates if c['eligible']]
    selected = eligible[:cfg['pool_target']]
    for rank, c in enumerate(selected, 1):
        c['rank'] = rank
    return candidates, selected, research, issues
