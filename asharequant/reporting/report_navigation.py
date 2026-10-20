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
