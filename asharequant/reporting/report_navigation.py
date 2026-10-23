"""Report navigation only; never modifies frozen model or account data."""
from pathlib import Path
import html,json,re
from ..core import atomic_text
MARK='<!-- CONTROL_REPORT_NOTICE -->'
END='<!-- END_CONTROL_REPORT_NOTICE -->'

def label_control(path,href=None):
 path=Path(path)
 if not path.exists():return False
 text=path.read_text();text=re.sub(re.escape(MARK)+'.*?'+re.escape(END),'',text,flags=re.S)
 text=re.sub(r'<title>.*?</title>','<title>原策略对照报告 · 非当前操作计划</title>',text,count=1,flags=re.S)
 text=re.sub(r'<h1>.*?</h1>','<h1>原策略对照报告 · 保留用于比较</h1>',text,count=1,flags=re.S)
 banner=MARK+'<section style="padding:16px;background:#fff1ce;border:2px solid #b66d00;margin:12px 0"><strong>对照用途：本页不是当前新模型的操作计划。</strong><p>本页保留旧策略的模拟结果及验证限制。“无合格计划”仅描述旧策略，不代表新模型研究模拟没有计划。请查看新模型报告的计划日期与持仓摘要。</p>'
 if href:banner+='<p><a style="font-size:20px;font-weight:bold" href="'+html.escape(href,quote=True)+'">打开当前新模型操作计划 →</a></p>'
 banner+='</section>'+END
 text=text.replace('</h1>','</h1>'+banner,1)
 atomic_text(path,text);return True

def prepare(root):
 root=Path(root);new=root/'reports/research_paper/latest/index.html'
 from ..active_research import enabled
 if enabled(root):
  label_control(new,'../../active_research/latest/index.html#current-holdings')
  label_control(root/'reports/paper_account/latest/index.html','../../active_research/latest/index.html#current-holdings')
  return
 label_control(root/'reports/paper_account/latest/index.html','../../research_paper/latest/index.html#current-holdings' if new.exists() else None)

def primary_section(root):
 from ..dynamic_service import enabled as dynamic_enabled,primary as dynamic_primary
 if dynamic_enabled(root):return dynamic_primary(root)
 from ..adaptive_intraday import enabled as adaptive_enabled,primary as adaptive_primary
 if adaptive_enabled(root):return adaptive_primary(root)
 from ..intraday import enabled as intraday_enabled,primary
 if intraday_enabled(root):return primary(root)
 from ..active_research import enabled
 if enabled(root):
  p=Path(root)/'reports/active_research/latest/report.json'
  if not p.exists():return '<section><h2>当前积极研究模拟尚未生成</h2><p>下方原研究策略仅供对照，不代替当前计划。</p></section>'
  r=json.loads(p.read_text())
  from ..holding_summary import render
  date=(r.get('plan') or {}).get('target_session','待确认')
  return '<section style="border:2px solid #087f8c"><h2>默认查看：当前日常研究模拟计划 · 22.5% / 45% / 45%</h2><p>计划交易日：'+html.escape(str(date))+'；状态：'+html.escape(r.get('status','未知'))+'。用户指定研究模拟方案，正式模型不变。</p><p><a href="详细报告/active_research/latest/index.html#current-holdings">打开当前计划与模拟收益</a> · <a href="详细报告/research_paper/latest/index.html">原研究策略对照</a></p>'+render(r)+'</section>'
 p=Path(root)/'reports/research_paper/latest/report.json'
 if not p.exists():return '<section><h2>当前新模型计划尚未生成</h2><p>请核对每日运行状态；下方旧策略报告仅用于对照。</p></section>'
 r=json.loads(p.read_text());date=(r.get('plan') or {}).get('target_session','待确认')
 from ..holding_summary import render
 return '<section style="border:2px solid #087f8c"><h2>默认查看：新模型操作计划</h2><p>计划交易日：'+html.escape(str(date))+'；报告状态：'+html.escape(r.get('status','未知'))+'。</p><p><a style="font-size:20px;font-weight:bold" href="详细报告/research_paper/latest/index.html#current-holdings">打开新模型计划、预测和模拟收益 →</a></p>'+render(r)+'</section>'

