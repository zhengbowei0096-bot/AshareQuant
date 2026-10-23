"""Dated desktop delivery and provenance-labelled performance, no trading authority."""
from pathlib import Path
import csv,hashlib,html,json,math,shutil,sqlite3
import pandas as pd
from ..core import now,write_json,read_watchlist,read_snapshot,atomic_text

REPORTS=[('实时行情驱动模拟委托与收益','dynamic/latest','index.html'),('动态盘中研究模拟主账本','adaptive/latest','index.html'),('原冻结预测盘中策略对照','intraday/latest','index.html'),('日线研究模拟对照（22.5% / 45% / 45%）','active_research/latest','index.html'),('风险预算同源策略对比（影子模拟）','strategy_experiment/latest','index.html'),('原研究策略对照计划与连续收益','research_paper/latest','index.html'),('模型准入进度与加速边界','validation_readiness/latest','index.html'),('独立风控模拟调仓方案','risk_control/latest','index.html'),('原策略对照账户与收益','paper_account/latest','index.html'),('预测与排名','forecasts/latest','index.html'),('交易计划','decisions/latest','index.html'),
 ('预测质量','prediction_quality/latest','index.html'),('模型监控','model_monitor/latest','index.html'),
 ('候选模型比较','model_comparison/latest','index.html'),('风险模型','risk_models/latest','index.html'),
 ('新增因子研究','factor_research/latest','index.html'),('股票池概览','latest','index.html')]
JSON_REPORTS=['iteration/latest.json','lifecycle/latest.json','connection_health/latest.json','public_history/latest.json',
 'decisions/execution_replay.json','strict_audit.json','quality.json','broker_records/latest.json']
TASKS=['核实交易日与重复运行','更新股票、指数和可用扩展数据','检查缺失、重复、异常和来源时间',
 '更新技术特征','检查正式模型准入并生成三期限预测','统一七项指标与九因素排名','比较四种组合并生成条件计划',
 '更新股票每日涨跌幅及跟踪累计收益','读取主账户已结算净值与每日收益（未结算明确说明）','检查模拟回放和模拟收益证据',
 '结算到期预测并生成质量报告','检查持续退化和分布漂移','按条件重训候选，推进冻结实验验收',
 '检查升级/回退条件并记录版本','执行严格审计和连接健康检查','生成可视化总览和收益账单','按北京时间日期归档并通知完成或失败']


def pct(x):return '待补数据' if x is None else f'{x:.2%}'
def safe_date(value):
    date=pd.Timestamp(value)
    if pd.isna(date) or str(date.date())!=value:raise ValueError('INVALID_SESSION_DATE')
    return value


def nav_returns(records,session,sessions):
    """Never infer no deposits from absent cash-flow fields. No return across gaps."""
    records=sorted([r for r in records if r['date']<=session],key=lambda r:r['date'])
    if len({r['date'] for r in records})!=len(records):raise ValueError('DUPLICATE_NAV_DATE')
    out=[];cumulative=1.;chain=True
    dates=list(map(str,sessions))
    for i,r in enumerate(records):
        nav=float(r['nav'])
        if not math.isfinite(nav) or nav<=0:raise ValueError('INVALID_NAV')
        daily=None;pnl=None;reason='BASELINE_ONLY'
        if i:
            prev=records[i-1];previous=dates[dates.index(r['date'])-1] if r['date'] in dates and dates.index(r['date'])>0 else None
            if previous!=prev['date']:reason='MISSING_SESSION_NAV';chain=False
            elif r.get('external_flow') is None:reason='CASH_FLOW_UNKNOWN';chain=False
            elif r['external_flow']!=0:reason='CASH_FLOW_TIMING_REQUIRES_TWR';chain=False
            else:
                pnl=nav-float(prev['nav']);daily=nav/float(prev['nav'])-1;reason='LOCAL_EXPORT_REFERENCE_ONLY'
                if chain:cumulative*=1+daily
        out.append(dict(date=r['date'],nav=nav,daily_pnl=pnl,daily_return=daily,cumulative_return=cumulative-1 if i and chain else None,
            basis_date=records[0]['date'],status=reason,verified=False,source=r.get('source_status','USER_SUPPLIED_REFERENCE')))
    return out


