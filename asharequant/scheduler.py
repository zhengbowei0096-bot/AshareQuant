"""LaunchAgent wake-up helper. No machine wake/sleep or account changes."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import hashlib
import logging
import subprocess
import sys

from .core import now, load_config, write_json, run_lock


def due(at, state, cfg, sessions=None):
    from .core import TZ
    if at.tzinfo is None: return False
    at = at.astimezone(TZ)
    if sessions is not None and str(at.date()) not in {str(d.date()) for d in sessions}:
        return False
    if (at.hour, at.minute) < max((18, 0), (cfg['schedule_hour'], cfg['schedule_minute'])):
        return False
    if at.weekday() >= 5:
        return False
    today = str(at.date())
    if state.get('success_date') == today:
        return False
    if state.get('attempt_date') == today and state.get('attempts', 0) >= 3:
        return False
    if state.get('attempt_date') == today and state.get('last_attempt'):
        if (at - datetime.fromisoformat(state['last_attempt'])).total_seconds() < 3600:
            return False
    return True


def maintenance(root, at=None):
    """Independent, bounded bookkeeping even if research/daily stages failed."""
    root=Path(root);at=at or now();path=root/'logs/maintenance.json'
    with run_lock(root/'logs/maintenance'):
        old=json.loads(path.read_text()) if path.exists() else {}
        files=[p for p in (root/'data/broker_inbox').glob('*/*.csv') if not p.is_symlink()]
        source_config=root/'broker_sources.json'
        try:
            sources=json.loads(source_config.read_text()) if source_config.exists() else {}
            if not isinstance(sources,dict):sources={}
        except (ValueError,OSError):sources={}
        for key in ('nav_directories','fills_directories'):
            for directory in sources.get(key,[]):
                p=Path(directory)
                if p.is_absolute() and p.is_dir() and not p.is_symlink():files.extend(x for x in p.glob('*.csv') if not x.is_symlink())
        signature=hashlib.sha256(json.dumps(dict(files=sorted((str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in files),configuration=source_config.read_bytes().hex() if source_config.exists() else None)).encode()).hexdigest()
        if old.get('signature')==signature and old.get('completed_at') and (at-datetime.fromisoformat(old['completed_at'])).total_seconds()<3600 and not old.get('errors'):
            return dict(status='ALREADY_MAINTAINED_RECENTLY')
        steps={};errors=[]
        for name in ('broker_records','prediction_quality'):
            try:
                if name=='broker_records':
                    from .broker_records import ingest
                    result=ingest(root);steps[name]={k:result[k] for k in ('status','counts','errors')}
                    if result['errors']:errors.append(name+':INVALID_EXPORTS')
                else:
                    from .prediction_quality import run
                    result=run(root);steps[name]={k:result[k] for k in ('asof','predictions','confirmed','pending')}
            except Exception as exc:errors.append(name+':'+type(exc).__name__)
        result=dict(status='FAILED' if errors else 'SUCCESS',completed_at=at.isoformat(),signature=signature,steps=steps,errors=errors,submitted_orders=0)
        write_json(path,result);return result


def main():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root)
    state_file = root / 'logs' / 'schedule.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    at = now()
    write_json(root/'logs/scheduler_heartbeat.json',dict(checked_at=at.isoformat(),timezone='Asia/Shanghai',submitted_orders=0))
    try:
        maintenance_state=maintenance(root,at)
    except RuntimeError:
        maintenance_state=dict(status='BUSY')
    except Exception:
        logging.exception('维护分支异常；保留每日主流程独立重试')
        maintenance_state=dict(status='FAILED')
    if not due(at, state, cfg):
        return 2 if maintenance_state.get('status')=='FAILED' else 0
    # Never infer an exchange session from weekday alone. Refresh before gating.
    from .data import calendar
    import pandas as pd
    try:
        sessions, _ = calendar(root, cfg, pd.Timestamp(at.date()), offline=False)
    except Exception:
        logging.exception('无法核实交易日历；本次禁止启动每日预测')
        return 2
    if not due(at, state, cfg, sessions):
        return 0
    # Separate lock covers scheduler retries; updater has its own lock as well.
    lock_root = root / 'logs' / 'scheduler'
    try:
        with run_lock(lock_root):
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
            if not due(at, state, cfg, sessions): return 0
            if state.get('attempt_date') != str(at.date()):
                state['attempts'] = 0
            state.update(attempt_date=str(at.date()), last_attempt=at.isoformat(),
                         attempts=state.get('attempts', 0) + 1)
            write_json(state_file, state)
            try:
                result = subprocess.run([sys.executable, '-m', 'asharequant.cli', 'daily', '--root', str(root)], cwd=root,timeout=14400)
            except subprocess.TimeoutExpired:
                state['last_exit_code']=124
                write_json(state_file,state)
                return 124
            state['last_exit_code'] = result.returncode
            if result.returncode == 0:
                state['success_date'] = str(at.date())
            write_json(state_file, state)
            return result.returncode
    except RuntimeError:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
