"""Fixed candidate estimators. No tuning on the outer validation set."""
from pathlib import Path
import json,hashlib,warnings
import numpy as np
import pandas as pd

NAMES=('ar5','lasso','lightgbm','xgboost')
PARAMS={
 'ar5':{'lags':5,'method':'direct multi-step least squares autoregression'},
 'lasso':{'alpha':.01,'max_iter':20000,'tol':1e-7,'selection':'cyclic'},
 'lightgbm':{'n_estimators':100,'learning_rate':.03,'num_leaves':7,'max_depth':3,'min_child_samples':40,'reg_lambda':10.,'random_state':1729,'n_jobs':1,'verbosity':-1,'deterministic':True,'force_col_wise':True},
 'xgboost':{'n_estimators':100,'learning_rate':.03,'max_depth':2,'min_child_weight':20,'reg_lambda':10.,'objective':'reg:squarederror','tree_method':'hist','random_state':1729,'n_jobs':1}}

def signature():
 directory=Path(__file__).parent
 files=['model_candidates.py','forecast.py','core.py','data.py','strict_validation.py','seven_metrics.py','model_research.py','model_monitor.py']
 content=b''.join(name.encode()+b'\0'+(directory/name).read_bytes() for name in files)
 return hashlib.sha256(content+json.dumps(PARAMS,sort_keys=True).encode()).hexdigest()

def add_lags(frame):
 f=frame.copy()
 for lag in range(5):f[f'ar_lag{lag}']=f.r1.shift(lag)
 return f

def columns(name):
 from .forecast import FEATURES
 return [f'ar_lag{i}' for i in range(5)] if name=='ar5' else FEATURES

def fit(name,train,test,artifact=None):
 if name not in NAMES:raise ValueError('未知候选模型')
 cols=columns(name);a=train[cols].to_numpy(float);b=test[cols].to_numpy(float);y=train.target.to_numpy(float)
 if not (np.isfinite(a).all() and np.isfinite(b).all() and np.isfinite(y).all()):raise ValueError('模型输入包含缺失或非有限值')
 meta={'model':name,'features':cols,'parameters':PARAMS[name],'implementation_hash':signature()}
 if name in ('ar5','lasso'):
  mu=a.mean(0);sd=a.std(0);sd[sd<1e-10]=1.;a=(a-mu)/sd;b=(b-mu)/sd
  ym=float(y.mean());ys=float(y.std());ys=max(ys,1e-10)
  if name=='ar5':
   coef=np.linalg.lstsq(a,y-ym,rcond=None)[0];intercept=ym
  else:
   from sklearn.linear_model import Lasso
   from sklearn.exceptions import ConvergenceWarning
   with warnings.catch_warnings():
    warnings.simplefilter('error',ConvergenceWarning)
    estimator=Lasso(**PARAMS[name]).fit(a,(y-ym)/ys)
   coef=estimator.coef_*ys;intercept=ym+estimator.intercept_*ys
  pred=intercept+b@coef
  meta.update(means=mu.tolist(),scales=sd.tolist(),coefficients=coef.tolist(),intercept=float(intercept))
 else:
  if name=='lightgbm':
   from lightgbm import LGBMRegressor
   estimator=LGBMRegressor(**PARAMS[name])
  else:
   from xgboost import XGBRegressor
   estimator=XGBRegressor(**PARAMS[name])
  estimator.fit(train[cols],y);pred=estimator.predict(test[cols])
  if artifact:
   artifact=Path(artifact);artifact.parent.mkdir(parents=True,exist_ok=True)
   dest=artifact.with_suffix('.txt' if name=='lightgbm' else '.ubj')
   if name=='lightgbm':estimator.booster_.save_model(str(dest))
   else:estimator.save_model(str(dest))
   meta['model_file']=str(dest);meta['model_file_sha256']=hashlib.sha256(dest.read_bytes()).hexdigest()
 if not np.isfinite(pred).all():raise ValueError('模型输出非有限值')
 if artifact:
  artifact=Path(artifact);artifact.parent.mkdir(parents=True,exist_ok=True);artifact.with_suffix('.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2))
 return np.asarray(pred),meta

def selected(root,symbol,horizon,input_version=None,asof=None):
 path=Path(root)/'data/model_registry.json'
 if not path.exists():return 'ridge'
 state=json.loads(path.read_text())
 from .core import cutoff
 from .forecast import DEFAULTS
 import importlib.metadata
 config=Path(root)/'forecast_config.json'
 current={**DEFAULTS,**(json.loads(config.read_text()) if config.exists() else {})}
 if state.get('config')!=current or state.get('evaluated_asof')!=(asof or str(cutoff().date())):return 'ridge'
 for package,version in state.get('dependencies',{}).items():
  if importlib.metadata.version(package)!=version:return 'ridge'
 entry=state.get('active',{}).get(f'{symbol}/{horizon}')
 if not entry:return 'ridge'
 if input_version is not None and entry.get('input_version')!=input_version:return 'ridge'
 if state.get('implementation_hash')!=signature():return 'ridge'
 if not entry.get('historical_passed') or not entry.get('prospective_passed'):return 'ridge'
 if entry.get('model') not in NAMES:return 'ridge'
 if not state.get('spec_id'):return 'ridge'
 from .model_monitor import permitted
 identity=f"return|{state['spec_id']}|{entry['model']}|{symbol}|{horizon}"
 if not permitted(root,'return',state,identity):return 'ridge'
 return entry['model']
