from html import escape
import json
from pathlib import Path
import shutil

import pandas as pd

from ..core import now, write_json, write_csv, atomic_text


DISCLAIMER = (
    '研究系统，不产生订单。当前名单由用户指定；风险警示按最新行情名称检查，不能代替交易所风险公告核验。'
    '排名是60日动量减0.5倍年化波动率，不是收益预测或买入建议。'
    '历史回测采用固定20/60日均线、下一交易日开盘模拟、可分割复权单位和固定费用；'
    '未精确模拟100股整手、最低佣金、历史税率、分红税及实际涨跌停排队。'
    '当前股票名单用于历史研究存在幸存者偏差，未回测历史动态选股；'
    '留出区间仅作描述性验证，不等于严格样本外证明。不自动调参、不自动实盘。'
)


def pct(x):
    return '—' if x is None else f'{x:.2%}'


def chart(curve):
    vals = curve.equity.to_numpy()
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    points = ' '.join(f'{i/max(1,len(vals)-1)*880:.1f},{160-(v-lo)/span*145:.1f}'
                      for i, v in enumerate(vals))
    return (f'<svg viewBox="0 0 880 180" role="img" aria-label="研究策略净值曲线">'
            f'<polyline points="{points}" fill="none" stroke="#42d6ba" stroke-width="2"/>'
            f'</svg><small>净值范围 {lo:,.0f}–{hi:,.0f} 元；横轴按交易日排列</small>')


