"""Next-session intentions are separate from live execution eligibility."""
from html import escape
import pandas as pd

EXECUTION_ONLY = {'PRICE_NOT_CURRENT_LIVE','TRADING_STATUS_UNVERIFIED','SESSION_UNVERIFIED',
 'OUTSIDE_SUPPORTED_CONTINUOUS_SESSION','LIMIT_PRICES_MISSING','LIMIT_LOCK_OR_PRICE_INVALID',
 'LIMIT_REGIME_UNVERIFIED','T_PLUS_1_OR_SELLABLE_CAP'}
LABELS = {
 'FORMAL_METHOD_NOT_VALIDATED':'组合方法尚未通过正式验证',
 'ACCOUNT_STALE_OR_UNVERIFIED':'账户快照过期或未核实，不能确定个性化交易数量',
 'FORECAST_VALIDATION_FAILED':'收益预测未通过验证要求',
 'PROBABILITY_LOW':'上涨概率未达到门槛', 'RETURN_BELOW_COST':'预期收益未覆盖成本要求',
 'PROBABILITY_UNCALIBRATED':'上涨概率尚未独立校准',
 'PRICE_NOT_CURRENT_LIVE':'开盘后刷新实时价格，重算成本与股数',
 'TRADING_STATUS_UNVERIFIED':'开盘后核实停牌、风险标记及交易状态',
 'SESSION_UNVERIFIED':'执行时核实交易日及交易时段',
 'OUTSIDE_SUPPORTED_CONTINUOUS_SESSION':'仅在支持的连续交易时段复核执行，不影响收盘后制定计划',
 'LIMIT_PRICES_MISSING':'执行前取得当日涨跌停价格',
 'LIMIT_LOCK_OR_PRICE_INVALID':'核实涨跌停与盘口，不能假设能够成交',
 'LIMIT_REGIME_UNVERIFIED':'核实当日适用的涨跌幅制度',
 'T_PLUS_1_OR_SELLABLE_CAP':'按执行日账户核实T+1可卖数量',
 'CURRENT_HOLDING_ABOVE_STOCK_CAP':'参考持仓集中度超过单股上限，需复核',
 'NEXT_SESSION_UNVERIFIED':'无法确认下一交易日',
 'PLAN_SESSION_EXPIRED':'计划对应交易日已结束，需重新分析',
 'PROBABILITY_NOT_INDEPENDENTLY_VALIDATED':'上涨概率缺少独立校准证据',
 'TARGET_NOT_VALIDATED':'尚无经验证的目标配置',
 'PRICE_MISSING':'缺少有效参考价格','FORECAST_STALE':'预测数据已过期',
 'MARKET_ENVIRONMENT_UNKNOWN':'市场环境尚未确认',
 'BROKER_FEES_UNVERIFIED':'账户费用规则尚未核实',
 'SPECIAL_SESSION_OR_RISK_SECURITY':'特殊交易状态或风险标记尚未排除',
 'ACCOUNT_ASSUMED_INITIAL_CASH':'账户为初始资金假设，需核实真实账户',
}
ACTIONS={'BUY':'条件性买入','SELL':'条件性卖出','REDUCE':'条件性减仓','HOLD':'保持参考持仓','NO_TRADE':'暂不形成买卖建议（证据不足）'}

