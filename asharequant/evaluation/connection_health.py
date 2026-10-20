"""Persist evidence, not promises, for unattended pipeline readiness."""
from pathlib import Path
import json,plistlib
from ..core import now,write_json

def run(root):
    root=Path(root);at=now();items={}
    for name,path in {'heartbeat':'logs/scheduler_heartbeat.json','maintenance':'logs/maintenance.json','daily':'logs/schedule.json','quality':'reports/prediction_quality/latest/report.json','history':'reports/public_history/latest.json','broker':'reports/broker_records/latest.json'}.items():
        p=root/path
        if p.exists():
            d=json.loads(p.read_text());t=d.get('checked_at') or d.get('completed_at') or d.get('generated_at') or d.get('fetched_at') or d.get('last_attempt')
            items[name]=dict(present=True,time=t,status=d.get('status'),counts=d.get('counts'),errors=len(d.get('errors',[])))
        else:items[name]=dict(present=False)
    plist=Path.home()/'Library/LaunchAgents/com.asharequant.daily.plist'
    config=plistlib.loads(plist.read_bytes()) if plist.exists() else {}
    result=dict(checked_at=at.isoformat(),items=items,launchagent_plist_present=bool(config),interval_seconds=config.get('StartInterval'),
        guarantees='Only runs while Mac user session is active and machine is awake; missed past predictions are not backfilled',
        submitted_orders=0,trade_policy='UNCHANGED_FAIL_CLOSED')
    write_json(root/'reports/connection_health/latest.json',result);return result
