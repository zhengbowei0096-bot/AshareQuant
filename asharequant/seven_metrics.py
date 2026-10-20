"""Seven research outputs. Scores never authorize trading or promote models."""
import json, math, hashlib
from pathlib import Path
from html import escape
import numpy as np
import pandas as pd

WEIGHTS={'历史流动性':.10,'成交金额':.10,'市值':.05,'波动率':.10,'价格趋势':.10,'财务质量':.20,'历史风险':.10,'行业分散程度':.10,'股票交易状态':.15}
VERSION='nine-factor-research-v1'

def finite(v):
    try:return v is not None and np.isfinite(float(v))
    except (ValueError,TypeError):return False

def clip(x):return float(np.clip(x,0,100))

def bootstrap_uncertainty(train,test,horizon,alpha,model_name='ridge',members=24):
    """Chronological contiguous-window sensitivity of refitted conditional means.
    This is parameter/sample sensitivity, NOT a predictive return interval.
    """
    from .forecast import fit_predict
    from .model_candidates import fit
    block=max(5,int(horizon));n=len(train)
    if n<max(60,4*block) or members<8:
        return dict(status='INSUFFICIENT_TRAINING',std=None,mean_prediction_interval80=None,members=0)
    # Legacy API name retained; contiguous chronological subsamples avoid
    # reordering time-series training rows, including in uncertainty diagnostics.
    width=max(60,int(n*.8));starts=np.linspace(0,n-width,members,dtype=int);preds=[]
    for start in starts:
        sample=train.iloc[start:start+width]
        pred,_=fit_predict(sample,test,alpha) if model_name=='ridge' else fit(model_name,sample,test)
        if not finite(pred[0]):raise ValueError('bootstrap预测非有限')
        preds.append(float(pred[0]))
    return dict(status='RESEARCH_ESTIMATE',std=float(np.std(preds,ddof=1)),
                mean_prediction_interval80=np.quantile(preds,[.1,.9]).tolist(),members=members,block_length=block,
                method='chronological_contiguous_window_sensitivity',seed=None,
                training_rows=n,training_last_label_end=str(train.label_end.max().date()),
                note='同一模型连续时间子窗口重训的均值预测离散度；不是收益波动/预测区间，也不覆盖模型结构错误和制度变化；未独立校准')

def combine(parts,weights):
    if set(weights)!=set(WEIGHTS) or any(not finite(v) or v<=0 for v in weights.values()) or not np.isclose(sum(weights.values()),1):
        raise ValueError('九因子权重须为正且合计1')
    missing=[k for k in weights if parts[k]['score'] is None]
    coverage=sum(weights[k] for k in weights if k not in missing)
    return dict(score=round(sum(parts[k]['score']*weights[k] for k in weights),2) if not missing else None,
                coverage=round(coverage,6),missing=missing,status='COMPLETE_RESEARCH_SCORE' if not missing else 'INCOMPLETE_NO_RANK',
                components=parts,weights=weights,version=VERSION,validated=False,
                note='预设研究权重，未证明预测能力；不用于自动交易。历史流动性与成交额存在相关性；不同观察池的分散项不可直接横比')

def financial_score(row,sector):
    keys=['ROEJQ','PARENTNETPROFITTZ']
    financial=sector in ('银行','证券')
    if not financial:keys+=['NCO_NETPROFIT','ZCFZL']
    if any(not finite(row.get(k)) for k in keys):return None
    scores=[clip(float(row['ROEJQ'])/15*100),clip(50+float(row['PARENTNETPROFITTZ']))]
    if not financial:scores += [clip(float(row['NCO_NETPROFIT'])*70),clip(100-float(row['ZCFZL']))]
    return float(np.mean(scores))