def build_report(root, cfg, watchlist, end, cal_meta, statuses, candidates, pool, research, issues, mode):
    stamp = now().strftime('%Y%m%d-%H%M%S-%f')
    folder = root / 'reports' / stamp
    folder.mkdir(parents=True)
    failures = [s for s in statuses if not s['ok']]
    shortfall = max(0, cfg['pool_min'] - len(pool))
    payload = {
        'generated_at': now().isoformat(), 'cutoff': str(end.date()), 'mode': mode,
        'calendar': cal_meta, 'enabled_count': len(watchlist), 'pool_count': len(pool),
        'pool_target': cfg['pool_target'], 'pool_shortfall_min': shortfall,
        'update_statuses': statuses, 'issues': issues, 'candidates': candidates,
        'pool': pool, 'config': cfg, 'disclaimer': DISCLAIMER,
        'research': {sym: {k: v for k, v in r.items() if k not in ('curve', 'trades')}
                     for sym, r in research.items()},
    }
    write_json(folder / 'summary.json', payload)
    flat_cols = ['rank', 'symbol', 'name', 'last_date', 'close', 'average_amount20',
                 'momentum60', 'volatility60', 'score', 'signal', 'eligible', 'reason']
    write_csv(folder / 'pool.csv', pd.DataFrame(pool).reindex(columns=flat_cols))
    write_csv(folder / 'candidates.csv', pd.DataFrame(candidates).reindex(columns=flat_cols))
    for symbol, r in research.items():
        write_csv(folder / f'{symbol}_equity.csv', r['curve'])
        write_csv(folder / f'{symbol}_trades.csv', r['trades'])
    pool_status = '研究观察池已生成' if not shortfall else f'名单/合格数据不足：距配置最少{cfg["pool_min"]}只还差 {shortfall} 只'
    rows = []
    for c in candidates:
        rows.append('<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in [
            c.get('rank', '—'), c['symbol'], c['name'], c['last_date'], f"{c['close']:.2f}",
            pct(c['momentum60']), pct(c['volatility60']), c['signal'],
            c['reason'] or ('技术条件符合；已核实历史停牌%d天' % c['quality']['known_suspension_count'] if c['quality']['known_suspension_count'] else '技术条件符合；需关注交易所公告'),
            c.get('sector',''), c.get('rationale',''),
        ]) + '</tr>')
    research_html = []
    for symbol, r in research.items():
        table = ''
        for label, key in [('全区间', 'full'), ('前70%参考区间', 'train'), ('后30%留出区间', 'holdout')]:
            s = r[key]
            table += ('<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in [
                label, s['start'] + ' 至 ' + s['end'], pct(s['total_return']),
                pct(s['annualized_return']), pct(s['max_drawdown']),
                '—' if s['sharpe_zero_rf'] is None else f"{s['sharpe_zero_rf']:.2f}",
                s['order_count'], pct(s['buy_hold_proxy']),
            ]) + '</tr>')
        research_html.append(f'<section><h2>{escape(symbol)} · 固定均线研究</h2>' + chart(r['curve']) +
            '<div class="scroll"><table><thead><tr><th>区间</th><th>日期</th><th>总收益</th>'
            '<th>年化</th><th>最大回撤</th><th>夏普(无风险利率0)</th><th>模拟订单</th>'
            '<th>持有对照</th></tr></thead><tbody>' + table + '</tbody></table></div>'
            '<p>各区间从现金独立开始。末期持仓按收盘标记并扣除估算退出费用，不代表实际成交。</p></section>')
    warnings = failures + issues
    warning_html = ''.join('<li>' + escape(s.get('symbol', '') + '：' + s.get('error', '')) + '</li>'
                           for s in warnings)
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AshareQuant 研究日报</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#0c1320;color:#e9eef7;font:15px/1.7 -apple-system,BlinkMacSystemFont,sans-serif}
main{max-width:1200px;margin:auto;padding:36px 24px}h1{font-size:32px;margin:0}h2{font-size:21px}small,.muted{color:#a9bbd1}
.cards{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}.card,section{background:#142136;border:1px solid #293d57;border-radius:12px;padding:20px;margin:18px 0}.card{min-width:150px;margin:0}.value{font-size:28px;color:#42d6ba}.notice{border-left:4px solid #e2b45b;padding:12px 18px;background:#252333}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}td,th{text-align:left;padding:10px;border-bottom:1px solid #293d57}th{color:#a9bbd1}svg{width:100%;max-height:260px}a{color:#7edacb}li{overflow-wrap:anywhere}
</style><main>'''
    html += (f'<h1>AshareQuant · 研究日报</h1><div class="muted">北京时间 {escape(payload["generated_at"])}'
             f' · 数据截止 {escape(str(end.date()))} · {escape(mode)}</div>'
             f'<div class="cards"><div class="card">启用候选<div class="value">{len(watchlist)}</div></div>'
             f'<div class="card">技术条件符合<div class="value">{len(pool)} / {cfg["pool_target"]}</div></div>'
             f'<div class="card">更新失败<div class="value">{len(failures)}</div></div></div>'
             f'<p class="notice">{escape(pool_status)}。不会自动添加用户未指定的股票。</p>'
             '<section><h2>候选名单与状态</h2><div class="scroll"><table><thead><tr>'
             '<th>排名</th><th>代码</th><th>名称</th><th>最新日期</th><th>不复权收盘</th>'
             '<th>60日动量</th><th>年化波动</th><th>趋势状态</th><th>说明</th><th>行业</th><th>候选理由</th>'
             '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></section>')
    if (root / 'reports' / 'forecasts' / 'latest' / 'index.html').exists():
        html += '<section><h2>多期限预测与15万元模拟资金规划</h2><p><a href="../forecasts/latest/index.html">打开统一七项指标、九因子评分及预测（请核对页面数据日期）</a></p></section>'
    seven_path=root/'reports/forecasts/latest/forecast.json'
    if seven_path.exists():
        seven_payload=json.loads(seven_path.read_text())
        uniform=seven_payload.get('seven_metrics',[])
        if seven_payload.get('asof')==str(end.date()) and {x['symbol'] for x in uniform}==set(watchlist.symbol):
            from ..seven_metrics import export as export_seven
            html+=export_seven(uniform,folder)
            write_json(folder/'seven_metrics.json',uniform)
        else:
            html+='<p>本次七项指标尚未更新或观察池不匹配，请先运行预测；不展示旧分数为本次结果。</p>'
    html += ''.join(research_html)
    if warnings:
        html += '<section><h2>需要处理的问题</h2><ul>' + warning_html + '</ul></section>'
    html += '<section><h2>方法与边界</h2><p>' + escape(DISCLAIMER) + '</p>'
    html += (f'<p>单边佣金 {cfg["commission_bps"]} bps；单边滑点 {cfg["slippage_bps"]} bps；'
             f'卖出税费 {cfg["sell_tax_bps"]} bps。费用是研究假设，非历史费率重建。'
             '周末和休市日使用最近已完成交易日，18:00北京时间前不纳入当日K线。'
             '日历核验不等于官方交易所确认。</p></section></main></html>')
    atomic_text(folder / 'index.html', html)
    # Self-contained latest view; archives retain previous membership/config/data versions.
    latest = root / 'reports' / 'latest'
    latest.mkdir(parents=True, exist_ok=True)
    for name in ('index.html', 'summary.json', 'pool.csv', 'candidates.csv'):
        atomic_text(latest / name, (folder / name).read_text(encoding='utf-8'))
    for name in ('seven_metrics.csv','seven_metrics.json'):
        if (folder/name).exists(): atomic_text(latest/name,(folder/name).read_text())
        elif (latest/name).exists(): (latest/name).unlink()
    write_json(root / 'reports' / 'latest_run.json', {'folder': str(folder),
               'generated_at': payload['generated_at'], 'failed_count': len(failures),
               'pool_shortfall_min': shortfall, 'mode': mode})
    return folder
