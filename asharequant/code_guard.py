"""Explicit code releases in isolated Git branches. Daily jobs never publish code.
Isolation avoids accidental production writes; this is not a hostile-code sandbox.
"""
from pathlib import Path
import hashlib,json,os,re,shutil,subprocess,sys,tempfile
from .core import now,write_json,run_lock
PROTECTED=('asharequant/lifecycle.py','asharequant/iteration.py','asharequant/code_guard.py',
           'asharequant/strict_validation.py','asharequant/validation_engine.py','asharequant/decision.py')


def files(root):
    root=Path(root);out={}
    for pattern in ('asharequant/*.py','tests/test*.py','*_config.json','config.json','watchlist.csv'):
        for p in root.glob(pattern):
            if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):raise ValueError('SYMLINK_SOURCE_REJECTED')
            out[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def git(path,*args):
    return subprocess.run(['git',*args],cwd=path,check=True,capture_output=True,text=True).stdout.strip()


def location(root,name):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',name):raise ValueError('INVALID_EXPERIMENT_NAME')
    return Path(root)/'code_experiments'/name


def create(root,name):
    dest=location(root,name)
    if dest.exists():raise ValueError('EXPERIMENT_EXISTS')
    repo=dest/'branch';repo.mkdir(parents=True)
    baseline=files(root)
    for rel in baseline:
        target=repo/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(Path(root)/rel,target)
    git(repo,'init','-b','baseline');git(repo,'add','.')
    git(repo,'-c','user.name=AshareQuant','-c','user.email=local@localhost','commit','-m','Frozen production source baseline')
    commit=git(repo,'rev-parse','HEAD');git(repo,'checkout','-b','experiment/'+name)
    record=dict(name=name,created_at=now().isoformat(),base_commit=commit,baseline=baseline,branch=str(repo),status='ISOLATED')
    write_json(dest/'manifest.json',record);return record


def validate(root,name):
    dest=location(root,name);meta=json.loads((dest/'manifest.json').read_text());repo=dest/'branch';current=files(repo)
    for rel,sha in meta['baseline'].items():
        if rel.startswith('tests/') or rel in PROTECTED or not rel.endswith('.py'):
            if current.get(rel)!=sha:raise ValueError('PROTECTED_GATE_TEST_OR_CONFIG_CHANGED:'+rel)
    if set(meta['baseline'])-set(current):raise ValueError('SOURCE_DELETION_REQUIRES_MANUAL_REVIEW')
    if files(root)!=meta['baseline']:raise ValueError('PRODUCTION_CHANGED_SINCE_BRANCH')
    completed=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=repo,capture_output=True,text=True,timeout=1800,
                             env={**os.environ,'PYTHONPATH':str(repo),'PYTHONDONTWRITEBYTECODE':'1'})
    (dest/'tests.txt').write_text(completed.stdout+completed.stderr)
    passed=completed.returncode==0 and files(repo)==current and files(root)==meta['baseline']
    result=dict(at=now().isoformat(),passed=passed,exit_code=completed.returncode,source=current,baseline=meta['baseline'],test_log_sha256=hashlib.sha256((dest/'tests.txt').read_bytes()).hexdigest())
    write_json(dest/'validation.json',result);return result


def publish(root,name):
    dest=location(root,name);repo=dest/'branch';v=json.loads((dest/'validation.json').read_text())
    if not v['passed'] or v['exit_code'] or files(repo)!=v['source']:raise ValueError('UNTESTED_OR_CHANGED_CODE')
    if hashlib.sha256((dest/'tests.txt').read_bytes()).hexdigest()!=v['test_log_sha256']:raise ValueError('TEST_LOG_CHANGED')
    with run_lock(Path(root)/'logs/daily'),run_lock(Path(root)):
        if files(root)!=v['baseline']:raise ValueError('PRODUCTION_CHANGED_SINCE_TEST')
        backup=dest/'rollback'
        if backup.exists():raise ValueError('ALREADY_PUBLISHED')
        changed=[p for p,h in v['source'].items() if v['baseline'].get(p)!=h]
        for rel in changed:
            if not rel.startswith('asharequant/') or rel in PROTECTED:raise ValueError('NON_RUNTIME_OR_PROTECTED_CHANGE')
        backup.mkdir()
        for rel in changed:
            original=Path(root)/rel
            if original.exists():
                target=backup/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(original,target)
        record=dict(at=now().isoformat(),changed=changed,baseline=v['baseline'],deployed=v['source'],status='PREPARED')
        write_json(dest/'deployment.json',record)
        try:
            for rel in changed:
                target=Path(root)/rel;temp=target.with_suffix('.pending');shutil.copy2(repo/rel,temp);os.replace(temp,target)
            if files(root)!=v['source']:raise ValueError('DEPLOYMENT_HASH_MISMATCH')
            record['status']='DEPLOYED'
        except BaseException:
            _restore(root,backup,record);record['status']='ROLLED_BACK_ON_FAILURE';write_json(dest/'deployment.json',record);raise
        write_json(dest/'deployment.json',record);return record


def _restore(root,backup,record):
    for rel in record['changed']:
        target=Path(root)/rel
        if rel in record['baseline']:
            source=backup/rel
            if hashlib.sha256(source.read_bytes()).hexdigest()!=record['baseline'][rel]:raise ValueError('ROLLBACK_BACKUP_CHANGED')
            temp=target.with_suffix('.rollback');shutil.copy2(source,temp);os.replace(temp,target)
        elif target.exists():target.unlink()


def rollback(root,name):
    dest=location(root,name);record=json.loads((dest/'deployment.json').read_text())
    with run_lock(Path(root)/'logs/daily'),run_lock(Path(root)):
        if record['status']!='DEPLOYED' or files(root)!=record['deployed']:raise ValueError('ROLLBACK_WOULD_OVERWRITE_NEWER_CODE')
        # Validate all backups before restoring any file.
        for rel in record['changed']:
            if rel in record['baseline'] and hashlib.sha256((dest/'rollback'/rel).read_bytes()).hexdigest()!=record['baseline'][rel]:raise ValueError('ROLLBACK_BACKUP_CHANGED')
        _restore(root,dest/'rollback',record);record.update(status='ROLLED_BACK',rolled_back_at=now().isoformat())
        write_json(dest/'deployment.json',record);return record


def dispatch(root,request):
    actions={'create':create,'validate':validate,'publish':publish,'rollback':rollback}
    if request.get('action') not in actions:raise ValueError('UNKNOWN_CODE_ACTION')
    return actions[request['action']](Path(root),request['name'])
