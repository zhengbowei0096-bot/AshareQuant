"""Fixed-specification research forecasts and a cash-account paper allocation.
No brokerage connection. Validation is conditional on today's selected universe.
"""
from pathlib import Path
from html import escape
import hashlib
import json
import math
import sqlite3
import numpy as np
import pandas as pd

from .core import (read_snapshot, read_watchlist, load_config, cutoff, quality,
                   stock_config, now, write_json, write_csv, atomic_text)
from .data import calendar, quotes

DEFAULTS = dict(capital=150000., equity=150000., risk_review_equity=120000.,
                max_stock_weight=.15, max_sector_weight=.25, max_invested_weight=.60,
                max_positions=5, lot_size=100, commission_bps=3., minimum_commission=5.,
                transfer_bps=.1, slippage_bps=5., sell_tax_bps=5.,
                ridge_alpha=10., training_cap=1260, minimum_train=504,
                validation_days=252, refit_days=63, minimum_mae_improvement=.02,
                minimum_direction_accuracy=.52, minimum_independent_observations=12,
                minimum_up_probability=.55,
                sector_groups={'001309':'半导体','603986':'半导体','002371':'半导体',
                               '600875':'电力设备','600406':'电力设备'})
FEATURES = ['r1','r5','r20','r60','vol20','vol60','range20','volume_ratio','trend20']
MODEL_VERSION = 'ridge-v1-fixed-no-search'


def features_and_labels(hfq, cal, horizon):
    """Reindex to real market sessions. No synthetic prices through halts."""
    f = hfq.set_index('date').reindex(pd.DatetimeIndex(cal))
    c = f.close
    daily = c.pct_change(fill_method=None)
    x = pd.DataFrame(index=f.index)
    for n in (1,5,20,60):
        x[f'r{n}'] = c/c.shift(n)-1
    x['vol20'] = daily.rolling(20).std()
    x['vol60'] = daily.rolling(60).std()
    x['range20'] = ((f.high-f.low)/c).rolling(20).mean()
    x['volume_ratio'] = f.volume/f.volume.rolling(20).mean()-1
    x['trend20'] = c/c.rolling(20).mean()-1
    x['target'] = c.shift(-horizon)/c-1
    x['label_end'] = pd.Series(f.index,index=f.index).shift(-horizon)
    return x.replace([np.inf,-np.inf],np.nan)


def fit_predict(train, test, alpha, feature_columns=None):
    columns = feature_columns if feature_columns is not None else FEATURES
    a = train[columns].to_numpy(float)
    b = test[columns].to_numpy(float)
    mu, sd = a.mean(axis=0), a.std(axis=0)
    sd[sd < 1e-10] = 1.
    a, b = (a-mu)/sd, (b-mu)/sd
    y = train.target.to_numpy(float)
    intercept = y.mean()
    coef = np.linalg.solve(a.T@a + alpha*np.eye(a.shape[1]), a.T@(y-intercept))
    return intercept+b@coef, dict(means=mu.tolist(), scales=sd.tolist(),
                                 coefficients=coef.tolist(), intercept=float(intercept))


def independent_rows(frame):
    chosen = []
    last_end = pd.Timestamp.min
    for i,row in frame.iterrows():
        if pd.Timestamp(row['date']) >= last_end:
            chosen.append(i)
            last_end = pd.Timestamp(row['label_end'])
    return frame.loc[chosen]