def entry_page(plan_url,overview_url,label):
 e=html.escape
 return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+e(label)+'</title><meta http-equiv="refresh" content="0;url='+e(plan_url,quote=True)+'"><h1>'+e(label)+'</h1><p><a href="'+e(plan_url,quote=True)+'">打开当前新模型操作计划</a></p><p><a href="'+e(overview_url,quote=True)+'">查看每日总览与对照报告</a></p></html>'


def overview_entry(url,label='完整每日总览'):
 e=html.escape
 return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+e(label)+'</title><meta http-equiv="refresh" content="0;url='+e(url,quote=True)+'"><h1>'+e(label)+'</h1><p><a href="'+e(url,quote=True)+'">打开完整总览：顶部操作计划与全部报告</a></p></html>'


def current_strategy(root):
 from ..dynamic_service import enabled as dynamic_enabled
 if dynamic_enabled(root):return 'dynamic'
 from ..adaptive_intraday import enabled as adaptive
 from ..intraday import enabled as intraday
 from ..active_research import enabled as active
 if adaptive(root):return 'adaptive'
 if intraday(root):return 'intraday'
 if active(root):return 'active_research'
 return 'research_paper'


def hidden_strategies(root):
 return {'paper_account','research_paper','active_research','intraday','adaptive','strategy_experiment','decisions'}-{current_strategy(root)}


def current_only(root,page):
 """Presentation only: remove obsolete strategy sections and links, not evidence."""
 hidden=hidden_strategies(root)
 headings=['原策略对照账户 · 非真实资金','历史策略回放（不是持续模拟账户）','正式模型计划限制（独立于研究模拟）']
 if 'research_paper' in hidden:headings.append('当前使用：研究模型模拟账户')
 for heading in headings:
  page=re.sub(r'<section[^>]*>\s*<h2>'+re.escape(heading)+r'</h2>.*?</section>','',page,flags=re.S)
 for name in hidden:
  # Match complete path segments, not prose or substrings of another strategy.
  pattern=r'<a\b[^>]*href=["\'][^"\']*(?:/|["\'])'+re.escape(name)+r'/[^"\']*["\'][^>]*>.*?</a>'
  page=re.sub(r'<li>(?:(?!</li>).)*'+pattern+r'(?:(?!</li>).)*</li>','',page,flags=re.S)
  page=re.sub(pattern,'',page,flags=re.S)
 page=re.sub(r'<h2>相同起点的收盘对照</h2><p>.*?</p>','',page,flags=re.S)
 page=page.replace('仅研究；原模型和原盘中策略保留对照。','仅研究模拟。')
 return page



def readable_record(value):
 """Human-readable audit tree; preserve source values without exposing JSON syntax."""
 names={'checked_at':'检查时间','mode':'运行模式','submitted_real_orders':'真实订单数','meta':'账户来源与版本','account':'模拟账户','nav':'每日净值','status':'状态','decision_rows':'逐股决策','last_decision_at':'最近决策时间','limitations':'使用限制','positions':'持仓','marks':'估值价格','cash':'现金','equity':'账户权益','valuation_at':'估值时间','valuation_status':'估值状态','orders':'委托','order_events':'委托状态变化','executions':'模拟成交','alerts':'事件记录','quote_seen':'已观察行情','cooldown':'交易冷却记录','realized_pnl':'已实现盈亏','unrealized_pnl':'未实现盈亏','unrealized_pnl_estimate':'未实现估值盈亏','total_fees':'累计费用','max_drawdown':'最大回撤','cumulative_return':'累计收益率','capital_utilization':'资金使用率','previous_close_equity':'上一收盘权益','peak':'历史最高权益','day':'交易日','closed_day':'已结算日','turnover':'换手金额','fees':'当日费用','shares':'股数','sellable':'可卖股数','basis':'阶段成本','at':'时间','symbol':'股票代码','name':'股票名称','side':'方向','price':'模拟成交价','source':'来源','quote_time':'报价时间','rationale':'决策依据','reason':'原因','filled':'已成交股数','quantity':'委托股数','limit_price':'委托限价','order_id':'委托编号','id':'记录编号','last_cycle_at':'最近运行时间','segment_initial_equity':'阶段起始权益','opening_basis':'起始成本','basis_kind':'成本口径','execution_started_at':'模拟开始时间','cash_after':'成交后现金','fee':'费用','assumption':'撮合假设'}
 values={'PAPER_ONLY':'仅模拟','BUY':'买入','SELL':'卖出','ACCEPTED':'待撮合模拟委托','FILLED':'全部模拟成交','PARTIALLY_FILLED':'部分模拟成交','CANCELED':'已撤单','REJECTED':'已拒单','MARKET_CLOSED':'非交易时段','SETTLED':'已结算'}
 if isinstance(value,dict):
  if not value:return '<p>暂无记录</p>'
  return '<table><tbody>'+''.join('<tr><th>'+html.escape(names.get(str(k),str(k)))+'</th><td>'+('<details><summary>展开明细（'+str(len(v))+'项）</summary>'+readable_record(v)+'</details>' if isinstance(v,(dict,list)) else readable_record(v))+'</td></tr>' for k,v in value.items())+'</tbody></table>'
 if isinstance(value,list):return '<ol>'+''.join('<li>'+readable_record(v)+'</li>' for v in value)+'</ol>' if value else '<p>暂无记录</p>'
 if value is None:return '未提供'
 return html.escape(values.get(str(value),str(value)))