def enrich(root,payload,watch):
    from .core import read_snapshot
    from .factor_research import Store,latest_period
    root=Path(root);point=pd.Timestamp(payload['generated_at']);asof=pd.Timestamp(payload['asof']).normalize()
    config_path=root/'seven_metrics_config.json';config=json.loads(config_path.read_text()) if config_path.exists() else {}
    weights=config.get('weights',WEIGHTS);groups=payload['config'].get('sector_groups',{})
    quotes=pd.read_csv(root/'data/quotes.csv',dtype={'symbol':str}).set_index('symbol')
    sp=root/'data/connections/suspensions/latest.json';susp=json.loads(sp.read_text()) if sp.exists() else {}
    store=Store(root);hist={};raws={};errs={}
    for item in watch.itertuples():
        try:
            snap=read_snapshot(root,item.symbol)
            if snap is None:raise ValueError('没有快照')
            raw=snap['raw'];hfq=snap['hfq'];raw=raw[raw.date<=asof];hfq=hfq[hfq.date<=asof]
            if len(raw)<252 or len(hfq)<252 or pd.Timestamp(raw.date.max()).date()!=asof.date():raise ValueError('历史不足或行情过期')
            raws[item.symbol]=raw;hist[item.symbol]=hfq.set_index('date').close
        except Exception as exc:errs[item.symbol]=str(exc)
    rets=pd.concat({s:c.pct_change(fill_method=None) for s,c in hist.items()},axis=1).tail(120) if hist else pd.DataFrame()
    forecasts={r['symbol']:r for r in payload['forecasts']};output=[]
    group_counts={}
    for item in watch.itertuples():
        g=groups.get(item.symbol,item.sector);group_counts[g]=group_counts.get(g,0)+1
    for item in watch.itertuples():
        s=item.symbol;parts={k:dict(score=None,raw=None,source=None,asof=None,reason='缺少可验证输入') for k in weights}
        def setpart(k,score,raw,source,date,reason=''):
            parts[k]=dict(score=score,raw=raw,source=source,asof=str(date),reason=reason)
        if s in hist:
            raw=raws[s];c=hist[s];r=c.pct_change(fill_method=None).dropna();turn=raw.turnover.tail(60);amt=raw.amount.tail(60)
            src='已校验Tencent行情快照';dt=payload['asof']
            if np.isfinite(turn).all() and (turn>=0).all():
                med=float(turn.median());positive=float((raw.volume.tail(60)>0).mean())
                setpart('历史流动性',clip(100*positive*min(1,med/.01)),dict(median_turnover60=med,positive_volume_fraction60=positive),src,dt)
            if np.isfinite(amt).all() and (amt>=0).all():
                a=float(amt.mean());setpart('成交金额',clip(50+25*np.log10(max(a,1)/1e8)),dict(mean_amount60_cny=a),src,dt)
            v=float(r.tail(60).std()*np.sqrt(252));momentum=float(c.iloc[-1]/c.iloc[-61]-1)
            setpart('波动率',clip(100*(.6-v)/.5),dict(annualized60=v),'后复权收益',dt)
            setpart('价格趋势',clip(50+100*momentum),dict(return60=momentum,above_ma60=bool(c.iloc[-1]>c.tail(60).mean())),'后复权收益',dt)
            dd=float((c.tail(252)/c.tail(252).cummax()-1).min());setpart('历史风险',clip(100*(1-abs(dd)/.5)),dict(max_drawdown252=dd),'后复权收益；非全部治理风险审查',dt)
            others=rets.drop(columns=[s]).mean(axis=1);pair=pd.concat([rets[s],others],axis=1).dropna()
            if len(pair)>=60 and pair.iloc[:,0].std()>0 and pair.iloc[:,1].std()>0:
                corr=float(pair.iloc[:,0].corr(pair.iloc[:,1]));g=groups.get(s,item.sector);concentration=group_counts[g]/len(watch)
                setpart('行业分散程度',clip(.6*50*(1-corr)+.4*100*(1-concentration)),dict(correlation_other_equalweight120=corr,sector=g,sector_fraction=concentration,sessions=len(pair)),'同日观察池后复权收益/配置行业分组',dt,'衡量观察池互补性，不代替实际持仓优化')
            if s in quotes.index:
                q=quotes.loc[s];ts=pd.to_datetime(str(q.quote_time),format='%Y%m%d%H%M%S',errors='coerce')
                if pd.notna(ts) and ts.date()==asof.date() and q['name']==item.name:
                    cap=q.get('market_cap_100m')
                    if finite(cap) and float(cap)>0:setpart('市值',clip(50+25*np.log10(float(cap)/100)),dict(total_market_cap_cny=float(cap)*1e8),'Tencent data/quotes.csv',ts)
                    matched=[x for x in susp.get('items',[]) if str(x.get('SECURITY_CODE'))==s]
                    valid_susp=susp.get('complete') is True and susp.get('date')==dt and susp.get('source') and susp.get('fetched_at') and pd.Timestamp(susp['fetched_at'])<=point
                    if valid_susp:
                        good=not matched and not any(t in item.name.upper() for t in ('ST','退')) and raw.volume.iloc[-1]>0
                        setpart('股票交易状态',70. if good else 0.,dict(close_session_evidence=bool(good),live_executable=False,suspension_records=matched),'Tencent + '+susp['source'],dt,'盘后状态仅70分；下一交易日须重新核验涨跌停/停牌和盘口')
        try:
            event=store.visible('financial_main',s,point)
            fin=latest_period(event[2],point,270) if event else None
            if fin is not None:
                score=financial_score(fin,item.sector)
                setpart('财务质量',score,{k:fin.get(k) for k in ['ROEJQ','PARENTNETPROFITTZ','NCO_NETPROFIT','ZCFZL','NOTICE_DATE','ORG_TYPE']},'Eastmoney financial_main; hash='+event[1],fin['_observation_date'],
                        '银行/证券仅采用ROE和利润增长；其他行业增加现金利润比与负债率；报告期ROE未年化，跨行业可比性有限')
        except Exception as exc:parts['财务质量']['reason']=str(exc)
        composite=combine(parts,weights)
        row=forecasts.get(s,{});row['composite_score']=composite
        for h in (1,5,20):
            f=row.get('forecasts',{}).get(str(h),{})
            output.append(dict(symbol=s,name=item.name,horizon=h,asof=payload['asof'],expected_return=f.get('predicted_return'),
                up_probability=f.get('up_probability_calibrated') if f.get('probability_calibration',{}).get('independent_validated') else None,probability_basis=f.get('probability_method'),probability_calibrated=False,
                expected_volatility=f.get('volatility_horizon'),downside_risk=f.get('downside_es95_normal_proxy'),
                prediction_interval80=f.get('interval80_empirical'),model_uncertainty=f.get('model_uncertainty',{'status':'UNAVAILABLE','std':None}),
                composite_score=composite,forecast_status='AVAILABLE_RESEARCH_ONLY' if f else 'MISSING',
                forecast_validation_passed=f.get('validation',{}).get('preliminary_screen_passed',False),input_error=errs.get(s),executable=False))
    eligible=sorted([r for r in output if r['horizon']==20 and r['composite_score']['score'] is not None],key=lambda r:(-r['composite_score']['score'],r['symbol']))
    ranks={r['symbol']:i+1 for i,r in enumerate(eligible)}
    for r in output:r['composite_rank']=ranks.get(r['symbol'])
    payload['seven_metrics']=output;payload['seven_metrics_policy']=dict(version=VERSION,weights=weights,ranked_symbols=len(ranks),score_validated=False,knowledge_at=payload['generated_at'],score_horizon='九因子分数为当期截面分数，三个预测期限共享；非收益概率')
    return output

