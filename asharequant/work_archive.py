"""Dated work records and deduplicated source snapshots; runtime data stays in place."""
from pathlib import Path
import hashlib,html,json,os,re,zipfile
from .core import now,write_json,atomic_text

PATTERNS=('asharequant/**/*.py','tests/test*.py','tools/*.py','scripts/*.py','docs/**/*.md','config/*.example.json','config/*.example.csv','*.py','*.md','*.txt','*.command','config.json','*_config.json','watchlist.csv')

def record(root,delivery,pipeline):
    root=Path(root);stamp=now();base=root/'archive';day=base/str(stamp.date());sources={}
    for pattern in PATTERNS:
        for p in root.glob(pattern):
            if p.is_file() and not p.is_symlink():sources[p.relative_to(root).as_posix()]=p.read_bytes()
    fingerprints={name:hashlib.sha256(data).hexdigest() for name,data in sorted(sources.items())}
    version=hashlib.sha256(json.dumps(fingerprints,sort_keys=True).encode()).hexdigest()
    snapshots=day/'代码版本';snapshots.mkdir(parents=True,exist_ok=True);snapshot=snapshots/(version+'.zip')
    if not snapshot.exists():
        temp=snapshot.with_suffix('.pending');temp.touch(mode=0o600,exist_ok=False)
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
            for name,data in sorted(sources.items()):z.writestr(name,data)
            z.writestr('SOURCE_MANIFEST.json',json.dumps(fingerprints,ensure_ascii=False,indent=2))
        with zipfile.ZipFile(temp) as z:
            if z.testzip():raise ValueError('SOURCE_ARCHIVE_CHECK_FAILED')
        os.replace(temp,snapshot)
    entry=day/('工作-'+stamp.strftime('%H%M%S-%f'));entry.mkdir()
    value=dict(work_date=str(stamp.date()),created_at=stamp.isoformat(),market_session=delivery['session'],
        status=pipeline.get('status'),mode=pipeline.get('mode'),code_version=version,code_snapshot=str(snapshot),
        report_directory=delivery['directory'],overview=delivery['overview'],pipeline=pipeline,
        note='按北京时间工作日归档；行情日期单列。原始数据、模型审计库和回滚记录保留在项目原路径。',submitted_orders=0)
    write_json(entry/'工作记录.json',value)
    e=html.escape
    atomic_text(entry/'工作说明.txt',f"工作日期：{value['work_date']}（北京时间）\n行情日期：{value['market_session']}\n状态：{value['status']}\n报告：{value['overview']}\n代码快照：{snapshot}\n原始数据和模型历史保留原路径；代码ZIP不是整个账户数据备份。\n")
    rows=[]
    for p in sorted(day.glob('工作-*/工作记录.json'),reverse=True):
        d=json.loads(p.read_text());rows.append('<li>'+e(d['created_at'])+' · 行情 '+e(str(d['market_session']))+' · '+e(str(d['status']))+' · <a href="'+e(os.path.relpath(d['overview'],day))+'">报告</a> · <a href="'+e(str(p.relative_to(day)))+'">工作记录</a></li>')
    atomic_text(day/'打开当天工作.html','<!doctype html><meta charset="utf-8"><h1>'+str(stamp.date())+' 工作归档</h1><p>工作日期与行情日期分别保存；代码快照在“代码版本”文件夹。</p><ul>'+''.join(rows)+'</ul>')
    days=sorted((p for p in base.iterdir() if p.is_dir() and re.fullmatch(r'\d{4}-\d{2}-\d{2}',p.name)),reverse=True)
    atomic_text(base/'打开工作归档.html','<!doctype html><meta charset="utf-8"><h1>AshareQuant 工作归档</h1><p>从北京时间2026-10-20开始记录。旧报告、模型记录和回滚备份保留。</p><ul>'+''.join('<li><a href="'+p.name+'/打开当天工作.html">'+p.name+'</a></li>' for p in days)+'</ul>')
    return dict(work_date=str(stamp.date()),record=str(entry/'工作记录.json'),source_snapshot=str(snapshot),index=str(base/'打开工作归档.html'))
