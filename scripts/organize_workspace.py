"""Build an English-first Finder workspace without moving runtime inputs or ledgers.

The generated folders contain relative links and purpose guides. On macOS only,
original root entries are hidden from Finder; Python paths stay unchanged.
"""
from pathlib import Path
import argparse, datetime, hashlib, html, json, os, shlex, shutil, stat, sys, zipfile

MARKER='.workspace-category.json'
STATE='.workspace-layout.json'

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def safe_relative(value):
    p=Path(value)
    if p.is_absolute() or '..' in p.parts:raise ValueError('Unsafe layout path: '+value)
    return p

def load_plan(root):
    root=Path(root).resolve()
    layout=json.loads((root/'config/workspace_layout.json').read_text())
    groups=layout['groups']
    for g in groups:
        safe_relative(g['name'])
        if len(Path(g['name']).parts)!=1:raise ValueError('Category must be a root folder')
        for e in g['entries']:
            for key in ('target','label','subfolder'):safe_relative(e.get(key,''))
    for rule in layout.get('rules',[]):
        g=next(g for g in groups if g['name']==rule['group'])
        for p in sorted((root/rule['base']).glob(rule['pattern'])):
            g['entries'].append(dict(target=p.relative_to(root).as_posix(),label=p.name,subfolder=rule['subfolder'],purpose=rule['purpose']))
    covered={Path(e['target']).parts[0] for g in groups for e in g['entries']}
    allowed=covered|set(layout['metadata_names'])|{g['name'] for g in groups}
    unknown=sorted(p.name for p in root.iterdir() if p.name not in allowed)
    if unknown:raise ValueError('Unclassified root items; review before applying: '+', '.join(unknown))
    source={e['target'] for g in groups for e in g['entries'] if e['target'].startswith('asharequant/')}
    unlisted=sorted(p.relative_to(root).as_posix() for p in (root/'asharequant').rglob('*.py') if p.relative_to(root).as_posix() not in source)
    if unlisted:raise ValueError('Unclassified source files: '+', '.join(unlisted))
    return layout