def validate_forecast(frame, horizon, cfg, feature_columns=None, model_name="ridge"):
    columns = feature_columns if feature_columns is not None else FEATURES
    clean = frame.dropna(subset=columns+['target','label_end'])
    validation = clean.tail(cfg['validation_days'])
    if len(validation) < cfg['validation_days']:
        raise ValueError('验证样本不足')
    samples, audits = [], []
    for offset in range(0,len(validation),cfg['refit_days']):
        test = validation.iloc[offset:offset+cfg['refit_days']]
        embargo=int(cfg.get('embargo_sessions',horizon))
        if embargo<0: raise ValueError('embargo不能为负数')
        boundary=frame.index.get_loc(test.index.min())
        allowed=frame.index[max(0,boundary-embargo)]
        train = clean[(clean.label_end < test.index.min()) & (clean.index < allowed)]
        if cfg.get('validation_window','rolling')=='rolling': train=train.tail(cfg['training_cap'])
        elif cfg.get('validation_window')!='expanding': raise ValueError('未知验证窗口')
        if len(train) < cfg['minimum_train']:
            raise ValueError('隔离重叠标签后训练样本不足')
        if model_name=='ridge':
            pred, _ = fit_predict(train,test,cfg['ridge_alpha'],feature_columns=columns)
        else:
            from .model_candidates import fit
            pred, _ = fit(model_name,train,test)
        audits.append(dict(test_start=str(test.index.min().date()),
                           test_end=str(test.index.max().date()),
                           training_last_label_end=str(train.label_end.max().date()),
                           training_rows=len(train),embargo_sessions=embargo,window=cfg.get('validation_window','rolling')))
        for j, (day,row) in enumerate(test.iterrows()):
            samples.append(dict(date=day,label_end=row.label_end, actual=float(row.target),
                                predicted=float(pred[j]), baseline_mean=float(train.target.mean()),
                                baseline_zero=0.))
    out = pd.DataFrame(samples)
    actual, predicted = out.actual.to_numpy(), out.predicted.to_numpy()
    mae = float(np.mean(abs(actual-predicted)))
    mean_mae = float(np.mean(abs(actual-out.baseline_mean.to_numpy())))
    zero_mae = float(np.mean(abs(actual)))
    independent = independent_rows(out)
    hit = float(((independent.actual>0)==(independent.predicted>0)).mean())
    baseline_hit = float(((independent.actual>0)==(independent.baseline_mean>0)).mean())
    n = len(independent)
    z = 1.96
    center = (hit+z*z/(2*n))/(1+z*z/n)
    half = z*math.sqrt(hit*(1-hit)/n+z*z/(4*n*n))/(1+z*z/n)
    improvement = 1-mae/max(min(mean_mae,zero_mae),1e-12)
    # A preliminary screen, not a statistical proof or a portfolio backtest.
    passed = (n >= cfg['minimum_independent_observations']
              and improvement >= cfg['minimum_mae_improvement']
              and hit >= max(cfg['minimum_direction_accuracy'],baseline_hit))
    metrics = dict(mae=mae,rmse=float(np.sqrt(np.mean((actual-predicted)**2))),
                   bias=float(np.mean(predicted-actual)), baseline_mean_mae=mean_mae,
                   baseline_zero_mae=zero_mae, mae_improvement=improvement,
                   observations=len(out), nonoverlapping_observations=n,
                   direction_accuracy_nonoverlap=hit, baseline_direction_accuracy_nonoverlap=baseline_hit,
                   direction_accuracy_95pct_wilson=[max(0.,center-half),min(1.,center+half)],
                   preliminary_screen_passed=bool(passed), folds=audits)
    return out, metrics


