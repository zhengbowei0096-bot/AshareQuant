"""Plain-language research progress, including explicit non-empty wait states."""
from html import escape as e
import pandas as pd


REASONS={
 'DAILY_MATURED_OUTCOMES':'今日已根据新到期预测与实际结果完成候选比较，没有额外天数冷却。',
 'ALREADY_RESEARCHED_SESSION':'这个数据日已完成候选比较，同日重试不重复调参；等待下一个完整交易日。',
 'FROZEN_VALIDATION_PENDING':'已有候选正在接受未来独立验证。每天继续核对预测与实际结果，验证结束前暂停新一轮调参。',
 'NO_NEW_MATURED_OUTCOMES':'暂无新增、已确认的首次日预测到期结果。已检查现有结果，等待新的实际收盘数据。',
 'WAIT_AFTER_CLOSE':'等待北京时间18:00之后的完整收盘数据。',
 'INPUT_ERRORS':'数据、误差确认或研究流程未通过检查，暂停新研究；修复后可重试，不属于固定天数冷却。',
}


def time_text(value):
    if not value:return '尚不能确定'
    # macOS strftime can return an empty string for non-ASCII format literals.
    return pd.Timestamp(value).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M:%S')+'（北京时间）'


def percent(value):return '无法评估' if value is None else f'{value:.2%}'


def render(root, report, quality, session):
    if report.get('version')=='slot-v2':return render_slots(report,quality,session)
    progress=report.get('research_progress',{})
    status=progress.get('status')
    last=progress.get('last_study')
    if not last and report.get('studies'):
        last={k:report.get(k) for k in ('asof','generated_at','studies')}
    # Compatibility for a report produced before the daily scheduler existed.
    if not last and report.get('generated_at'):
        from ..daily_iteration import previous_study
        last=previous_study(root,{},pd.Timestamp(report['generated_at']))
    why=REASONS.get(status,'这是旧版研究记录，尚未记录具体等待原因；请运行每日研究以生成完整进度。' if report else '尚无研究记录；每日流程取得可靠数据后开始评估。')
    candidate=report.get('candidate') or {}
    evaluation=candidate.get('evaluation','')
    if evaluation.startswith('WAITING'):candidate_text='已冻结候选，等待独立验证结果；尚未替换正式模型。'
    elif candidate.get('release'):candidate_text='已有验收及版本处理记录；正式使用仍以当前准入检查为准。'
    elif candidate:candidate_text='候选评估已结束，未确认有正式版本替换；继续沿用当前获准版本。'
    else:candidate_text='本次没有新增待验收候选，当前模型版本保持不变。'
    next_at=progress.get('next_research_at')
    next_text=time_text(next_at)+'起，须有新增到期结果且数据检查通过。' if next_at else (
        '候选独立验证结束后、且数据检查通过时才能重新研究；目前不能承诺日期。' if status=='FROZEN_VALIDATION_PENDING' or evaluation.startswith('WAITING') else
        '检查问题解决后重新判断；目前不能承诺日期。' if report.get('errors') else '等待交易日历或新的到期结果，当前没有可确认的时间。')
    daily='已启用每个完整交易日研究，不再使用旧的30天/90天等待间隔。' if report.get('schedule') else '此记录来自旧版研究调度。'
    text='<section><h2>每日模型评估与迭代</h2><p>'+daily+'</p>'
    text+='<p>研究数据日：'+e(str(report.get('asof','尚无')))+'；本页日期：'+e(session)+'；研究检查时间：'+e(time_text(report.get('generated_at')))+'</p>'
    if report.get('asof') and report['asof']!=session:text+='<p class="bad">研究记录尚未更新到本页日期，以下为上次结果。</p>'
    text+='<p><strong>本次状态／等待原因：</strong>'+e(why)+'</p>'
    text+='<p><strong>下次可开展新研究：</strong>'+e(next_text)+'</p><p>下次日常评估：'+e(time_text(progress.get('next_review_at')))+'；18:00为启动时间，完成时间取决于数据和计算。</p>'
    text+='<p>到期确认预测：'+e(str(progress.get('confirmed',quality.get('confirmed','待核验'))))+'；首次日预测有效样本：'+e(str(progress.get('primary_confirmed','待核验')))+'；相比上次研究新增：'+e(str(progress.get('new_primary_outcomes','待核验'))) +'。重复运行的预测不作为新增样本。</p>'
    summary=progress.get('quality_summary') or {}
    text+='<p>当前已确认预测：平均绝对误差 '+percent(summary.get('mae'))+'；方向准确率 '+percent(summary.get('direction_accuracy'))+'。这些是预测质量统计，不是收益或正式升级证明。</p>'
    text+='<p>漂移检查：'+('已发现持续分布变化，需要关注。' if report.get('persistent_drift') else '未发现持续变化，或资料不足。')+' 候选进度：'+e(candidate_text)+'</p>'
    if report.get('errors'):
        text+='<p class="bad">本次有 '+str(len(report['errors']))+' 项检查问题，暂停新研究：</p><ul>'
        for error in report['errors']:
            code=error.get('error',error.get('reason',''))
            description={
                'EMPTY_WATCHLIST':'观察名单为空。',
                'CANDIDATE_COMPARISON_FAILED':'候选参数比较中有计算或数据错误，未完成研究。',
                'FROZEN_RUNTIME_CHANGED_RESEARCH_RESTART_REQUIRED':'冻结实验与当前程序版本不一致，需要核查，原实验记录已保留。',
                'DAILY_STUDY_RUNTIME_CHANGED_REVIEW_REQUIRED':'同一天研究使用的版本发生变化，需要核查，不能覆盖原结果。',
                'FUTURE_PREDICTION_OUTCOME':'到期结果包含当时尚不可用的信息，已停止使用。',
                'ORPHAN_PREDICTION_OUTCOME':'实际结果缺少对应的封存预测，需核对。',
            }.get(code,'历史行情、实际结果的来源／时间／完整性或研究处理未通过检查，请核查数据及运行日志。')
            text+='<li>'+e(str(error.get('symbol','研究流程')))+'：'+e(description)+'</li>'
        text+='</ul>'
    text+='<h3>上次完成的候选研究</h3>'
    if not last or not last.get('studies'):
        return text+'<p>尚无已完成的候选比较。原因见上方；取得合格数据后会显示比较结果。</p></section>'
    studies=last['studies'];eligible=[x for x in studies if x.get('eligible_for_future_test')]
    finite=[x for x in studies if x.get('mean_gain') is not None]
    best=max(finite,key=lambda x:x['mean_gain']) if finite else None
    text+='<p>数据日 '+e(str(last.get('asof','未知')))+'；完成时间 '+e(time_text(last.get('generated_at')))+'。比较 '+str(len(studies))+' 个候选，'+str(len(eligible))+' 个达到进入未来验证的筛选要求。</p>'
    if best:text+='<p>历史平均误差改善最好为 '+percent(best['mean_gain'])+'；该候选在表现最弱的股票／期限上，有 '+str(best.get('minimum_positive_folds',0))+'/4 段改善。原要求：平均改善至少2%，且每个股票／期限至少3/4段改善。</p>'
    text+='<div class="scroll"><table><tr><th>候选参数</th><th>特征数</th><th>历史平均误差改善</th><th>最少改善段数</th><th>结论</th></tr>'
    for row in studies:
        problems=[]
        if row.get('errors'):problems.append('部分数据或计算检查失败')
        if row.get('mean_gain') is None:problems.append('缺少可比较结果')
        elif row['mean_gain']<.02:problems.append('平均改善未达到2%')
        if row.get('minimum_positive_folds',0)<3:problems.append('部分股票／期限改善段数不足3/4')
        conclusion='达到历史筛选要求，仍须未来验证' if row.get('eligible_for_future_test') else '；'.join(problems) or '未满足完整筛选要求'
        values=[row.get('candidate',{}).get('alpha','未知'),len(row.get('candidate',{}).get('features',[])),percent(row.get('mean_gain')),str(row.get('minimum_positive_folds',0))+'/4',conclusion]
        text+='<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in values)+'</tr>'
    return text+'</table></div><p>每天评估与研究，只有未来独立验证全部通过才升级。历史改善不能替代独立验证；当前模拟持仓和既有成交不因研究重算而改写。</p></section>'


