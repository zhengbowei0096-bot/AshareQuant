"""Silent local paper alerts; read-only trading inputs, separate delivery cursor."""
import argparse
import datetime as dt
import fcntl
import json
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')

def stamp(value):
    result = dt.datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('missing timezone')
    return result

def trading_time(now):
    t = now.astimezone(TZ)
    minutes = t.hour * 60 + t.minute
    return t.weekday() < 5 and (570 <= minutes <= 690 or 780 <= minutes <= 897)

def messages(alerts, health, state, now, notify_trades=False):
    seen = set(state.get('seen', []))
    new = [a for a in alerts if a['id'] not in seen]
    grouped = {}
    for a in (new if notify_trades else []):
        grouped.setdefault(a.get('order_id', a['id']), []).append(a)
    lines = []
    for group in grouped.values():
        fills = [a for a in group if a['status'] == 'PAPER_FILL']
        last = group[-1]
        side = '买入' if last['side'] == 'BUY' else '卖出'
        when = stamp(last['at']).astimezone(TZ)
        historical = '【历史事件，勿按旧价操作】' if (now-when).total_seconds() > 300 else ''
        identity = next((a.get('name') for a in group if a.get('name')), '')
        head = f"{historical}{identity} {last['symbol']} {side} · 北京时间{when:%m-%d %H:%M:%S}"
        detail = []
        if any(a['status'] == 'ACCEPTED' for a in group):
            a = next(a for a in group if a['status'] == 'ACCEPTED')
            detail.append(f"待撮合模拟委托 {a['shares']}股，限价{a['limit_price']:.3f}元")
        if fills:
            quantity = sum(a['shares'] for a in fills)
            price = sum(a['shares']*a['price'] for a in fills)/quantity
            detail.append(f"本次新增模拟成交 {quantity}股，均价{price:.3f}元")
        labels = {'FILLED':'全部模拟成交','PARTIALLY_FILLED':'部分模拟成交','CANCELED':'模拟撤单','REJECTED':'模拟拒单'}
        if last['status'] in labels:
            detail.append(labels[last['status']] + f"；累计成交{last.get('filled',0)}股")
        reasons = last.get('rationale') or [last.get('reason') or '后续有效报价满足模拟撮合条件']
        detail.append('依据：' + '；'.join(map(str, reasons)))
        lines.append(head + '\n' + '；'.join(detail))
    anomaly = state.get('anomaly')
    reason_count = state.get('reason_count', 0)
    if trading_time(now):
        current = None
        if (now-stamp(health['checked_at'])).total_seconds() > 120:
            current = '服务超过120秒未更新'
        elif health.get('status') in ('FAILED','WAIT_CONFIG_REVIEW','STALE_VALUATION'):
            current = health['status'] + ': ' + str(health.get('reason') or '')
        reason = health.get('reason')
        reason_count = reason_count+1 if reason and reason == state.get('last_reason') else (1 if reason else 0)
        if not current and reason_count >= 2:
            current = '研究推理持续不可用：' + str(reason)
        if current != anomaly:
            if current: lines.append('服务异常：' + current)
            elif anomaly: lines.append('服务已恢复：当前健康检查正常。')
        anomaly = current
    updated = dict(state, seen=sorted(seen | {a['id'] for a in new}), anomaly=anomaly,
                   reason_count=reason_count, last_reason=health.get('reason'), checked_at=now.isoformat())
    return lines, updated

def popup(body):
    # argv supplies message text as data, never executable AppleScript.
    script = '''on run argv
    display dialog (item 1 of argv) with title "AshareQuant 服务异常提醒" buttons {"知道了"} default button "知道了" giving up after 120
    end run'''
    subprocess.run(['/usr/bin/osascript','-e',script,body],check=True,capture_output=True,timeout=130)

def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)

def run(root):
    folder = root/'logs/mac_alerts'
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'lock').open('w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        path = folder/'cursor.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        now = dt.datetime.now(TZ)
        try:
            alerts = json.loads((root/'data/dynamic/alerts.json').read_text())['alerts']
            health = json.loads((root/'logs/dynamic_health.json').read_text())
            lines, updated = messages(alerts,health,state,now)
            if state.get('read_error'):
                lines.append('提醒服务已恢复：可以读取交易提醒和健康状态。')
            updated['read_error'] = None
        except (OSError,ValueError,KeyError,TypeError) as exc:
            if not trading_time(now): return
            error = type(exc).__name__ + ': ' + str(exc)
            lines = ['提醒服务异常：'+error] if state.get('read_error') != error else []
            updated = dict(state,read_error=error)
        if lines:
            # Persist a visible local outbox first. Delivery failure retries next poll.
            body = '\n\n'.join(lines) + '\n\n仅模拟；真实账户由你手动操作，成交可能不同。'
            with (folder/'events.jsonl').open('a') as log:
                log.write(json.dumps(dict(at=now.isoformat(),body=body,status='ATTEMPT'),ensure_ascii=False)+'\n')
            popup(body)
        save(path,updated)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();run(args.root)

if __name__ == '__main__': main()
