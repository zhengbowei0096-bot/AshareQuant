"""Prospective error monitoring and fail-closed model lifecycle controls."""
from pathlib import Path
import json, hashlib, sqlite3, html, os
import numpy as np
import pandas as pd
from .core import now

DEFAULTS = {'window_independent':12, 'consecutive_windows':2, 'mae_deterioration':0.20}

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def atomic(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
    os.replace(temp,path)

def evaluate(rows, risk, cfg):
    f=pd.DataFrame(rows)
    if f.empty:return {'status':'insufficient','samples':0}
    f=f.sort_values('origin')
    cols=['actual','prediction','baseline']
    if not np.isfinite(f[cols].to_numpy(float)).all() or (risk and ((f.prediction<=0)|(f.baseline<=0)|(f.actual<0)).any()):
        return {'status':'invalid','samples':len(f)}
    err=f.prediction-f.actual
    def summary(part):
        e=part.prediction-part.actual
        out={'samples':len(part),'mae':float(abs(e).mean()),'baseline_mae':float(abs(part.baseline-part.actual).mean()),'bias':float(e.mean()),'underestimate_rate':float((e<0).mean())}
        if risk:
            out['qlike_gain']=float((np.log(part.baseline)+part.actual/part.baseline-np.log(part.prediction)-part.actual/part.prediction).mean())
        else:out['direction_accuracy']=float(((part.prediction>0)==(part.actual>0)).mean())
        return out
    # Greedy non-overlapping label intervals. No repeated-run counters.
    chosen=[];last=None
    for i,r in f.iterrows():
        if last is None or r.origin>=last:chosen.append(i);last=r.target
    independent=f.loc[chosen];n=cfg['window_independent'];k=cfg['consecutive_windows']
    windows=[]
    if len(independent)>=n*k:
        tail=independent.tail(n*k)
        for j in range(k):
            w=summary(tail.iloc[j*n:(j+1)*n]);w['degraded']=bool(w['mae']>max(w['baseline_mae'],1e-12)*(1+cfg['mae_deterioration']) or (risk and w['qlike_gain']<0));windows.append(w)
    # Buckets are known at issuance (forecast sign / magnitude), not future regimes.
    groups={}
    for label,mask in [('预测上涨',f.prediction>0),('预测非上涨',f.prediction<=0)] if not risk else [('预测风险低于基准',f.prediction<f.baseline),('预测风险不低于基准',f.prediction>=f.baseline)]:
        if mask.any():groups[label]=summary(f[mask])
    if not risk:
        for label,mask in [('预测绝对收益不超过1%',f.prediction.abs()<=.01),('预测绝对收益超过1%',f.prediction.abs()>.01)]:
            if mask.any():groups[label]=summary(f[mask])
    return {'status':'degraded' if windows and all(w['degraded'] for w in windows) else 'healthy' if windows else 'insufficient', 'samples':len(f),'independent_samples':len(independent),'metrics':summary(f),'attribution':groups,'windows':windows,'last_target':str(f.target.max())}

def collect(root, asof, cfg):
    result={}
    db=sqlite3.connect(f'file:{root}/data/market.sqlite3?mode=ro',uri=True)
    with db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for family,table,sql in [
          ('return','model_shadow','SELECT spec,model,symbol,horizon,origin_date AS origin,target_date AS target,issued_at,predicted AS prediction,champion_predicted AS baseline,actual FROM model_shadow'),
          ('risk','risk_shadow','SELECT spec,kind AS model,symbol,horizon,origin,target,issued_at,candidate_variance AS prediction,baseline_variance AS baseline,actual FROM risk_shadow')]:
            if table not in tables:continue
            f=pd.read_sql_query(sql,db)
            if f.empty:continue
            for key,g in f.groupby(['spec','model','symbol','horizon']):
                spec,model,symbol,h=key;identity=f'{family}|{spec}|{model}|{symbol}|{h}'
                valid=g.actual.notna() & (g.target<=asof)
                # Exclude invalid issuance chronology and not-yet-closed targets.
                issued=pd.to_datetime(g.issued_at,utc=True)
                end=pd.to_datetime(g.target).dt.tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15)
                start=pd.to_datetime(g.origin).dt.tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15)
                valid &= (issued>=start)&(issued<end)&(end<=pd.Timestamp(now()))
                result[identity]=evaluate(g[valid].to_dict('records'),family=='risk',cfg) if valid.any() else {'status':'insufficient','samples':0}
                result[identity].update(family=family,spec=spec,model=model,symbol=symbol,horizon=int(h),pending=int((g.actual.isna()).sum()))
    db.close()
    return result

def entries(family, registry):
    if family=='return':
        return [(f"return|{registry['spec_id']}|{v['model']}|{key.replace('/', '|')}",v) for key,v in registry.get('active',{}).items()]
    return [(f"risk|{registry.get('spec_id','unknown')}|{v['kind']}|{v['symbol']}|{v['horizon']}",v) for v in registry.get('approved',[])]