def account_returns(root,session):
    """Read verified frozen NAV records only; never create or modify a ledger."""
    date=safe_date(session)
    path=Path(root)/'data/dynamic/account.sqlite3'
    if not path.is_file():return []
    from ..paper_account import get
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        records=get(db,'nav')
    rows=[]
    for key,row in records.items():
        if safe_date(row['date'])!=key:raise ValueError('ACCOUNT_NAV_DATE_MISMATCH')
        if row['date']>date:continue
        if row.get('mode')!='PAPER_ONLY':raise ValueError('ACCOUNT_NAV_MODE_MISMATCH')
        if not math.isfinite(float(row['equity'])) or row['equity']<=0:raise ValueError('INVALID_NAV')
        for field in ('daily_pnl','daily_return','cumulative_return'):
            if not math.isfinite(float(row[field])):raise ValueError('INVALID_NAV_VALUE')
        rows.append(row)
    return sorted(rows,key=lambda r:r['date'])


def account_section(rows,session):
    """The sole account summary; dates distinguish settled history from today's P&L."""
    today=next((r for r in rows if r.get('date')==session),{})
    latest=max(rows,key=lambda r:r['date']) if rows else {}
    date=html.escape(latest.get('date','尚无结算'))
    status='已完成当日结算' if today else '当日尚未完成结算；历史记录见收益单'
    return ('<section id="account-income"><h2>主账户每日与累计收益</h2>'
        f'<p>报告日期：{html.escape(session)}；最近结算日：{date}；{status}。</p>'
        f'<p>当日收益率：{pct(today.get("daily_return"))}；累计阶段收益率（截至最近结算日）：{pct(latest.get("cumulative_return"))}。</p>'
        '<p>与盘中持仓、委托和成交使用同一套动态模拟账本，无需另行导入券商净值。累计收益从本阶段起点计算；模拟记录不代表券商确认的真实成交。</p>'
        '<p><a href="账户每日与累计收益.csv.view.html">查看主账户每日收益明细</a></p></section>')


def performance(root,session):
    root=Path(root);date=safe_date(session);stocks=[]
    calendar=root/'data/calendar.csv';sessions=[]
    if calendar.exists():sessions=pd.read_csv(calendar).iloc[:,0].astype(str).str[:10].tolist()
    prior=sessions[sessions.index(date)-1] if date in sessions and sessions.index(date)>0 else None
    anchor_path=root/'data/performance_tracking.json';anchor=json.loads(anchor_path.read_text()) if anchor_path.exists() else dict(basis_date=prior,created_at=now().isoformat(),purpose='Stock HFQ reference tracking, not account returns')
    # Initialize only from a complete same-session cohort; failed/preview runs do not move baseline.
    for item in read_watchlist(root).itertuples():
        try:
            snap=read_snapshot(root,item.symbol)
            if snap is None:raise ValueError('MISSING_SNAPSHOT')
            meta=snap['meta']
            if meta.get('synthetic') or meta.get('source')!='tencent' or pd.Timestamp(meta['fetched_at'])>pd.Timestamp(now()):raise ValueError('UNVERIFIED_SOURCE')
            f=snap['hfq'].set_index('date');f=f[f.index<=pd.Timestamp(date)]
            if f.empty:raise ValueError('NO_PRICE_BEFORE_SESSION')
            asof=str(f.index.max().date());daily=cumulative=None
            valid=asof==date and prior and pd.Timestamp(prior) in f.index
            if valid:daily=float(f.loc[pd.Timestamp(date),'close']/f.loc[pd.Timestamp(prior),'close']-1)
            base=anchor.get('basis_date')
            if asof==date and base and pd.Timestamp(base) in f.index:cumulative=float(f.loc[pd.Timestamp(date),'close']/f.loc[pd.Timestamp(base),'close']-1)
            stocks.append(dict(symbol=item.symbol,name=item.name,asof=asof,daily_return=daily,cumulative_return=cumulative,basis_date=base,
                source=meta.get('source'),fetched_at=meta.get('fetched_at'),input_version=meta.get('version'),status='REFERENCE_ONLY' if valid else 'STALE_OR_MISSING_PREVIOUS_CLOSE'))
        except Exception as exc:stocks.append(dict(symbol=item.symbol,name=item.name,asof=None,daily_return=None,cumulative_return=None,status=str(exc)))
    if not anchor_path.exists() and stocks and all(r['status']=='REFERENCE_ONLY' for r in stocks) and anchor.get('basis_date'):write_json(anchor_path,anchor)
    # One operational account: read the settled dynamic ledger, never broker imports.
    account=account_returns(root,date)
    current=next((r for r in account if r['date']==date),None)
    replay=root/'reports/decisions/execution_replay.json';simulation=json.loads(replay.read_text()) if replay.exists() else {}
    return dict(session=date,generated_at=now().isoformat(),stock_reference=stocks,account_ledger=account,
        account_today=current,account_status='已完成当日结算' if current else '当日尚未完成结算',
        account_source='data/dynamic/account.sqlite3',account_mode='PAPER_ONLY',
        simulation_status=simulation.get('status','NO_SIMULATION_EVIDENCE'),simulation_summary={k:v.get('summary') for k,v in simulation.get('results',{}).items()},
        simulation_note='历史回放，非持续模拟账户，更非真实收益；无完整执行证据不生成收益曲线',
        submitted_orders=0,notes=['本系统仅使用一套主账户，持仓、成交与收益均来自动态模拟账本。','每日收益读取已冻结的收盘结算；当日未结算时不把上一日收益当成今日收益。','累计收益以本阶段迁移起点为基准，不叠加旧策略收益。','这是模拟记账结果，不是券商确认的真实成交。'])