def forecast_one(snap, cal, end, horizon, cfg, model_name="ridge"):
    start = pd.Timestamp(end)-pd.DateOffset(years=5)
    hfq = snap['hfq']
    hfq = hfq[hfq.date.between(start,end)]
    days = pd.DatetimeIndex(cal)
    days = days[(days>=max(start,hfq.date.min())) & (days<=end)]
    frame = features_and_labels(hfq,days,horizon)
    cols=FEATURES
    if model_name!='ridge':
        from .model_candidates import add_lags,columns,fit
        frame=add_lags(frame);cols=columns(model_name)
    validation_frame=frame if model_name=='ridge' else frame.dropna(subset=FEATURES+columns('ar5')+['target','label_end'])
    out, metrics = validate_forecast(validation_frame,horizon,cfg,cols,model_name=model_name)
    train = validation_frame.dropna(subset=cols+['target','label_end'])
    train = train[train.label_end <= days.max()].tail(cfg['training_cap'])
    test = frame.iloc[[-1]]
    if test[cols if model_name=='ridge' else FEATURES+columns('ar5')].isna().any().any():
        raise ValueError('最新特征缺失，拒绝预测')
    pred, parameters = fit_predict(train,test,cfg['ridge_alpha']) if model_name=='ridge' else fit(model_name,train,test)
    p = float(pred[0])
    errors = independent_rows(out)
    residual = (errors.actual-errors.predicted).to_numpy()
    low, high = np.quantile(residual,[.1,.9])
    p_up = float((1+np.sum(residual > -p))/(len(residual)+2))
    vol = float(test.vol20.iloc[0]*np.sqrt(horizon))
    # Conservative daily-normal expected shortfall proxy; not a tail guarantee.
    es = float(min(0.,p-2.063*vol))
    future = pd.DatetimeIndex(cal)
    future = future[future>days.max()]
    target_day = str(future[horizon-1].date()) if len(future)>=horizon else None
    from .seven_metrics import bootstrap_uncertainty
    uncertainty=bootstrap_uncertainty(train,test,horizon,cfg['ridge_alpha'],model_name)
    return dict(model=model_name,horizon=horizon, model_uncertainty=uncertainty, predicted_return=p, up_probability_empirical=p_up,
                probability_method='Smoothed empirical nonoverlapping out-of-sample residual frequency; not a calibrated classifier',
                interval80_empirical=[float(p+low),float(p+high)], interval_calibration_samples=len(residual),
                volatility_horizon=vol, downside_es95_normal_proxy=es,
                validation=metrics, forecast_target_date=target_day,
                target_date_note='Calendar unavailable beyond cached range' if target_day is None else '',
                final_training_last_label_end=str(train.label_end.max().date()),
                model_parameters=parameters), out


def entry_cost(notional,cfg):
    return max(cfg['minimum_commission'],notional*cfg['commission_bps']/10000)+notional*(cfg['transfer_bps']+cfg['slippage_bps'])/10000


def validate_config(cfg):
    if not all(np.isfinite(v) for k,v in cfg.items() if isinstance(v,(int,float))):
        raise ValueError('参数不能含非有限数值')
    if cfg['ridge_alpha'] <= 0 or cfg['minimum_train'] < 60 or cfg['training_cap'] < cfg['minimum_train'] or cfg['refit_days'] <= 0 or cfg['validation_days'] < 60:
        raise ValueError('模型训练与验证参数无效')
    if cfg['capital']<=0 or cfg['equity']<0:
        raise ValueError('资金与权益参数无效')
    if not (0<cfg['max_stock_weight']<=cfg['max_invested_weight']<=1 and 0<cfg['max_sector_weight']<=1):
        raise ValueError('仓位上限无效')
    if cfg['lot_size']!=100 or cfg['max_positions']<1:
        raise ValueError('主板整手或持仓数参数无效')
    for key in ['commission_bps','minimum_commission','transfer_bps','slippage_bps','sell_tax_bps']:
        if cfg[key]<0: raise ValueError('费用不能为负')