def run(root):
    root=Path(root);config=root/'monitor_config.json'
    cfg={**DEFAULTS,**(json.loads(config.read_text()) if config.exists() else {})}
    if cfg['window_independent']<2 or cfg['consecutive_windows']<2 or cfg['mae_deterioration']<0:raise ValueError('无效监控参数')
    registries={}
    for family,file in [('return','model_registry.json'),('risk','risk_model_registry.json')]:
        p=root/'data'/file
        if p.exists():registries[family]=json.loads(p.read_text())
    asof=max([r.get('evaluated_asof',r.get('asof','')) for r in registries.values()]+[''])
    groups=collect(root,asof,cfg)
    path=root/'data/model_monitor.json';previous=json.loads(path.read_text()) if path.exists() else {}
    blocked=dict(previous.get('blocked',{}));events=list(previous.get('events',[]));active=[]
    for identity in previous.get('active',[]):
        metric=groups.get(identity,{})
        if metric.get('status') in ('degraded','invalid') and identity not in blocked:
            blocked[identity]={'at':now().isoformat(),'reason':'持续退化或数据异常','evidence':metric}
            events.append({'action':'rollback','identity':identity,**blocked[identity]})
    for family,r in registries.items():
        for identity,entry in entries(family,r):
            metric=groups.get(identity,{'status':'insufficient','samples':0})
            gates=entry.get('historical_passed') and entry.get('prospective_passed' if family=='return' else 'forward_passed')
            reason=None
            if not gates:reason='原准入验证未通过'
            elif metric['status'] in ('degraded','invalid'):reason='持续退化' if metric['status']=='degraded' else '评估数据异常'
            elif metric['status']=='insufficient':reason='独立监控样本不足，暂用基准'
            if reason:
                if metric['status'] in ('degraded','invalid') and identity not in blocked:
                    blocked[identity]={'at':now().isoformat(),'reason':reason,'evidence':metric}
                    events.append({'action':'rollback','identity':identity,**blocked[identity]})
                continue
            if identity not in blocked:active.append(identity)
    for identity in sorted(set(active)-set(previous.get('active',[]))):events.append({'at':now().isoformat(),'action':'activate','identity':identity})
    for identity in sorted(set(previous.get('active',[]))-set(active)):
        if not any(e.get('action')=='rollback' and e.get('identity')==identity for e in events[len(previous.get('events',[])):]):events.append({'at':now().isoformat(),'action':'fallback','identity':identity,'reason':'未获当前验证或监控许可'})
    state={'generated_at':now().isoformat(),'asof':asof,'config':cfg,'registry_hashes':{k:digest(v) for k,v in registries.items()},'active':active,'blocked':blocked,'events':events,'groups':groups}
    history=root/'data/model_lifecycle';history.mkdir(exist_ok=True)
    # Immutable input registry snapshots retain the preceding release for audit, never restore stale predictions.
    for family,r in registries.items():
        p=history/f'{family}-{digest(r)}.json'
        if not p.exists():atomic(p,r)
    atomic(path,state)
    report=root/'reports/model_monitor/latest';report.mkdir(parents=True,exist_ok=True)
    atomic(report/'report.json',state)
    names={}
    import csv
    if (root/'watchlist.csv').exists():names={r['symbol']:r['name'] for r in csv.DictReader((root/'watchlist.csv').open())}
    names['sh000300']='沪深300'
    labels={'insufficient':'待积累到期样本','healthy':'未触发持续退化','degraded':'持续退化','invalid':'数据异常'}
    rows=[]
    for identity,g in groups.items():
        m=g.get('metrics',{});values=[names.get(g['symbol'],g['symbol'])+'（'+g['symbol']+'）',g['model'],'T+'+str(g['horizon']),g['samples'],g.get('independent_samples',0),labels[g['status']],m.get('mae','—'),m.get('bias','—'),'已隔离' if identity in blocked else '监控中']
        rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>')
    page='<meta charset="utf-8"><title>预测误差与模型健康</title><style>body{font:16px/1.8 system-ui;margin:40px}td,th{padding:8px;border-bottom:1px solid #ddd}</style><h1>预测误差与模型健康</h1>'
    page+=f'<p>数据截至 {asof}；监控许可 {len(active)} 组；已隔离 {len(blocked)} 组。未到期预测不计入误差。</p><p>连续 {cfg["consecutive_windows"]} 段、每段 {cfg["window_independent"]} 个不重叠样本，MAE比同期基准恶化超过 {cfg["mae_deterioration"]:.0%}，或风险QLIKE持续更差，触发回退。参数是预设监控规则，不是已证实最优阈值。</p>'
    page+='<p>收益回退至岭回归；风险回退至历史波动率和趋势规则。保留旧注册版本供审计，不恢复过期预测。被隔离版本不自动重新启用。误差分组为统计描述，不是因果解释；尚无可靠的预测时点行业/市场状态快照归因。</p><table><tr>'+''.join('<th>'+x+'</th>' for x in ['标的','模型','期限','到期数','独立数','状态','MAE','偏差（预测−实际）','处理'])+'</tr>'+''.join(rows)+'</table>'
    page+='<h2>误差分组与回退证据</h2><p>JSON报告保存各股票、期限的高估/低估、方向正确率或QLIKE，以及按预测方向/风险相对基准分组的误差。</p><a href="report.json">查看完整指标及升级/回退日志</a>'
    for identity,g in groups.items():
        if g.get('attribution'):
            page+='<details><summary>'+html.escape(names.get(g['symbol'],g['symbol'])+' '+g['model']+' T+'+str(g['horizon']))+'</summary><pre>'+html.escape(json.dumps(g['attribution'],ensure_ascii=False,indent=2))+'</pre></details>'
    page+='<h2>升级与回退日志</h2><pre>'+html.escape(json.dumps(events,ensure_ascii=False,indent=2) if events else '尚无升级或回退事件')+'</pre>'
    (report/'index.html').write_text(page)
    return state

def permitted(root,family,registry,identity):
    try:
        s=json.loads((Path(root)/'data/model_monitor.json').read_text())
        return s['registry_hashes'].get(family)==digest(registry) and identity in s['active'] and identity not in s['blocked']
    except (OSError,ValueError,KeyError):return False