def retain_latest(root, day):
    """Keep the last complete desktop export by timestamp, backing up older copies."""
    from datetime import datetime
    from ..core import run_lock
    from ..report_navigation import overview_entry
    root, day = Path(root), Path(day)
    safe_date(day.name)
    with run_lock(root/'logs/report_retention'):
        candidates = []
        for folder in day.glob('运行-*'):
            if folder.is_symlink() or not folder.is_dir():
                continue
            manifest = json.loads((folder/'归档清单.json').read_text())
            created = datetime.fromisoformat(manifest['created_at'])
            if created.tzinfo is None or not (folder/'00_每日总览.html').is_file():
                raise ValueError('报告不完整或缺少时区，保留原文件以供检查')
            candidates.append((created, folder.name, folder))
        if not candidates:
            return dict(removed=0, kept=None)
        candidates.sort()
        keep = candidates[-1][2]
        older = [x[2] for x in candidates[:-1]]
        backup = None
        if older:
            backup = root/'backups/desktop_report_versions'/day.name/now().strftime('%Y%m%d-%H%M%S-%f')
            backup.mkdir(parents=True, exist_ok=False)
            write_json(backup/'恢复清单.json',dict(day=str(day),kept=keep.name,
                       moved=[x.name for x in older],reason='每日仅保留最后一次；备份不改变历史证据'))
            for folder in older:
                shutil.move(str(folder), str(backup/folder.name))
        atomic_text(day/'打开当天报告.html',overview_entry(keep.name+'/00_每日总览.html',day.name+' 完整每日总览'))
        return dict(removed=len(older),kept=str(keep),backup=str(backup) if backup else None)