def guide_html(title,body):
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>body{font:15px/1.55 system-ui;color:#183047;max-width:1250px;margin:24px auto;padding:0 18px}h1{font-size:26px}h2{font-size:20px;margin:20px 0 8px}table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #ddd;padding:6px 9px}td:first-child{width:33%;overflow-wrap:anywhere}a{color:#066b85}p{margin:8px 0}</style><h1>'+html.escape(title)+'</h1>'+body+'</html>'

def build_files(root,g):
    folder=root/g['name'];files={};links={};rows=[]
    for e in g['entries']:
        target=root/e['target']
        if not target.exists():continue
        relative=Path(e.get('subfolder',''))/e['label']
        if relative.as_posix() in links:raise ValueError('Duplicate workspace label')
        links[relative.as_posix()]=os.path.relpath(target,folder/relative.parent)
        rows.append((relative.as_posix(),e))
    body='<p>'+html.escape(g['purpose'])+'</p><p>Links open the original files. Editing a link edits the running project. (链接指向原文件，编辑会影响运行项目。)</p><table><tr><th>Item (项目)</th><th>Purpose (用途)</th></tr>'
    for rel,e in rows:body+='<tr><td>'+html.escape(rel)+'</td><td>'+html.escape(e['purpose'])+'</td></tr>'
    files['00_README.html']=guide_html(g['name'],body+'</table>')
    files['00_README.md']='# '+g['name']+'\n\n'+g['purpose']+'\n\n'+''.join('- **'+r+'** — '+e['purpose']+'\n' for r,e in rows)
    for sub in sorted({e.get('subfolder','') for _,e in rows}-{''}):
        entries=[(r,e) for r,e in rows if e.get('subfolder','')==sub]
        files[sub+'/00_README.txt']=sub+'\n\n'+''.join(Path(r).name+' — '+e['purpose']+'\n\n' for r,e in entries)
    return files,links

def check_owned(folder):
    if not folder.exists():return
    marker=folder/MARKER
    if not marker.is_file():raise ValueError('Unmanaged category exists: '+str(folder))
    previous=json.loads(marker.read_text())
    expected=set(previous['files'])|set(previous['links'])|{MARKER}
    for p in folder.rglob('*'):
        if p.is_dir() and not p.is_symlink():continue
        rel=p.relative_to(folder).as_posix()
        if rel=='.DS_Store':continue
        if rel not in expected:raise ValueError('Unmanaged file in category: '+str(p))
        if rel in previous['files'] and (p.is_symlink() or sha(p)!=previous['files'][rel]):raise ValueError('Edited guide must be preserved: '+str(p))
        if rel in previous['links'] and (not p.is_symlink() or os.readlink(p)!=previous['links'][rel]):raise ValueError('Edited link must be preserved: '+str(p))

def cache_candidates(root):
    result=[]
    for name in ('asharequant','tests','scripts','tools','vendor'):
        for p in (root/name).rglob('*'):
            if p.is_file() and not p.is_symlink() and (p.suffix in ('.pyc','.pyo') or p.name=='.DS_Store'):result.append(p)
    if (root/'.DS_Store').is_file():result.append(root/'.DS_Store')
    return result

def generate_guide(root,layout):
    md='# AshareQuant File Guide (文件逐项说明)\n\nDesktop workspace: numbered English categories; runtime paths are retained for services and integrity checks. (桌面按编号英文分类，服务路径与完整性检查保持原位。)\n\n'
    body='<p>Numbered categories match the desktop workspace. Source files stay in their existing locations. (编号与桌面一致，源码保留原位置。)</p>'
    for g in layout['groups']:
        md+='## '+g['name']+'\n\n'+g['purpose']+'\n\n';body+='<h2>'+html.escape(g['name'])+'</h2><p>'+html.escape(g['purpose'])+'</p>'
        if not g['name'].startswith('02_'):continue
        for sub in dict.fromkeys(e.get('subfolder','') for e in g['entries']):
            md+='### '+sub+'\n\n| File (文件) | Purpose (用途) |\n|---|---|\n';body+='<h3>'+html.escape(sub)+'</h3><table><tr><th>File (文件)</th><th>Purpose (用途)</th></tr>'
            for e in g['entries']:
                if e.get('subfolder','')!=sub:continue
                target=e['target'];desc=e['purpose']
                md+='| ['+target+'](../'+target+') | '+desc+' |\n';body+='<tr><td><a href="../'+html.escape(target,quote=True)+'">'+html.escape(target)+'</a></td><td>'+html.escape(desc)+'</td></tr>'
            md+='\n';body+='</table>'
    md+='## Maintenance (维护)\n\nRun `python scripts/organize_workspace.py --root .` to preview classification; add `--apply` to refresh it. Unknown root files or Python modules stop the refresh for review. Use `--rollback BACKUP` to restore the previous display and cleaned caches. Only generated navigation and confirmed caches are removed; data, ledgers, source algorithms and risk settings are retained. (先预览，未知文件须补充分类；可恢复原显示和缓存，不删除账本和算法。)\n'
    doc=guide_html('AshareQuant File Guide (文件逐项说明)',body)
    return {'docs/FILE_GUIDE.md':md,'docs/FILE_GUIDE.html':doc,'docs/文件逐项说明.md':md,'docs/文件逐项说明.html':doc}

def apply(root,clean_cache=False):
    root=Path(root).resolve();layout=load_plan(root)
    for g in layout['groups']:check_owned(root/g['name'])
    original=[p for p in root.iterdir() if p.name not in {g['name'] for g in layout['groups']} and p.name!=STATE]
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=root/'backups/workspace-organization'/stamp;backup.mkdir(parents=True)
    changes={'root':str(root),'created_at_utc':stamp,'flags':{},'categories':{},'docs':{},'caches':[]}
    for p in original:changes['flags'][p.name]=getattr(p.lstat(),'st_flags',0)
    if (root/STATE).exists():shutil.copy2(root/STATE,backup/STATE)
    legacy=root/'00_程序文件导航';changes['legacy_navigation']=legacy.exists()
    if legacy.exists():shutil.copytree(legacy,backup/'legacy-navigation',symlinks=True)
    for g in layout['groups']:
        folder=root/g['name'];changes['categories'][g['name']]=folder.exists()
        if folder.exists():shutil.copytree(folder,backup/'categories'/g['name'],symlinks=True)
    for name,text in generate_guide(root,layout).items():
        p=root/name;changes['docs'][name]=p.exists()
        if p.exists():q=backup/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    # Persist rollback metadata before changing the desktop view.
    (backup/'changes.json').write_text(json.dumps(changes,ensure_ascii=False,indent=2))
    try:
        for g in layout['groups']:
            folder=root/g['name'];files,links=build_files(root,g)
            stage=backup/'prepared'/g['name'];stage.mkdir(parents=True)
            if g['name'].startswith('00_'):
                for label,rel in [('Open_Daily_Overview (每日总览).command','reports/dashboard/latest/index.html')]:
                    files[label]='#!/bin/zsh\nROOT="$(cd "$(dirname "$0")/.." && pwd)"\n/usr/bin/open "$ROOT/'+rel+'"\n'
                files['Open_Live_Account (实时账户).command']='#!/bin/zsh\nROOT="$(cd "$(dirname "$0")/.." && pwd)"\n'+shlex.quote(sys.executable)+' - "$ROOT" <<\'PYOPEN\'\nfrom pathlib import Path\nimport json,subprocess,sys\nr=Path(sys.argv[1]);c=json.loads((r/"report_delivery_config.json").read_text());p=Path(c["desktop_directory"])/"打开动态研究模拟.html"\nif p.is_file():subprocess.run(["/usr/bin/open",str(p)],check=True)\nelse:raise SystemExit("Live account report is not available yet.")\nPYOPEN\n'
            for name,value in files.items():
                p=stage/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
                if p.suffix=='.command':p.chmod(0o755)
            for name,target in links.items():
                p=stage/name;p.parent.mkdir(parents=True,exist_ok=True);p.symlink_to(target)
            (stage/MARKER).write_text(json.dumps({'files':{name:sha(stage/name) for name in files},'links':links},ensure_ascii=False,indent=2))
            if folder.exists():shutil.rmtree(folder)
            stage.rename(folder)
        if changes['legacy_navigation']:
            shutil.rmtree(legacy);legacy.mkdir()
            (legacy/'File_Guide.html').symlink_to('../docs/FILE_GUIDE.html')
            source_group=next(g['name'] for g in layout['groups'] if g['name'].startswith('02_'))
            (legacy/'Source_by_Function (按用途查看)').symlink_to('../'+source_group)
            (legacy/'README.txt').write_text('Compatibility navigation: use the current source categories or 00_Start_Here. (兼容入口，使用当前分类或首页入口。)\n')
        if hasattr(os,'chflags'):
            for p in original:
                if p.name.startswith('.'):continue
                os.chflags(p,changes['flags'][p.name]|stat.UF_HIDDEN,follow_symlinks=False)
        if clean_cache:
            candidates=cache_candidates(root)
            with zipfile.ZipFile(backup/'removed-cache.zip','w',zipfile.ZIP_DEFLATED) as z:
                for p in candidates:z.write(p,p.relative_to(root).as_posix())
            with zipfile.ZipFile(backup/'removed-cache.zip') as z:
                if z.testzip():raise ValueError('Cache backup verification failed')
            for p in candidates:
                changes['caches'].append(dict(path=p.relative_to(root).as_posix(),bytes=p.stat().st_size,sha256=sha(p)));p.unlink()
            for name in ('asharequant','tests','scripts','tools','vendor'):
                for p in sorted((root/name).rglob('__pycache__'),key=lambda p:len(p.parts),reverse=True):
                    if not any(p.iterdir()):p.rmdir()
        links=[p for g in layout['groups'] for p in (root/g['name']).rglob('*') if p.is_symlink()]
        if not all(p.exists() for p in links):raise ValueError('Broken workspace link')
        changes.update(links_verified=len(links),cache_bytes=sum(p['bytes'] for p in changes['caches']))
        (backup/'changes.json').write_text(json.dumps(changes,ensure_ascii=False,indent=2))
        (root/STATE).write_text(json.dumps(dict(version=1,backup=str(backup),categories=[g['name'] for g in layout['groups']],links=len(links)),ensure_ascii=False,indent=2))
        return dict(backup=str(backup),categories=len(layout['groups']),links=len(links),removed_cache_files=len(changes['caches']),cache_bytes=changes['cache_bytes'])
    except Exception:
        (backup/'changes.json').write_text(json.dumps(changes,ensure_ascii=False,indent=2))
        rollback(root,backup);raise

def rollback(root,backup):
    root=Path(root).resolve();backup=Path(backup).resolve();m=json.loads((backup/'changes.json').read_text())
    if Path(m['root'])!=root:raise ValueError('Rollback root mismatch')
    for name,existed in m['categories'].items():
        p=root/safe_relative(name)
        if p.exists():check_owned(p);shutil.rmtree(p)
        if existed:shutil.copytree(backup/'categories'/name,p,symlinks=True)
    if m.get('legacy_navigation'):
        legacy=root/'00_程序文件导航'
        if legacy.exists():shutil.rmtree(legacy)
        shutil.copytree(backup/'legacy-navigation',legacy,symlinks=True)
    for name,existed in m['docs'].items():
        p=root/safe_relative(name)
        if existed:shutil.copy2(backup/name,p)
        elif p.exists():p.unlink()
    if hasattr(os,'chflags'):
        for name,flags in m['flags'].items():
            p=root/safe_relative(name)
            if p.exists():os.chflags(p,flags,follow_symlinks=False)
    archive=backup/'removed-cache.zip'
    if archive.exists():
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                p=root/safe_relative(name)
                if not p.exists():p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(name))
    if (backup/STATE).exists():shutil.copy2(backup/STATE,root/STATE)
    elif (root/STATE).exists():(root/STATE).unlink()
    return {'restored_from':str(backup)}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path.cwd());parser.add_argument('--apply',action='store_true');parser.add_argument('--clean-cache',action='store_true');parser.add_argument('--rollback',type=Path);args=parser.parse_args()
    if args.rollback:result=rollback(args.root,args.rollback)
    elif args.apply:result=apply(args.root,args.clean_cache)
    else:
        plan=load_plan(args.root);result=dict(categories=[g['name'] for g in plan['groups']],entries=sum(len(g['entries']) for g in plan['groups']),cache_candidates=len(cache_candidates(args.root)))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