def allocate(rows,cfg):
    validate_config(cfg)
    capital = cfg['capital']
    if cfg['equity'] <= cfg['risk_review_equity']:
        for row in rows:
            row['signal']='风险审查，暂停新增仓位'
            row['allocation_blockers']=['账户权益达到风险审查阈值']
        return dict(status='risk_review_pause',cash=capital,invested=0.,estimated_entry_cost=0.,positions=[])
    candidates = []
    for row in rows:
        f5,f20 = row['forecasts']['5'],row['forecasts']['20']
        reason = []
        if not row['data_eligible']: reason.append('数据或名称核验未通过')
        if not f5['validation']['preliminary_screen_passed']: reason.append('T+5未通过样本外基准筛选')
        if not f20['validation']['preliminary_screen_passed']: reason.append('T+20未通过样本外基准筛选')
        # Conservative cost floor at the maximum stock ticket, plus minimum fees.
        ticket = capital*cfg['max_stock_weight']
        sell_cost = max(cfg['minimum_commission'],ticket*cfg['commission_bps']/10000)+ticket*(cfg['transfer_bps']+cfg['slippage_bps']+cfg['sell_tax_bps'])/10000
        cost_fraction = (entry_cost(ticket,cfg)+sell_cost)/ticket
        if f5['predicted_return'] <= cost_fraction: reason.append('T+5预测未覆盖往返估算成本')
        if f20['predicted_return'] <= cost_fraction: reason.append('T+20预测未覆盖往返估算成本')
        probabilities=[f.get('up_probability_calibrated') if f.get('probability_calibration',{}).get('independent_validated') else f.get('up_probability_empirical') for f in (f5,f20)]
        if any(p is None for p in probabilities) or min(probabilities) < cfg['minimum_up_probability']:
            reason.append('经验上涨概率不足')
        row['allocation_blockers'] = reason
        row['signal'] = '模拟买入候选' if not reason else '观察，暂不买入'
        if not reason:
            candidates.append(row)
    candidates.sort(key=lambda r:r['forecasts']['20']['predicted_return']/max(r['forecasts']['20']['volatility_horizon'],1e-8),reverse=True)
    positions=[]
    cash=capital
    invested=costs=0.
    sectors={}
    for row in candidates:
        if len(positions)>=cfg['max_positions']: break
        sector=cfg['sector_groups'].get(row['symbol'],row['sector'])
        budget=min(capital*cfg['max_stock_weight'],capital*cfg['max_sector_weight']-sectors.get(sector,0.),capital*cfg['max_invested_weight']-invested)
        price=row['close']
        shares=max(0,int(budget/(price*cfg['lot_size']))*cfg['lot_size'])
        while shares and (shares*price+entry_cost(shares*price,cfg)>cash-capital*(1-cfg['max_invested_weight'])):
            shares-=cfg['lot_size']
        if not shares:
            row['signal']='观察，整手或预算不足'
            row['allocation_blockers'].append('100股整手或剩余行业/资金预算不足')
            continue
        value=shares*price
        fee=entry_cost(value,cfg)
        sell_fee=max(cfg['minimum_commission'],value*cfg['commission_bps']/10000)+value*(cfg['transfer_bps']+cfg['slippage_bps']+cfg['sell_tax_bps'])/10000
        if value*min(row['forecasts']['5']['predicted_return'],row['forecasts']['20']['predicted_return']) <= fee+sell_fee:
            row['signal']='观察，实际整手成本未覆盖'
            row['allocation_blockers'].append('实际整手往返估算成本高于预测收益')
            continue
        positions.append(dict(symbol=row['symbol'],name=row['name'],sector=sector,shares=shares,
                              reference_price=price,reference_notional=round(value,2),weight=value/capital,
                              estimated_entry_cost=round(fee,2),action='仅模拟计划，非已成交'))
        cash-=value+fee; invested+=value; costs+=fee; sectors[sector]=sectors.get(sector,0.)+value
    return dict(status='paper_plan' if positions else 'cash_no_validated_edge',
                cash=round(cash,2),invested=round(invested,2),estimated_entry_cost=round(costs,2),positions=positions)


def account_snapshot_plan(root,rows):
    path=Path(root)/'data/decision_account.json'
    if not path.exists():return None,None
    account=json.loads(path.read_text());cash=float(account['cash']);prices={r['symbol']:r for r in rows}
    if cash<0:raise ValueError('账户现金无效')
    holdings=[]
    for symbol,position in account.get('positions',{}).items():
        quote=prices.get(symbol);price=float(quote['close'] if quote else position['reference_price'])
        shares=int(position['shares'])
        if shares<0 or price<=0:raise ValueError('账户持仓无效')
        holdings.append(dict(symbol=symbol,name=position.get('name',symbol),shares=shares,reference_price=price,reference_notional=round(shares*price,2),price_basis='行情收盘价' if quote else '账户截图参考价'))
    invested=sum(x['reference_notional'] for x in holdings)
    plan=dict(status='existing_account_use_decision_report',cash=cash,available_cash=float(account['available_cash']),invested=round(invested,2),equity=round(cash+invested,2),estimated_entry_cost=0.,positions=[],current_holdings=holdings,note='账户快照及参考估值，不是新增买卖计划；调整方案见交易决策报告')
    return account,plan