def export(rows,folder):
    def pct(x):return '缺失' if x is None else f'{x:.2%}'
    table=[];flat=[]
    for r in sorted(rows,key=lambda x:(x['horizon'],x['composite_rank'] or 999,x['symbol'])):
        score=r['composite_score'];u=r['model_uncertainty'];interval=r['prediction_interval80'];details='；'.join(k+':'+('缺失' if p['score'] is None else f'{p["score"]:.1f}') for k,p in score['components'].items())
        vals=[r['composite_rank'] or '—',r['symbol'],r['name'],r['horizon'],pct(r['expected_return']),pct(r['up_probability']),pct(r['expected_volatility']),pct(r['downside_risk']),
              '缺失' if interval is None else ' 至 '.join(pct(v) for v in interval),pct(u.get('std')),score['score'] if score['score'] is not None else '缺项不排名',f'{score["coverage"]:.0%}',details]
        table.append('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in vals)+'</tr>')
        flat.append({k:v for k,v in r.items() if k not in ('composite_score','model_uncertainty','prediction_interval80')}|dict(score=score['score'],score_coverage=score['coverage'],score_missing=';'.join(score['missing']),model_uncertainty_std=u.get('std'),model_uncertainty_method=u.get('method'),interval80_low=interval[0] if interval else None,interval80_high=interval[1] if interval else None,components_json=json.dumps(score['components'],ensure_ascii=False)))
    pd.DataFrame(flat).to_csv(Path(folder)/'seven_metrics.csv',index=False)
    report = '<section><h2>统一七项指标 · 九因子综合排名</h2><p>按预测期限1/5/20交易日展示；波动率为对应期限估计。下行风险为正态近似95%预期损失；80%收益区间为经验残差区间。模型不确定性单列同模型连续时间子窗口24次重训的均值预测标准差（并非预测区间）；尚未独立校准。上涨概率仅在独立校准通过时展示，否则为空。综合分0–100，预设九因子研究权重，缺项不排名；分数和排名不能启用真实交易。</p><p><a href="seven_metrics.csv">下载统一七项CSV</a>；原始因子、来源、日期、重采样参数及分数明细见forecast.json。评分权重：流动性10%、成交额10%、市值5%、波动10%、趋势10%、财务20%、历史风险10%、行业分散10%、交易状态15%。</p><div class="scroll"><table><tr>'+''.join('<th>'+s+'</th>' for s in ['综合排名','代码','名称','期限','预期收益','上涨概率估计','预期波动率','下行风险ES95','收益区间80%','模型不确定性SD','综合分','因子覆盖','九因子分项'])+'</tr>'+''.join(table)+'</table></div></section>'

    return compact_metrics_html(report)


COMPACT_STYLE = """<style id="aq-compact-metrics">table.aq-metrics{width:max-content;min-width:100%;border-collapse:collapse;font-size:13px}table.aq-metrics td,table.aq-metrics th{height:auto;min-height:0;padding:6px 8px!important;vertical-align:top!important;white-space:nowrap!important;line-height:1.4!important;text-align:left}table.aq-metrics summary{cursor:pointer;white-space:nowrap}table.aq-metrics details .factor-details{white-space:normal!important;width:260px;line-height:1.5;padding-top:6px}table.aq-metrics details{margin:0}table.aq-metrics th{font-weight:600}</style>"""

def compact_metrics_html(page):
    """Presentation only: keep every value, fold verbose factor explanations."""
    import re
    marker='统一七项指标 · 九因子综合排名'
    if marker not in page:return page
    pos=page.index(marker);start=page.index('<table',pos);end=page.index('</table>',start)+len('</table>')
    table=page[start:end]
    if 'class="aq-metrics"' not in table:
        table=table.replace('<table','<table class="aq-metrics"',1)
    def fold(match):
        row=match.group(0);cells=list(re.finditer(r'<td(?:\s[^>]*)?>[\s\S]*?</td>',row))
        if len(cells)!=13:return row
        last=cells[-1];content=re.sub(r'^<td(?:\s[^>]*)?>|</td>$','',last.group(0))
        if '<details' in content:return row
        cell='<td><details><summary>查看9项因子</summary><div class="factor-details">'+content+'</div></details></td>'
        return row[:last.start()]+cell+row[last.end():]
    table=re.sub(r'<tr[ >][\s\S]*?</tr>',fold,table)
    page=page[:start]+table+page[end:]
    if 'id="aq-compact-metrics"' not in page:page=page.replace('<table class="aq-metrics"',COMPACT_STYLE+'<table class="aq-metrics"',1)
    return page
