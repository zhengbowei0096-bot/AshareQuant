"""Explain frozen admission gates without relaxing them or promising promotion."""
from pathlib import Path
import json,html
from ..core import now,write_json,atomic_text

def run(root):
 root=Path(root)
 def read(relative):
  p=root/relative;return json.loads(p.read_text()) if p.exists() else {}
 lifecycle=read('reports/lifecycle/latest.json');p=lifecycle.get('policy',{})
 spec=read('data/lifecycle/experiments/'+str(lifecycle.get('experiment'))+'/spec.json');h=max(spec.get('horizons',[1,5,20]));total=p.get('calibration_origins',0)+h+p.get('test_origins',0)+h
 calibration=read('reports/decisions/calibration.json')
 result=dict(generated_at=now().isoformat(),status='INFORMATION_ONLY',experiment=lifecycle.get('experiment'),start_session=lifecycle.get('start_session'),evaluation_status=lifecycle.get('evaluation_status'),policy=p,configured_session_budget_with_20_day_embargo_and_maturity=total,calibration=calibration.get('horizons',{}),standards_changed=False,
  guidance=['立即可用于研究展示、独立规则风控方案与模拟收益跟踪；这不等于收益预测已获正式资格。','当前冻结流程按126日校准、20日隔离、672日测试和最长20日标签成熟推进；不能以重复跑程序缩短市场时间。','固定候选、特征与评价指标，先推进既定实验；界面改动与核心模型修改分离，核心实现变化需重新审查。','不反复试参数迎合留出结果；研究候选并行比较，但必须保留独立测试、多重检验和交易成本、风险与稳定性检查。','优先诊断T+1：当前独立块较多，但概率误差仍需优于基准；T+5/T+20重叠标签不能冒充独立样本。','若设计更短周期的独立上线协议，必须预先固定并证明统计证据充分，不能删掉现有失败门槛；本次没有改动冻结标准。'])
 folder=root/'reports/validation_readiness/latest';folder.mkdir(parents=True,exist_ok=True);write_json(folder/'report.json',result)
 from ..probability_display import REASONS
 rows=''
 for h,c in result['calibration'].items():
  rows+='<tr><td>'+html.escape(h)+'</td><td>'+str(c.get('holdout_blocks'))+'</td><td>'+html.escape('；'.join(REASONS.get(x,x) for x in c.get('blockers',[])) or c.get('status','未知'))+'</td></tr>'
 page='<meta charset="utf-8"><title>模型准入进度与加速边界</title><style>body{font:15px/1.6 system-ui;margin:24px}td,th{padding:6px 10px;text-align:left}</style><h1>模型准入进度与加速边界</h1><p>实验开始：'+html.escape(str(result['start_session']))+'；状态：'+html.escape(str(result['evaluation_status']))+'</p><p>当前配置约需 '+str(total)+' 个交易日（含最长20日隔离和标签成熟；不承诺届时通过）。不修改准入门槛。</p><table><tr><th>预测期限</th><th>独立验证日期块</th><th>当前未通过原因</th></tr>'+rows+'</table><ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in result['guidance'])+'</ul>'
 page+='<p>方法参考：<a href="https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html">时间序列验证与隔离窗口</a> · <a href="https://scikit-learn.org/stable/modules/calibration.html">独立概率校准</a>。上面的838日来自本项目配置，不是通用上线年限。</p>'
 atomic_text(folder/'index.html',page);return result
