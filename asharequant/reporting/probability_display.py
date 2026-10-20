"""Research estimates are visible without granting validation or trading authority."""
import math
REASONS={'INDEPENDENT_HOLDOUT_BLOCKS_BELOW_30':'独立验证日期块不足30个','BRIER_NOT_BETTER_THAN_BASE_RATE':'概率误差未优于历史上涨比例基准','CALIBRATION_ERROR_ABOVE_10PCT':'校准误差超过10个百分点','NONPOSITIVE_PROBABILITY_SLOPE':'概率与预测收益关系未通过方向检查','INSUFFICIENT_DATA':'独立校准样本不足','MISSING':'尚未完成该版本的独立校准'}
def fields(f):
 c=f.get('probability_calibration') or {};valid=c.get('independent_validated') is True
 value=f.get('up_probability_calibrated');kind='独立验证概率' if valid else '研究校准估计'
 if value is None:value=f.get('up_probability_empirical');kind='经验残差研究估计'
 if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1:value=None;valid=False
 codes=[] if valid else c.get('blockers') or [c.get('status','MISSING')]
 reasons=[REASONS.get(x,x) for x in codes]
 if c.get('holdout_blocks') is not None and not valid:reasons.append('独立验证日期块：'+str(c['holdout_blocks']))
 return dict(probability_basis=c.get('method',f.get('probability_method')) if f.get('up_probability_calibrated') is not None else f.get('probability_method'),research_up_probability=value,probability_estimate_kind=kind,probability_validated=valid,probability_validation_reasons=reasons,probability_validation_codes=codes,probability_calibration_status=c.get('status','MISSING'))
def text(row):
 v=row.get('research_up_probability',row.get('up_probability'));valid=row.get('probability_validated',row.get('probability_calibrated',False))
 if v is None:return '无可用估计；'+('；'.join(row.get('probability_validation_reasons',[])) or '缺少数据')
 return f'{v:.2%}（'+('独立验证通过' if valid else '研究估计，未通过验证')+'）'+('；'+'；'.join(row.get('probability_validation_reasons',[])) if not valid else '')

def html_cell(row):
 from html import escape
 v=row.get('research_up_probability',row.get('up_probability'));valid=row.get('probability_validated',row.get('probability_calibrated',False))
 title=('无可用估计' if v is None else f'{v:.2%}')+' · '+('验证通过' if valid else '研究估计')
 reasons=row.get('probability_validation_reasons',[])
 return escape(title)+(' <details><summary>未通过原因</summary><div style="white-space:normal;width:240px">'+escape('；'.join(reasons) or '尚未完成独立验证')+'</div></details>' if not valid else '')