def export(root,session,result,destination=None):
    root=Path(root);safe_date(session)
    config=root/'report_delivery_config.json';cfg=json.loads(config.read_text()) if config.exists() else {}
    destination=Path(destination or cfg.get('desktop_directory',str(root/'reports/desktop_delivery')))
    day=destination/session;stamp=now().strftime('%H%M%S-%f');folder=day/('运行-'+stamp);folder.mkdir(parents=True,exist_ok=False)
    perf=performance(root,session);write_json(folder/'收益账单.json',perf)
    for name,rows in [('股票每日与累计收益.csv',perf['stock_reference']),('账户每日与累计收益.csv',perf['account_ledger'])]:
        columns=sorted(set().union(*(r.keys() for r in rows))) if rows else ['date','nav','daily_pnl','daily_return','cumulative_return','status']
        with (folder/name).open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader();writer.writerows(rows)
    from ..report_navigation import prepare,primary_section,overview_entry,current_only,hidden_strategies
    prepare(root)
    # Mirror latest paths to retain report-to-report relative links. No historical tree copies.
    links=[];hashes={}
    for title,relative,entry in REPORTS:
        if relative.split("/")[0] in hidden_strategies(root) or relative=="decisions/latest":continue
        source=root/'reports'/relative
        if not source.exists():links.append(dict(title=title,status='MISSING',url=None));continue
        target=folder/'详细报告'/relative
        shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__'),dirs_exist_ok=True)
        links.append(dict(title=title,status='REFER_TO_SOURCE_DATE',url='详细报告/'+relative+'/'+entry))
    for relative in JSON_REPORTS:
        source=root/'reports'/relative
        if source.exists():
            target=folder/'详细报告'/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
            links.append(dict(title=relative,status='REFER_TO_SOURCE_DATE',url='详细报告/'+relative))
    logs=result.get('log_directory')
    if logs and Path(logs).is_dir():shutil.copytree(logs,folder/'运行日志',dirs_exist_ok=True)
    for p in folder.rglob('*.html'):
        p.write_text(current_only(root,p.read_text()))
    for p in folder.rglob('*'):
        if p.is_file():hashes[str(p.relative_to(folder))]=hashlib.sha256(p.read_bytes()).hexdigest()
    manifest=dict(session=session,created_at=now().isoformat(),pipeline=result,performance_status=perf['account_status'],files=hashes,links=links,
        warning='归档日期不代表所有附件数据均为当日；失败时附件可能是上次报告。总览收益严格核对日期。')
    write_json(folder/'归档清单.json',manifest)
    e=html.escape
    table=''.join('<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in [r['symbol'],r['name'],r['asof'] or '缺失',pct(r['daily_return']),pct(r['cumulative_return']),r.get('basis_date') or '待建立',r['status']])+'</tr>' for r in perf['stock_reference'])
    today=perf['account_today'] or {};total=pct(today.get('cumulative_return'));daily=pct(today.get('daily_return'))
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>每日量化总览</title><style>body{font:15px/1.5 system-ui;background:#f4f6fa;color:#17253a;margin:0}main{max-width:1300px;margin:auto;padding:20px}section{background:white;padding:16px;margin:12px 0;border-radius:10px}h1{font-size:25px}h2{font-size:19px;margin:0}td,th{padding:6px 9px;border-bottom:1px solid #ddd;text-align:left}table{border-collapse:collapse;width:100%}.scroll{overflow:auto}.bad{color:#9b3e18}a{color:#006b72}</style><main>'''
    mode_label='最近完整交易日验收（未占用今日18点任务）' if result.get('mode')=='ACCEPTANCE' else '每日运行'
    if result.get('mode')=='UI_REFRESH':mode_label='报告界面与主模拟入口更新（未重跑每日分析）'
    page+=f'<p>{mode_label}</p><h1>{e(session)} 每日量化报告</h1><p>北京时间生成：{e(now().isoformat())} · 流水线状态：{e(result.get("status","UNKNOWN"))}</p><p class="bad">归档日期不等于所有附件行情日期。失败或预览时附件可能是旧报告；请核对各页时间，不当作当天信号。</p>'
    page+=primary_section(root)
    page+=account_section(perf['account_ledger'],session)
    paper_path=root/'reports/paper_account/latest/report.json'
    paper=json.loads(paper_path.read_text()) if paper_path.exists() else {}
    paper_daily=paper.get('daily_return') if paper.get('last_valuation_date')==session else None
    page+=f'<section><h2>原策略对照账户 · 非真实资金</h2><p>状态：{e(str(paper.get("status","尚未建立")))}；估值日期：{e(str(paper.get("last_valuation_date") or "尚未首日结算"))}；日收益：{pct(paper_daily)}；累计收益：{pct(paper.get("cumulative_return"))}。</p><p><a href="详细报告/paper_account/latest/index.html">查看原策略对照记录（非当前计划）</a>。以授权的华福持仓截图与现金为起点；无合格计划则保留原模拟持仓与现金，不虚构成交。</p></section>'
    page+=f'<section><h2>历史策略回放（不是持续模拟账户）</h2><p>{e(perf["simulation_status"])}</p><p>{e(perf["simulation_note"])}</p></section>'
    def load_report(relative):
        path=root/'reports'/relative
        try:return json.loads(path.read_text()) if path.exists() else {}
        except (ValueError,OSError):return {}
    research=load_report('research_paper/latest/report.json')
    if research:
        research_current=research.get('valuation_date')==session
        page+=f'<section><h2>当前使用：研究模型模拟账户</h2><p>模型版本：{e(research.get("spec",{}).get("version","未知"))}；估值日期：{e(str(research.get("valuation_date")))}；状态：{e(research.get("status","未知"))}。日收益：{pct(research.get("daily_return") if research_current else None)}；从原始基准累计收益：{pct(research.get("cumulative_from_original") if research_current else None)}；新模型阶段收益：{pct(research.get("research_segment_return") if research_current else None)}。</p><p><a href="详细报告/research_paper/latest/index.html">查看当前模型的90组预测、次日模拟买卖建议和连续收益单</a>。未验证模型允许参与研究模拟，正式模型仍受原验证门槛约束。原策略账户仅作对照，不能将其“未通过”理解为研究模拟暂停。</p></section>'
    iteration=load_report('iteration/latest.json');quality=load_report('prediction_quality/latest/report.json');decision=load_report('decisions/latest/decision.json')
    studyrows=''.join('<tr><td>'+e(str(x.get('candidate',{}).get('alpha','—')))+'</td><td>'+str(len(x.get('candidate',{}).get('features',[])))+'</td><td>'+pct(x.get('mean_gain'))+'</td><td>'+('进入待测' if x.get('eligible_for_future_test') is True else '未达筛选门槛')+'</td></tr>' for x in iteration.get('studies',[]))
    page+='<section><h2>每日模型评估与迭代</h2><p>数据日：'+e(str(iteration.get('asof','缺失')))+'；到期确认预测：'+str(quality.get('confirmed','待核验'))+'；持续漂移：'+('已触发研究关注' if iteration.get('persistent_drift') else '未触发或待核验')+'。</p><p>本次研究原因：'+e(str(iteration.get('research_reason') or '未触发新一轮研究'))+'。候选状态：'+e(str((iteration.get('candidate') or {}).get('evaluation','没有新增合格候选')))+'</p><table><tr><th>候选参数</th><th>特征数</th><th>历史误差改善</th><th>状态</th></tr>'+studyrows+'</table><p>历史改善不等于独立验证通过，不强制每日升级。</p></section>'
    page+='<section><h2>正式模型计划限制（独立于研究模拟）</h2><p>计划来源数据日：'+e(str(decision.get('source_forecast_asof','缺失')))+'</p><ul>'+''.join('<li>'+e(str(x))+'</li>' for x in decision.get('blockers',['未取得决策报告']))+'</ul><p>用户手动交易。持仓快照未更新时，不据此计算真实收益或承诺可执行订单。</p></section>'
    page+='<section><h2>分类报告</h2><ul>'+''.join('<li>'+('<a href="'+e(x['url'])+'">'+e(x['title'])+'</a>（请看原报告日期）' if x['url'] else e(x['title'])+'：缺失')+'</li>' for x in links)+'</ul></section>'
    page+='<section><h2>30只股票每日涨跌与跟踪累计收益</h2><p>后复权收盘价格比值，仅为观察参考；不扣账户交易费用，不是个人账户收益。累计基准建立后固定，不随每天运行重置。</p><p><a href="股票每日与累计收益.csv">下载股票收益单</a></p><div class="scroll"><table><tr><th>代码</th><th>名称</th><th>实际数据日</th><th>日涨跌</th><th>跟踪累计</th><th>基准日</th><th>状态</th></tr>'+table+'</table></div></section>'
    page+='<section><h2>每日循环清单</h2><ol>'+''.join('<li>'+e(t)+'</li>' for t in TASKS)+'</ol><p>模型每日评估、按条件重训、验收通过才升级；每天不强制改参数或替换正式模型。源码不自动发布，真实订单不自动发送。</p></section></main></html>'
    (folder/'00_每日总览.html').write_text(current_only(root,page))
    day.mkdir(exist_ok=True);(day/'打开当天报告.html').write_text(overview_entry(folder.name+'/00_每日总览.html',session+' 完整每日总览'))
    from .report_navigation import live_entry
    destination.mkdir(exist_ok=True)
    atomic_text(destination/'打开动态研究模拟.html',live_entry(root,destination))
    destination.mkdir(exist_ok=True);(destination/'打开最新报告.html').write_text(overview_entry(session+'/'+folder.name+'/00_每日总览.html','最新完整每日总览'))
    atomic_text(root/'reports/dashboard/latest/index.html',overview_entry((folder/'00_每日总览.html').resolve().as_uri()))
    delivered=dict(directory=str(folder),overview=str(folder/'00_每日总览.html'),session=session,account_status=perf['account_status'])
    from ..work_archive import record
    import os
    delivered['work_archive']=record(root,delivered,result)
    (destination/'工作归档.html').write_text('<!doctype html><meta charset="utf-8"><a href="'+html.escape(os.path.relpath(delivered['work_archive']['index'],destination))+'">打开按北京时间日期整理的工作归档</a>')
    from .plain_chinese import localize,attachment
    atomic_text(folder/'账户每日与累计收益.csv.view.html',attachment(folder/'账户每日与累计收益.csv'))
    delivered['readability']=localize(folder)
    manifest['files']={str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file() and p.name!='归档清单.json' and p.name!='归档清单.json.view.html'}
    write_json(folder/'归档清单.json',manifest)
    if (folder/'归档清单.json.view.html').exists():atomic_text(folder/'归档清单.json.view.html',attachment(folder/'归档清单.json'))
    delivered['retention']=retain_latest(root,day)
    return delivered
