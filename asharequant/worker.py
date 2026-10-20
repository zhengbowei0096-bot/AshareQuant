"""Disposable network worker; the parent enforces a total wall-clock timeout."""
import json
from pathlib import Path
import sys


def main():
    import akshare as ak
    import requests
    # Also bound undocumented internal requests that omit timeout.
    original = requests.sessions.Session.request

    def bounded(self, method, url, **kwargs):
        if kwargs.get('timeout') is None:
            kwargs['timeout'] = (8, 20)
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = bounded
    job = json.loads(Path(sys.argv[1]).read_text())
    if job['kind'] == 'quotes':
        import re
        import pandas as pd
        codes = [('sh' if s.startswith('6') else 'sz') + s for s in job['symbols']]
        response = requests.get('https://qt.gtimg.cn/q=' + ','.join(codes), timeout=(8, 20))
        response.raise_for_status()
        rows = []
        for code, content in re.findall(r'v_([a-z]{2}[0-9]{6})="([^"]*)";', response.text):
            fields = content.split('~')
            if len(fields) < 46 or fields[2] != code[2:]:
                continue
            rows.append({'symbol': fields[2], 'name': fields[1], 'price': fields[3],
                         'quote_time': fields[30], 'pe': fields[39],
                         'market_cap_100m': fields[45]})
        if len(rows) != len(codes):
            raise ValueError('实时名称快照缺少股票，拒绝视作风险核验完成')
        df = pd.DataFrame(rows)
    elif job['kind'] == 'index':
        df = ak.stock_zh_index_daily_tx(symbol=job['symbol'], start_date=job['start'].replace('-', ''), end_date=job['end'].replace('-', ''))
    elif job['kind'] == 'history_sse_delisted':
        df = ak.stock_info_sh_delist(symbol='沪市')
    elif job['kind'] == 'history_sz_delisted':
        df = ak.stock_info_sz_delist(symbol='终止上市公司')
    elif job['kind'] == 'calendar':
        df = ak.tool_trade_date_hist_sina()
    else:
        symbol = job['symbol']
        df = ak.stock_zh_a_hist_tx(
            symbol=('sh' if symbol.startswith('6') else 'sz') + symbol,
            start_date=job['start'].replace('-', ''),
            end_date=job['end'].replace('-', ''),
            adjust=job['adjust'], timeout=(8, 20),
        )
        from .core import adapt_tencent
        df = adapt_tencent(df, symbol)
    df.to_csv(job['output'], index=False)


if __name__ == '__main__':
    main()
