"""Create a new shared profile from readonly, consistent SQLite snapshots.

Original profiles and running applications are retained. Only known application
databases, non-secret settings and capture images are copied; Key files never
enter the destination. Reports contain paths/counts/timestamps, never messages.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import ctypes
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid

from PySide6.QtCore import QCoreApplication,QLockFile


ROOT=Path(__file__).resolve().parents[1]
DATABASES=('tasks.sqlite3','task-runtime.sqlite3','knowledge.sqlite3')
IMAGE_SUFFIXES={'.png','.jpg','.jpeg','.gif','.webp','.bmp'}


class PreparationError(RuntimeError):pass


def _checked(path,root):
    path=Path(path)
    if path.is_symlink() or path.is_junction():
        raise PreparationError('Filesystem links are not followed.')
    resolved=path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved==root.resolve():
        raise PreparationError('Profile paths must stay inside the project.')
    return resolved


def _readonly(path):
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)
    connection.execute('PRAGMA query_only=ON')
    return connection


def _time_key(value):
    if not value:return float('-inf')
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (AttributeError,ValueError,OverflowError):
        raise PreparationError('A conversation timestamp is invalid; original profiles were retained.') from None


def _process_active(pid):
    if not pid:return None
    if sys.platform!='win32':return Path('/proc',str(pid)).exists()
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)];kernel.GetExitCodeProcess.restype=w.BOOL
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    handle=kernel.OpenProcess(0x1000,False,pid)  # Read-only query; never terminate or signal.
    if not handle:return None if ctypes.get_last_error()==5 else False
    try:
        code=w.DWORD()
        return code.value==259 if kernel.GetExitCodeProcess(handle,ctypes.byref(code)) else None
    finally:kernel.CloseHandle(handle)


def _lock_status(directory):
    path=directory/'session.lock';pid=None
    if path.exists():
        try:
            info=QLockFile(str(path)).getLockInfo()
            if len(info)==3 and type(info[0]) is int and info[0]>0:pid=info[0]
            elif len(info)==4 and info[0]:pid=int(info[1])
        except (TypeError,ValueError,IndexError):pass
    return {'path':str(path),'exists':path.exists(),'pid':pid,'active':_process_active(pid)}


def _key_status(path):
    result={'path':str(path),'exists':path.is_file(),'has_non_comment_line':False}
    if not result['exists']:return result
    with path.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:return result
    for encoding in (('utf-16',) if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else ('utf-8-sig','gb18030')):
        try:text=raw.decode(encoding);break
        except UnicodeError:continue
    else:return result
    result['has_non_comment_line']=any(line.strip() and not line.lstrip().startswith(('#',';','//'))
                                       for line in text.splitlines())
    return result


def _summary(directory,database=None):
    path=database or directory/'conversation.sqlite3'
    count,newest=0,None
    if path.is_file():
        with closing(_readonly(path)) as connection:
            count,newest=connection.execute('SELECT COUNT(*),MAX(created_at) FROM conversation_messages').fetchone()
    return {'path':str(directory),'conversation_count':count,'max_created_at':newest,
            'source_session_lock':_lock_status(directory)}


def discover(root=ROOT):
    root=Path(root).resolve()
    paths=[root/'data',*sorted((root/'release').glob('ShuCompanion-*/data'))]
    return [_checked(path,root) for path in paths if path.is_dir()]


def _latest(summaries):
    candidates=[item for item in summaries if item['conversation_count']]
    if not candidates:raise PreparationError('No conversation history was found.')
    return max(candidates,key=lambda item:(_time_key(item['max_created_at']),
        item['source_session_lock'].get('active') is True,item['conversation_count']))


def _backup(source,target):
    deadline=time.monotonic()+30
    def progress(_status,_remaining,_total):
        if time.monotonic()>deadline:raise PreparationError('SQLite backup timed out; no destination was promoted.')
    with closing(_readonly(source)) as original,closing(sqlite3.connect(target)) as snapshot:
        original.execute('BEGIN')
        if original.execute('PRAGMA quick_check').fetchone()!=('ok',):
            raise PreparationError('Source database integrity check failed.')
        original.backup(snapshot,pages=128,progress=progress,sleep=.05)
        snapshot.execute('PRAGMA journal_mode=DELETE')
        if snapshot.execute('PRAGMA quick_check').fetchone()!=('ok',):
            raise PreparationError('Snapshot database integrity check failed.')


def _table_counts(path):
    with closing(_readonly(path)) as connection:
        names=connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        return {name:connection.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
                for (name,) in names}


def _merge_conversations(snapshots,target):
    seen=set();records=[];input_count=0
    for path in snapshots:
        with closing(_readonly(path)) as connection:
            for role,content,created_at in connection.execute('SELECT role,content,created_at FROM conversation_messages ORDER BY id'):
                input_count+=1;record=(role,content,created_at)
                if record not in seen:
                    if role not in {'user','assistant','system'} or not isinstance(content,str) or not content.strip():
                        raise PreparationError('A conversation record is invalid; no history was discarded.')
                    _time_key(created_at);seen.add(record);records.append(record)
    records.sort(key=lambda record:_time_key(record[2]))
    with closing(sqlite3.connect(target)) as output:
        output.execute('''CREATE TABLE conversation_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content TEXT NOT NULL CHECK(length(content)>0),created_at TEXT NOT NULL)''')
        output.executemany('INSERT INTO conversation_messages(role,content,created_at) VALUES(?,?,?)',records)
        output.commit()
    return {'input_rows':input_count,'unique_rows':len(records),'duplicate_rows':input_count-len(records)}


def _settings(source,target):
    if not source.is_file():return 0
    value=json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict):raise PreparationError('Source settings are invalid.')
    def clean(item):
        if isinstance(item,dict):return {key:clean(child) for key,child in item.items()
            if str(key).casefold() not in {'api_key','deepseek_api_key','authorization','password','access_token'}}
        if isinstance(item,list):return [clean(child) for child in item]
        return item
    target.write_text(json.dumps(clean(value),ensure_ascii=False,indent=2),encoding='utf-8')
    return 1


def _captures(source,destination,root):
    if not source.is_dir():return 0
    _checked(source,root);count=0;destination.mkdir(parents=True,exist_ok=True)
    for path in source.iterdir():
        path=_checked(path,root);copied=destination/path.name
        if path.is_dir():count+=_captures(path,copied,root)
        elif path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES:
            before=path.stat();shutil.copy2(path,copied);after=path.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                raise PreparationError('A capture changed during copying; no destination was promoted.')
            count+=1
    return count


def _require_empty(destination):
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise PreparationError('Shared destination is already nonempty; nothing was overwritten.')


def prepare(root=ROOT,*,dry_run=False):
    root=Path(root).resolve();destination=_checked(root/'user-data',root)
    profiles=discover(root);summaries=[_summary(path) for path in profiles]
    latest=_latest(summaries);latest_path=Path(latest['path'])
    keys=[_key_status(_checked(latest_path.parent/'DeepSeek_API_Key.txt',root)),
          _key_status(_checked(root/'DeepSeek_API_Key.txt',root))]
    selected_key=next((item['path'] for item in keys if item['has_non_comment_line']),keys[0]['path'])
    report={'profiles':summaries,'latest_profile':str(latest_path),'destination':str(destination),
            'key_files':keys,'selected_key_file':selected_key,'dry_run':dry_run}
    if dry_run:return report
    _require_empty(destination)
    artifacts=_checked(root/'artifacts',root);artifacts.mkdir(exist_ok=True)
    lock=QLockFile(str(artifacts/'shared-profile-prepare.lock'))
    if not lock.tryLock(100):raise PreparationError('Another shared-profile preparation is running.')
    try:
        _require_empty(destination)
        staging=_checked(artifacts/('shared-profile-'+uuid.uuid4().hex),root)
        snapshots=staging/'readonly-snapshots';data=staging/'data'
        snapshots.mkdir(parents=True);data.mkdir()
        profile_snapshots={};frozen_summaries=[]
        for index,path in enumerate(profiles):
            original=_checked(path/'conversation.sqlite3',root)
            if not original.is_file():continue
            snapshot=snapshots/f'profile-{index}-conversation.sqlite3';_backup(original,snapshot)
            profile_snapshots[path]=snapshot;frozen_summaries.append(_summary(path,snapshot))
        latest=_latest(frozen_summaries);latest_path=Path(latest['path'])
        if not (latest_path/'tasks.sqlite3').is_file():raise PreparationError('The latest profile has no task database.')
        database_counts={}
        for name in DATABASES:
            source=_checked(latest_path/name,root)
            if source.is_file():
                _backup(source,data/name);database_counts[name]=_table_counts(data/name)
        merged=_merge_conversations(profile_snapshots.values(),data/'conversation.sqlite3')
        settings_count=_settings(_checked(latest_path/'settings.json',root),data/'settings.json')
        capture_copies=[]
        for index,path in enumerate(profiles):
            relative=Path('captures') if path==latest_path else Path('profile-captures')/f'profile-{index}'
            count=_captures(path/'captures',data/relative,root)
            if count:capture_copies.append({'source':str(path/'captures'),
                'destination':str(destination/relative),'file_count':count})
        # Election is based on the committed conversation snapshots, not mtimes.
        keys=[_key_status(_checked(latest_path.parent/'DeepSeek_API_Key.txt',root)),
              _key_status(_checked(root/'DeepSeek_API_Key.txt',root))]
        selected_key=next((item['path'] for item in keys if item['has_non_comment_line']),keys[0]['path'])
        report.update(profiles=frozen_summaries,latest_profile=str(latest_path),key_files=keys,
            selected_key_file=selected_key,conversation_merge=merged,database_rows=database_counts,
            settings_files=settings_count,capture_copies=capture_copies,snapshot_directory=str(snapshots),
            snapshot_created_at=datetime.now(timezone.utc).isoformat())
        (data/'profile-preparation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        _require_empty(destination)
        if destination.exists():destination.rmdir()  # Verified empty, nonrecursive.
        data.rename(destination)
        return report
    finally:lock.unlock()


def main():
    parser=argparse.ArgumentParser(description='Prepare a new shared local profile without overwriting originals')
    parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
    app=QCoreApplication.instance() or QCoreApplication([])
    try:report=prepare(dry_run=args.dry_run)
    except (PreparationError,OSError,sqlite3.Error,ValueError,TypeError) as error:
        print(json.dumps({'preparation_error':str(error) if isinstance(error,PreparationError) else type(error).__name__}))
        return 1
    print(json.dumps(report,ensure_ascii=True,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