SLOT_REASONS={
 'MULTIPLE_TEST_AND_REPEAT_TRIAL_CORRECTION':'扣除多组比较及重复试验影响后，改善证据不足',
 'COMPLETE_PROSPECTIVE_COHORT_REQUIRED':'观察期内的预测或到期结果不完整',
 'INDEPENDENT_SAMPLE_COUNT':'非重叠到期样本不足',
 'DIRECTION_NOT_CONFIDENT':'方向准确率的证据不足',
 'CHAMPION_IMPROVEMENT_NOT_PROVEN':'尚不能证明优于原方案',
 'PAIRED_BASELINE_IMPROVEMENT_NOT_CONFIDENT':'相对基准的误差改善尚不可靠',
 'NO_INDEPENDENT_OUTCOMES':'尚无可验收的独立到期结果',
}


def render_slots(report,quality,session):
    p=report.get('research_progress',{});status=p.get('status');last=p.get('last_slot_study') or {}
    why=REASONS.get(status,'正在检查各组进度。')
    text='<section><h2>每日模型研究、试用与独立验证</h2>'
    text+='<p>研究数据日：'+e(str(report.get('asof','未知')))+'；检查时间：'+e(time_text(report.get('generated_at')))+'</p>'
    text+='<p><strong>本次状态／等待原因：</strong>'+e(why)+'</p>'
    text+='<p>每只股票的1／5／20交易日预测分别研究、分别试用、分别验收。一组未通过不会否决其他组；不再要求统一改善2%。</p>'
    text+='<p><strong>快速研究试用：</strong>历史平均误差减少、4段中至少3段改善，最近20个已到期预测误差减少且方向准确率不低于原方案，才可在下一次完整预测中试用。以上属于历史研究检查，不能称为独立验证通过。</p>'
    text+='<p>当前可研究试用 '+str(len(report.get('research_trial_slots',[])))+' 组；独立预测验证通过 '+str(len(report.get('accepted_slots',[])))+' 组。每日仍按原算法用已成熟数据更新权重；待验证组不重新挑选参数方案。</p>'
    text+='<p><strong>下一次日常研究：</strong>'+e(time_text(p.get('next_review_at')))+'；须有新增到期结果且数据可靠。同一数据日不重复搜索，各组不设额外30天／90天冷却。</p>'
    text+='<p>独立验证与试用并行：首次验收最少1日预测60个起点、5日预测90个起点、20日预测253个起点，再等待末条预测到期。重复试验因多重比较校正可能需要更多证据。交易日历尚未公布的日期不推测，研究试用无需等到正式验收。</p>'
    text+='<p>确认到期预测 '+str(p.get('confirmed','待核验'))+'；首次日预测有效样本 '+str(p.get('primary_confirmed','待核验'))+'；新增 '+str(p.get('new_primary_outcomes','待核验'))+'。近期质量统计不等于真实收益。</p>'
    if report.get('errors'):
        text+='<p class="bad">有 '+str(len(report['errors']))+' 项数据或流程问题，相关组暂停推进；旧记录保留。详情请查看运行诊断。</p>'
    text+='<h3>各组试用和独立验证进度</h3>'
    rows=report.get('slot_progress',[])
    if not rows:text+='<p>当前没有待验证方案；沿用原模型。尚未入选的原因见下方研究结果。</p>'
    else:
        text+='<div class="scroll"><table><tr><th>股票／预测期限</th><th>状态</th><th>已到期／所需起点</th><th>最早独立验收时间</th><th>说明</th></tr>'
        for row in rows:
            symbol,h=row['slot'].split('/');state=row['status'];m=row.get('metrics',{})
            label={'WAITING':'已获研究试用资格，独立验证未完成','ACCEPTED':'独立预测验证通过','REJECTED':'未通过，退出本次试用','ERROR':'检查异常，暂停推进'}.get(state,'待核验')
            explanation='；'.join(SLOT_REASONS.get(x,'其他验收检查未通过') for x in m.get('blockers',[])) or ('尚未验收，不提前查看测试误差来调参。' if state=='WAITING' else '仅涉及预测方案；概率及交易策略资格另行检查。')
            due=row.get('earliest',row.get('first_evaluation_at'))
            date=time_text(due) if due else '后续交易日历尚未齐全，暂不能确定日期'
            count=str(row.get('confirmed',m.get('observations','待核验')))+' / '+str(row.get('required','已完成'))
            text+='<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in [row.get('name',symbol)+' '+symbol+' / '+h+'日',label,count,date,explanation])+'</tr>'
        text+='</table></div>'
    text+='<h3>上次完成的分组研究</h3>'
    studies=last.get('studies',report.get('slot_studies',[]))
    text+='<p>数据日 '+e(str(last.get('asof',report.get('asof','未知'))))+'；完成时间 '+e(time_text(last.get('generated_at',report.get('generated_at'))))+'。待验证组不重复搜索；其他组照常每日检查。</p>'
    if not studies:text+='<p>本次没有新的可搜索组；可能正在等待验证、新到期结果或可靠数据，原因见上方。</p>'
    else:
        text+='<div class="scroll"><table><tr><th>股票／期限</th><th>比较数</th><th>平均误差改善</th><th>近期误差改善</th><th>改善段数</th><th>结果</th></tr>'
        for row in studies:
            best=row.get('winner') or max(row['comparisons'],key=lambda x:x['mean_gain']);symbol,h=row['slot'].split('/')
            reasons=[]
            if best['mean_gain']<=0:reasons.append('总体误差未减少')
            if best.get('recent_gain',0)<=0:reasons.append('近期误差未减少')
            if best['minimum_positive_folds']<3:reasons.append('改善不足3/4段')
            if best.get('recent_direction',0)<best.get('champion_recent_direction',0):reasons.append('近期方向表现退步')
            conclusion='通过历史试用检查；独立验证另行进行' if row.get('winner') else '；'.join(reasons) or '与当前方案相同，保持原方案'
            text+='<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in [row.get('name',symbol)+' '+symbol+' / '+h+'日',row['candidate_count'],percent(best['mean_gain']),percent(best.get('recent_gain')),str(best['minimum_positive_folds'])+'/4',conclusion])+'</tr>'
        text+='</table></div>'
    return text+'<p>预测误差改善不等于交易赚钱。仓位、费用、概率门槛及风险规则不变；研究试用只用于模拟，不赋予正式交易资格。历史预测、成交和收益不追改。</p></section>'