def generate(root):
    factor_path=root/'reports/factor_validation_latest.json'
    factor_validation=json.loads(factor_path.read_text()) if factor_path.exists() else None
    base = load_config(root)
    p = root/'forecast_config.json'
    cfg = {**DEFAULTS,**(json.loads(p.read_text()) if p.exists() else {})}
    validate_config(cfg)
    watch = read_watchlist(root)
    end = cutoff()
    cal, cal_meta = calendar(root,base,end,offline=True)
    latest=pd.DatetimeIndex(cal)[pd.DatetimeIndex(cal)<=end].max()
    quote_map=quotes(root,base,watch.symbol.tolist(),end,cal,offline=True)
    from .lifecycle import refresh_registry,load_prediction,record_live
    production_registry=refresh_registry(root)
    rows,errors,versions,oos = [],[],{},[]
    for item in watch.itertuples():
        try:
            snap=read_snapshot(root,item.symbol)
            if snap is None: raise ValueError('没有数据快照')
            q=quality(snap['raw'],cal,stock_config(base,item.symbol),end)
            if not q['fresh'] or not q['calendar_complete']:
                raise ValueError('行情过期或日历不完整')
            live=quote_map[item.symbol]
            eligible=(live['name']==item.name and not any(t in live['name'].upper() for t in ['ST','退'])
                      and float(snap['raw'].volume.iloc[-1])>0
                      and snap['raw'].amount.tail(20).mean()>=base['min_average_amount'])
            forecasts={}
            for horizon in (1,5,20):
                input_frame=features_and_labels(snap['hfq'],cal[cal<=latest],horizon)
                formal=load_prediction(root,item.symbol,horizon,input_frame.iloc[[-1]],production_registry)
                if formal is not None:
                    future=cal[cal>latest]
                    if len(future)<horizon: raise ValueError('正式模型预测目标交易日不可核验')
                    vol=float(input_frame.vol20.iloc[-1]*np.sqrt(horizon))
                    formal.update(horizon=horizon,volatility_horizon=vol,downside_es95_normal_proxy=float(min(0.,formal['predicted_return']-2.063*vol)),
                        forecast_target_date=str(future[horizon-1].date()),model_uncertainty={'status':'FROZEN_REGISTERED_MODEL','std':None})
                    record_live(root,item.symbol,horizon,str(latest.date()),formal['forecast_target_date'],formal,snap['meta']['version'],snap['meta']['sha256'])
                    forecasts[str(horizon)]=formal
                else:
                    from .model_candidates import selected
                    chosen=selected(root,item.symbol,horizon,input_version=snap['meta']['version'],asof=str(latest.date()))
                    forecasts[str(horizon)], frame=forecast_one(snap,cal,end,horizon,cfg,model_name=chosen)
                    frame['symbol']=item.symbol;frame['horizon']=horizon;oos.append(frame)
            rows.append(dict(symbol=item.symbol,name=item.name,sector=getattr(item,'sector','未分类'),
                             close=float(snap['raw'].close.iloc[-1]),last_date=str(latest.date()),
                             data_eligible=bool(eligible),forecasts=forecasts))
            versions[item.symbol]=snap['meta']['version']
        except Exception as exc:
            errors.append(dict(symbol=item.symbol,error=str(exc)))
    from .strict_validation import formal_gate
    qualification=formal_gate(root)
    for row in rows:
        for f in row['forecasts'].values():
            f.setdefault('model_status','RESEARCH_ONLY')
            f['formal_qualification']=qualification
    plan=allocate(rows,cfg)
    # Historical error screens alone never authorize an allocation.
    if qualification['status']!='VALIDATED':
        plan=dict(status='formal_validation_blocked',cash=cfg['capital'],invested=0.,estimated_entry_cost=0.,positions=[])
        for row in rows:
            row['signal']='NO_TRADE：正式验证未通过'
            row.setdefault('allocation_blockers',[]).extend(qualification['blockers'])
    if qualification['status']=='VALIDATED':
        plan=dict(status='USE_VALIDATED_DECISION_ENGINE',cash=cfg['capital'],invested=0.,estimated_entry_cost=0.,positions=[])
        for row in rows:row['signal']='已加载正式模型；目标组合由已验证决策引擎计算'
    if errors:
        plan=dict(status='incomplete_data_pause',cash=cfg['capital'],invested=0.,estimated_entry_cost=0.,positions=[])
        for row in rows:
            row['signal']='观察，全池数据不完整，暂停配置'
    account,account_plan=account_snapshot_plan(root,rows)
    if account_plan is not None:plan=account_plan
    stamp=now().strftime('%Y%m%d-%H%M%S-%f')
    folder=root/'reports'/'forecasts'/stamp
    folder.mkdir(parents=True)
    next_days=pd.DatetimeIndex(cal)[pd.DatetimeIndex(cal)>latest]
    payload=dict(model_qualification=qualification,mode=('VALIDATED_MODELS_WITH_PLAN_GATE' if production_registry['models'] else 'RESEARCH_ONLY'),generated_at=now().isoformat(),asof=str(latest.date()),start=str((end-pd.DateOffset(years=5)).date()),
                 next_session=str(next_days[0].date()) if len(next_days) else None,
                 model=(MODEL_VERSION if all(f['model']=='ridge' for r in rows for f in r['forecasts'].values()) else 'validated-champion-challenger'),model_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 account_snapshot=account,account_assumption=('读取用户已导入的账户快照；时间未完全核实，非实时账户' if account is not None else '初始150000元全现金假设；仅模拟'),
                 factor_validation=factor_validation,active_factor_groups=[],config=cfg,data_versions=versions,calendar=cal_meta,forecasts=rows,errors=errors,plan=plan,
                 limitations=['回报标签为后复权收盘至未来收盘的研究代理；下一日开盘入场收益不同。',
                              '参数预先固定；滚动验证隔离尚未到期标签，但当前名单存在幸存者/选择偏差。',
                              '没有历史动态股票池、完整交易组合回测或多重检验校正，不构成已证实盈利策略。',
                              '80%区间与经验上涨概率来自少量非重叠样本残差，未完成独立概率校准。',
                              '收盘价仅为预算参考价；涨跌停、停复牌和券商实际费用必须下单前另核对。',
                              '权益120000元阈值仅根据输入权益审查；未连接真实账户。'])
    from .strict_validation import metrics as strict_metrics
    payload['strict_metrics']={}
    if oos:
        all_records=pd.concat(oos,ignore_index=True)
        for horizon,records in all_records.groupby('horizon'):
            payload['strict_metrics'][str(horizon)]=strict_metrics(records)
    # Diagnostic calibration is displayed, not admitted to production.
    from .probability_calibration import calibrate,predict
    if oos:
        diagnostic=pd.concat(oos,ignore_index=True)
        for h in (1,5,20):
            report=calibrate(diagnostic[diagnostic.horizon==h],payload['asof'])
            for row in rows:
                f=row['forecasts'][str(h)]
                if f.get('model_status')=='VALIDATED_REGISTERED':continue
                f['probability_calibration']=report
                if report.get('model'):f['up_probability_calibrated']=float(predict(report['model'],f['predicted_return']))
    from .seven_metrics import enrich,export
    unified=enrich(root,payload,watch)
    seven_html=export(unified,folder)
    from .prediction_quality import archive as archive_quality, report as report_quality
    archive_quality(root,payload)
    report_quality(root)
    write_json(folder/'forecast.json',payload)
    write_csv(folder/'validation_predictions.csv',pd.concat(oos,ignore_index=True) if oos else pd.DataFrame(columns=['date','label_end','actual','predicted','symbol','horizon']))
    flat=[]
    for row in rows:
        for h,f in row['forecasts'].items():
            flat.append(dict(symbol=row['symbol'],name=row['name'],horizon=int(h),predicted_return=f['predicted_return'],
                             probability_up_empirical=f.get('up_probability_empirical'),research_up_probability=f.get('up_probability_calibrated',f.get('up_probability_empirical')),probability_validated=bool(f.get('probability_calibration',{}).get('independent_validated')),interval_low=f['interval80_empirical'][0],
                             interval_high=f['interval80_empirical'][1],mae=f['validation']['mae'],
                             baseline_best_mae=min(f['validation'].get('baseline_mean_mae',f['validation'].get('baseline_mae',0.)),f['validation'].get('baseline_zero_mae',f['validation'].get('baseline_mae',0.))),
                             validation_passed=f['validation']['preliminary_screen_passed'],signal=row['signal'],
                             blockers='；'.join(row['allocation_blockers'])))
    write_csv(folder/'predictions.csv',pd.DataFrame(flat))
    write_csv(folder/'allocation.csv',pd.DataFrame(plan['positions'],columns=['symbol','name','sector','shares','reference_price','reference_notional','weight','estimated_entry_cost','action']))
    table=[]
    for row in sorted(rows,key=lambda r:r['forecasts']['20']['predicted_return'],reverse=True):
        vals=[row['symbol'],row['name'],f"{row['close']:.2f}"]
        for h in ('1','5','20'):
            f=row['forecasts'][h]
            from .probability_display import fields as probability_fields,text as probability_text
            vals.append(f"{f['predicted_return']:.2%} / "+probability_text(probability_fields(f)))
        f=row['forecasts']['20']
        vals.extend([f"{f['interval80_empirical'][0]:.2%} 至 {f['interval80_empirical'][1]:.2%}",
                     f"{f['validation']['mae_improvement']:.1%}",row['signal'],'；'.join(row['allocation_blockers'])])
        table.append('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in vals)+'</tr>')
    ptable=''.join('<tr>'+''.join('<td>'+escape(str(pos[k]))+'</td>' for k in ['symbol','name','shares','reference_price','reference_notional','estimated_entry_cost'])+'</tr>' for pos in plan['positions'])
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>15万元预测与模拟配置</title><style>body{font:16px/1.7 system-ui;background:#f4f6fa;color:#17253a;margin:0}main{max-width:1400px;margin:auto;padding:32px}section{background:white;padding:24px;margin:20px 0;border-radius:12px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left;min-width:80px}h1{font-size:28px}.amount{font-size:24px;color:#08675e}.note{color:#754509}</style><main>'''
    html+='<p class="note">研究模式 RESEARCH_ONLY：无正式合格模型，不生成可执行交易。研究概率显示估计值和验证原因；未验证估计不能作为正式交易依据。</p>'
    html+=f'<h1>15万元 · 多期限预测与模拟配置</h1><p>数据截至 {escape(payload["asof"])}；五年窗口自 {escape(payload["start"])}。已核验 {len(rows)} 只，失败 {len(errors)} 只。</p>'
    if account is not None:
        htable=''.join('<tr>'+''.join('<td>'+escape(str(h[k]))+'</td>' for k in ['symbol','name','shares','reference_price','reference_notional'])+'</tr>' for h in plan['current_holdings'])
        html+=f'<section><h2>实际账户快照</h2><p class="amount">股票参考市值 {plan["invested"]:,.2f} 元 · 可用现金 {plan["available_cash"]:,.2f} 元 · 参考权益 {plan["equity"]:,.2f} 元</p><p>已读取持仓，不再按15万元全现金配置。快照日期 {escape(str(account.get("snapshot_date",account.get("asof"))))}；具体时间未核实，不能视为实时账户。下表为现有持仓，不是买入订单。</p><table><tr><th>代码</th><th>名称</th><th>持股数</th><th>参考价</th><th>参考市值</th></tr>{htable}</table><p><a href="../../decisions/latest/index.html">查看按实际持仓生成的决策报告</a> · <a href="../../risk_models/latest/index.html">查看GARCH/HMM验证</a></p></section>'
    else:
        html+=f'<section><h2>本次资金计划</h2><p class="amount">股票预算 {plan["invested"]:,.2f} 元 · 预计入场成本 {plan["estimated_entry_cost"]:,.2f} 元 · 保留现金 {plan["cash"]:,.2f} 元</p>'
        html+='<p>初始资金全部现金。若无股票同时通过T+5和T+20的预设验证与成本门槛，本次维持现金。最多5只，单股15%、行业25%、总股票60%；这些是可调整研究假设。</p>'
        html+='<p class="note">这是下一交易日及以后的模拟预算。参考价为最新收盘价，不是可成交买入价；须重新核对实时价格、可交易状态及涨跌停。新买入股票不安排当日卖出。输入权益达到或低于120,000元时暂停新增配置。</p>'
        html+='<table><tr><th>代码</th><th>名称</th><th>股数</th><th>参考价</th><th>金额</th><th>入场成本</th></tr>'+ptable+'</table></section>'
    html+='<p><a href="../../prediction_quality/latest/index.html">每日预测质量报告</a></p>'
    html+='<p><a href="../../model_monitor/latest/index.html">预测误差、持续退化与回退记录</a></p>'
    if factor_validation:
        html+='<section><h2>新增数据验证结果</h2><p>行业、估值、财务和宏观数据已完成时间对齐审计。现有历史可见版本不足，没有新增因子通过严格准入，当前仍使用原9项行情特征。历史对照中的改善不视为模型升级依据。</p><p><a href="../../factor_research/latest/index.html">查看逐类样本外对照与限制</a></p></section>'
    if (root/'reports/model_comparison/latest/index.html').exists():
        html+='<section><h2>新增预测模型</h2><p>AR(5)、Lasso、LightGBM和XGBoost已进入固定参数的样本外对照与前瞻跟踪。只有同时通过历史与前瞻门槛的模型才参与正式预测；各期限实际模型见forecast.json。</p><p><a href="../../model_comparison/latest/index.html">查看模型对照、准入状态及候选预测</a></p></section>'
    html+=seven_html
    html+='<section><h2>预测结果</h2><p>各期限单元格：研究预期收益 / 概率校准状态。T+1/T+5/T+20均指市场交易日。80%区间不是保证；误差改进为相对更强简单基准的MAE降幅，负值表示更差。</p><div class="scroll"><table><tr><th>代码</th><th>名称</th><th>收盘价</th><th>T+1</th><th>T+5</th><th>T+20</th><th>T+20经验80%区间</th><th>T+20误差改进</th><th>信号</th><th>未入选原因</th></tr>'+''.join(table)+'</table></div></section>'
    html+='<section><h2>验证方法与局限</h2><p>默认岭回归使用9项当时已知行情特征；若有严格验证合格的候选则按股票和期限选择，实际模型见forecast.json；最近252个可到期样本分4块顺序验证。每块只训练标签在该块开始前已完全到期的记录，训练标准化参数也只从训练集估计。比较零收益与历史均值；无超参数搜索。方向与区间使用非重叠样本，样本数较少。筛选通过只代表初步研究门槛，不代表交易净收益已经过回测证明。</p><ul>'+''.join('<li>'+escape(x)+'</li>' for x in payload['limitations'])+'</ul><p>账户状态以本页快照为准；佣金最低5元、佣金3bps、滑点5bps等为估算，非用户真实券商报价。完整参数、模型系数及各折训练截止日期见forecast.json。</p></section></main></html>'
    atomic_text(folder/'index.html',html)
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS forecast_runs (run_id TEXT PRIMARY KEY, asof TEXT, model TEXT, payload TEXT)')
        db.execute('INSERT INTO forecast_runs VALUES (?,?,?,?)',(stamp,payload['asof'],payload['model'],json.dumps(payload,ensure_ascii=False,allow_nan=False)))
    latest_folder=root/'reports'/'forecasts'/'latest'
    latest_folder.mkdir(exist_ok=True)
    for name in ['index.html','forecast.json','predictions.csv','allocation.csv','seven_metrics.csv']:
        atomic_text(latest_folder/name,(folder/name).read_text(encoding='utf-8'))
    write_json(root/'reports'/'forecast_latest.json',dict(folder=str(folder),asof=payload['asof'],errors=errors,plan=plan))
    return payload,folder