def live_entry(root, destination):
 """Render the current report directly; keep downloads in the same folder tree."""
 root=Path(root);destination=Path(destination)
 source=root/'reports'/current_strategy(root)/'latest'
 target=source/'index.html'
 if not target.exists():
  return '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><h1>动态报告尚未生成</h1><p>请稍后刷新，或检查盯盘服务。</p>'
 page=target.read_text()
 import csv,io
 sections=[]
 labels={'FILLS.csv':'模拟成交明细','ORDERS.csv':'模拟委托明细','NAV.csv':'每日收益明细','report.json':'完整决策与时间证据'}
 headers={'at':'北京时间','created_at':'委托时间','symbol':'代码','name':'名称','side':'方向','shares':'股数','price':'模拟成交价','quantity':'委托股数','filled':'已成交股数','limit_price':'限价','status':'状态','fee':'费用','fees':'费用','cash':'现金','equity':'权益','date':'日期','daily_pnl':'日盈亏','daily_return':'日收益率（小数）','cumulative_return':'累计收益率（小数）','realized_pnl':'已实现盈亏','unrealized_pnl':'未实现盈亏','max_drawdown':'最大回撤（小数）'}
 for index,(name,label) in enumerate(labels.items()):
  anchor='detail-'+str(index)
  page=page.replace('href="'+name+'"','href="#'+anchor+'"')
  p=source/name
  if not p.exists():content='<p>当前尚无记录。</p>'
  elif name.endswith('.csv'):
   rows=list(csv.reader(io.StringIO(p.read_text(encoding='utf-8-sig'))))
   if not rows or not rows[0] or len(rows)<2:content='<p>当前尚无记录；没有记录不代表收益为零。</p>'
   else:
    content='<div class="scroll"><table><thead><tr>'+''.join('<th>'+html.escape(headers.get(x,x))+'</th>' for x in rows[0])+'</tr></thead><tbody>'
    content+=''.join('<tr>'+''.join('<td>'+html.escape(x)+'</td>' for x in row)+'</tr>' for row in rows[1:])+'</tbody></table></div>'
  else:
   content=readable_record(json.loads(p.read_text()))
  sections.append('<section id="'+anchor+'"><h2>'+label+'</h2>'+content+'</section>')
 block=''.join(sections)
 if '</body>' in page:page=page.replace('</body>',block+'</body>')
 elif '</html>' in page:page=page.replace('</html>',block+'</html>')
 else:page+=block
 from .plain_chinese import render
 return render(page)


def sync_live(root):
 root=Path(root)
 config=root/'report_delivery_config.json'
 cfg=json.loads(config.read_text()) if config.exists() else {}
 destination=Path(cfg.get('desktop_directory',str(root/'reports/desktop_delivery')))
 destination.mkdir(parents=True,exist_ok=True)
 atomic_text(destination/'打开动态研究模拟.html',live_entry(root,destination))
 # Daily bundles are translated at export; only the current live page changes every cycle.


if __name__ == '__main__':
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
 sync_live(parser.parse_args().root)
