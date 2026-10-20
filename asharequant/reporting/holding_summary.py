"""Presentation-only summary of an existing frozen research plan. No model changes."""
from pathlib import Path
import html,json,re
from ..core import atomic_text
START='<!-- CURRENT_HOLDING_SUMMARY -->'
END='<!-- END_CURRENT_HOLDING_SUMMARY -->'

def render(r):
 plan=r.get('plan') or {};preview=r.get('reference_scenario') or {};e=html.escape
 orders={x['symbol']:x for x in preview.get('fills',[])};cfg=r.get('meta',{}).get('config',{})
 used=sum(x['shares']*x['reference_price'] for x in orders.values());remaining=r.get('equity',0)*cfg.get('max_turnover',0)-used
 lines=[]
 risk_order=plan.get('policy_version')=='individual-risk-budget-v2'
 for x in sorted(plan.get('rows',[]),key=lambda x:(-x.get('risk_priority',0),x['symbol']) if risk_order else (0,x['symbol'])):
  if not x.get('current_shares'):continue
  o=orders.get(x['symbol']);qty=o['shares'] if o else 0;side=o['side'] if o else None
  buy=qty if side=='BUY' else 0;sell=qty if side=='SELL' else 0
  reason=('减仓风险优先分 '+format(x.get('risk_priority',0),'.2f')+'；' if risk_order else '')+'；'.join(x.get('reasons',[]))
  if not qty and x['direction']!='HOLD':
   reason+='；本次暂不交易：'+(('按风险优先处理卖单后，剩余换手预算不足最低交易金额' if risk_order else '按代码顺序处理卖单后，剩余换手预算不足最低交易金额') if orders and remaining<cfg.get('minimum_trade_value',0) else '参考撮合未分配数量，须核验预算和成交条件')
  p=x.get('probability',{}).get('research_up_probability')
  probability='缺失' if p is None else f'{p:.2%}（研究估计）'
  values=[x['name']+' '+x['symbol'],x['current_shares'],buy,sell,x['current_shares']+buy-sell,reason,probability,f'{x["reference_price"]:.2f}元 / {x["price_date"]} / {x["price_source"]}']
  lines.append('<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in values)+'</tr>')
 if not lines:return ''
 title=e(str(plan.get('target_session','待确认')))
 ordering='减仓按风险评分降序处理，同分时才按代码稳定排序' if risk_order else '卖单采用代码顺序处理'
 return START+f'<section id="current-holdings" style="padding:14px;background:#eef7f7;border:1px solid #bedbdb;margin:12px 0"><h2>{title} · 当前持仓操作摘要</h2><p>依据已结算模拟持仓与已冻结研究计划；下面是本次参考股数，不是已成交记录。实际账户若已变动，须重新核对。</p><div class="scroll"><table><tr><th>股票</th><th>当前股数</th><th>参考买入</th><th>参考卖出</th><th>参考剩余</th><th>原因</th><th>'+str(cfg.get('horizon',20))+'交易日上涨概率</th><th>参考行情</th></tr>'+''.join(lines)+'</table></div>'+f'<p>每日换手上限 {cfg.get("max_turnover",0):.0%}；{ordering}，因而减仓分批进行。概率对应{cfg.get("horizon",20)}个交易日，不代表明日上涨概率。零目标权重是研究配置目标，不要求明天一次性卖完。</p><p>执行条件：下一交易日重新核验真实开盘价、停牌/涨跌停、可卖股数、费用和成交量；触及限制则减少数量或不成交。模拟价格含不利滑点，不保证真实成交。报告每天收盘更新，未授权真实自动下单。</p></section>'+END

def update(root,r=None):
 folder=Path(root)/'reports/research_paper/latest'
 if r is None:r=json.loads((folder/'report.json').read_text())
 p=folder/'index.html'
 if not p.exists():return
 text=p.read_text();text=re.sub(re.escape(START)+'.*?'+re.escape(END),'',text,flags=re.S)
 summary=render(r)
 if summary:text=text.replace('</h1>','</h1>'+summary,1)
 atomic_text(p,text)
 from ..report_navigation import prepare
 prepare(root)