def annotate(result, next_session, calendar_verified=False):
    stamp=pd.Timestamp(result['generated_at']);local=stamp.tz_convert('Asia/Shanghai')
    flags=[]
    try:
        day=pd.Timestamp(next_session)
        if pd.isna(day) or str(day.date())!=next_session or not calendar_verified:raise ValueError()
        if day.date()<local.date() or (day.date()==local.date() and local.hour>=15):flags.append('PLAN_SESSION_EXPIRED')
    except (ValueError,TypeError):flags.append('NEXT_SESSION_UNVERIFIED')
    result.update(planning_mode='NEXT_SESSION_CONDITIONAL_PLAN',plan_session=next_session,plan_calendar_verified=calendar_verified)
    for d in result['decisions']:
        pending=[x for x in d['blockers'] if x in EXECUTION_ONLY]
        planning=[x for x in d['blockers'] if x not in EXECUTION_ONLY and x!='CURRENT_HOLDING_ABOVE_STOCK_CAP']+flags
        if not d.get('probability_calibrated'):planning.append('PROBABILITY_NOT_INDEPENDENTLY_VALIDATED')
        if not result.get('formal_method') or d.get('target_kind')!='RESEARCH_TARGET':planning.append('TARGET_NOT_VALIDATED')
        action='NO_TRADE';price=d.get('price')
        if not planning:
            if not isinstance(price,(int,float)) or not 0<price<float('inf'):planning.append('PRICE_MISSING')
            else:
                # Direction only. Actual lot sizes, T+1, cost and cash are recomputed at execution.
                delta=d['target_amount']-d['current_shares']*price
                action='HOLD' if abs(delta)<.01 else 'BUY' if delta>0 else 'SELL' if d['target_amount']==0 else 'REDUCE'
        d.update(next_session_action=action,planning_blockers=list(dict.fromkeys(planning)),
                 execution_checks=list(dict.fromkeys(pending+['刷新执行日账户、可用资金及T+1可卖数量','按实际价格、盘口、费用和资金约束重算整数股数；不保证成交'])),
                 risk_notes=[LABELS['CURRENT_HOLDING_ABOVE_STOCK_CAP']] if 'CURRENT_HOLDING_ABOVE_STOCK_CAP' in d['blockers'] else [],
                 next_session_suggested_shares=None,plan_executable=False)
    return result

def render(result):
    def text(flags):
        if not flags:return '无额外阻止条件'
        return '<details><summary>查看'+str(len(flags))+'项</summary><div>'+ '<br>'.join(escape(LABELS.get(x,x)) for x in flags)+'</div></details>'
    from ..probability_display import html_cell as probability_text
    cells=[]
    for d in result['decisions']:
        target=d.get('target_kind')=='RESEARCH_TARGET'
        vals=[escape(d['symbol']),escape(d['name']),str(d['price']),escape(str(d['price_source'])),escape(str(d['price_asof'])),
              '待补数据' if d['predicted_return'] is None else f"{d['predicted_return']:.2%}",
              probability_text(d),
              f"{d.get('current_weight',0):.2%}",f"{d.get('current_amount',d['current_shares']*(d['price'] or 0)):,.2f}",
              ACTIONS[d['next_session_action']],f"{d['target_weight']:.2%}" if target else '尚未确定',
              f"{d['target_amount']:,.2f}" if target else '尚未确定','执行时重算',
              text(d['planning_blockers']),text(d['execution_checks']),text(d['risk_notes']) if d['risk_notes'] else '—']
        cells.append('<tr>'+''.join('<td>'+x+'</td>' for x in vals)+'</tr>')
    headers=['代码','名称','参考价','来源','价格时间','预期收益','上涨概率','当前仓位','当前市值','次日建议','研究目标仓位','研究目标金额','建议股数','计划缺项','开盘核验','集中度提醒']
    style='<style>.next-session-scroll{overflow-x:auto;max-width:100%}.next-session-table{table-layout:auto!important;width:max-content!important;min-width:100%;height:auto!important;border-collapse:collapse}.next-session-table tr,.next-session-table th,.next-session-table td{height:auto!important;min-height:0!important;vertical-align:top!important;white-space:nowrap!important;padding:6px 9px!important;font-size:14px;line-height:1.4!important}.next-session-table details>div{white-space:normal!important;width:280px;max-height:240px;overflow:auto;padding-top:6px}.next-session-table summary{cursor:pointer}</style>'
    return style+'<h2>下一交易日：'+escape(str(result.get('plan_session') or '待核实'))+'</h2><p>收盘后制定计划；开盘后复核执行条件。闭市本身不阻止形成计划。当前仓位只是现状，绝非最优目标；尚无目标时保持空值。研究目标不是委托，未核实账户时不显示虚假的交易后余额或可执行股数。</p><div class="next-session-scroll"><table class="next-session-table"><tr>'+''.join('<th>'+h+'</th>' for h in headers)+'</tr>'+''.join(cells)+'</table></div>'
