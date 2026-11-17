"""Read-only evidence for pinned MAA GUI queue inputs.

No GUI, input, API, process-memory or account access. A queue snapshot contains
only the selected configuration's whitelist and copied copilot bodies. Native
process identities and screenshot/GUI facts remain separate caller evidence.
An input receipt is per-input, not the aggregate QueuePlan completion receipt.
"""
from __future__ import annotations

import base64
from collections import Counter
import copy
import ctypes
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import stat
import time
import uuid
from datetime import datetime

from . import maa_load_receipt as single

PINNED_VERSION='v6.21.4+52d0b2acaca116585b02e1e6a9da303460f81a24'
# Both immutable revisions have the same Copilot view/model/config schema.
# The scoped source comparison is preserved in the multi-job acceptance audit.
SUPPORTED_VERSIONS=frozenset({PINNED_VERSION,
    'v6.22.0+901e01c600f4895ac4e7e48a9b471fc9f52af301'})
_MAA_EXECUTABLE=Path(r'D:\MAA\MAA-v6.21.4-win-x64\MAA.exe')
_MAA_ROOT=_MAA_EXECUTABLE.parent
_MAX_BYTES=8*1024*1024
_MAX_ROWS=1000
_MAX_SNAPSHOT_BODY_BYTES=64*1024*1024
# This separates two read-only observations. It is not evidence that MAA has
# finished parsing; the importer must also verify parse-idle/no in-flight input.
_MIN_ABSENCE_INTERVAL=.1
_ROW_FIELDS=('Name','FilePath','CopilotId','IsRaid','IsChecked','Index','IsNavNameOverride')
_LOWER_FIELDS=('name','file_path','copilot_id','is_raid','is_checked','index','is_nav_name_override')
_HASH=re.compile(r'[0-9a-f]{64}\Z')
_CODE=re.compile(r'prts://([1-9][0-9]{0,11})\Z')


class MaaQueueReceiptError(RuntimeError):
    def __init__(self,code):
        self.code=code if isinstance(code,str) and re.fullmatch(r'[a-z_]{1,64}',code) else 'queue_receipt_invalid'
        super().__init__('MAA 队列证据未核实（'+self.code+'）。')


def _bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _hash(raw):return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('duplicate key')
            result[key]=value
        return result
    return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=pairs,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))


def _text(value,limit=1024):
    return isinstance(value,str) and 0<len(value)<=limit and not any(ord(c)<32 for c in value)


def project_queue_config(raw:bytes)->dict:
    """Only PascalCase System.Text.Json schema from the pinned GUI is supported.

    Core defaults are mandatory: absent booleans never become checked/false by
    inference. Optional locator fields stay None when absent. No alias adapter.
    """
    try:
        if type(raw) is not bytes or not 0<len(raw)<=_MAX_BYTES:raise ValueError()
        root=_json(raw)
        if not isinstance(root,dict) or not _text(root.get('Current'),256):raise ValueError()
        current=root['Current']; configuration=root['Configurations'][current]
        rows=configuration['Copilot']['TaskList']
        if not isinstance(rows,list) or len(rows)>_MAX_ROWS:raise ValueError()
        result=[]
        for row in rows:
            if not isinstance(row,dict):raise ValueError()
            # Reject old Newtonsoft spellings rather than quietly ignoring one
            # half of a contradictory alias pair.
            if set(row)&set(_LOWER_FIELDS):raise ValueError()
            if (not _text(row.get('Name')) or not _text(row.get('FilePath'),4096)
                    or type(row.get('CopilotId')) is not int or not 0<=row['CopilotId']<=2**31-1
                    or type(row.get('IsRaid')) is not bool or type(row.get('IsChecked')) is not bool):
                raise ValueError()
            index=row.get('Index'); nav=row.get('IsNavNameOverride')
            if index is not None and (type(index) is not int or not 0<=index<=1000000):raise ValueError()
            if nav is not None and type(nav) is not bool:raise ValueError()
            result.append({out:row.get(key) for key,out in zip(_ROW_FIELDS,_LOWER_FIELDS)})
        return {'current':current,'rows':result}
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError,RecursionError,OverflowError):
        raise MaaQueueReceiptError('config_schema_unverified') from None


def canonical_copilot_document(document:dict)->dict:
    """Use the existing conservative, tested single-load normalization rules."""
    try:return single._canonical(document)
    except single.MaaLoadReceiptError as error:raise MaaQueueReceiptError(error.code) from None


def expected_loaded_copilot_document(document:dict)->dict:
    """Predict audited elite fills and location-over-name action corrections.

    Observed queue bodies retain their own canonical bytes. New code input
    must additionally prove the exact normalization messages in its load log.
    """
    try:
        return single._load_metadata(canonical_copilot_document(document))[0]
    except single.MaaLoadReceiptError as error:
        raise MaaQueueReceiptError(error.code) from None


def expected_queue_copilot_id(source_document:dict,mystery_code:str)->int:
    """Derive the pinned GUI's saved ID from the actual station document.

    CopilotViewModel.ParseCopilotAsync clears the ID passed to the queue when
    it corrects an explicitly insufficient elite requirement or removes a
    redundant action name (is_corrected). Filling an absent requirement does
    not clear it. Other corrections remain unsupported.
    Source: immutable 901e01c.../src/MaaWpfGui/ViewModels/UI/
    CopilotViewModel.cs, lines 1367-1371 and 1439-1442.
    """
    if not isinstance(mystery_code,str) or not _CODE.fullmatch(mystery_code):
        raise MaaQueueReceiptError('target_invalid')
    try:
        _,_,_,corrected=single._load_metadata(canonical_copilot_document(source_document))
    except single.MaaLoadReceiptError as error:
        raise MaaQueueReceiptError(error.code) from None
    if corrected:
        return 0
    return int(_CODE.fullmatch(mystery_code).group(1))


def _process(process):
    if (not isinstance(process,dict) or any(type(process.get(k)) is not int or process[k]<=0
            for k in ('hwnd','pid','creation_time')) or not isinstance(process.get('executable'),str)
            or ntpath.normcase(ntpath.normpath(process['executable']))!=ntpath.normcase(str(_MAA_EXECUTABLE))):
        raise MaaQueueReceiptError('binding_changed')
    return {k:process[k] for k in ('hwnd','pid','creation_time','executable')}


def _process_matches(process):
    from .maa_visual_native import discover_maa_windows
    try:
        return any(all(item.get(k)==process[k] for k in ('hwnd','pid','creation_time','executable'))
                   for item in discover_maa_windows())
    except Exception:return False


def _no_links(path):
    path=Path(path).absolute()
    for parent in (path,*path.parents):
        try:info=parent.lstat()
        except FileNotFoundError:continue
        except OSError:raise MaaQueueReceiptError('path_unreadable') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise MaaQueueReceiptError('reparse_path_rejected')
    return path


def _state(value):
    if not value.st_ino:raise MaaQueueReceiptError('file_identity_unavailable')
    return {'file_identity':{'device':value.st_dev,'inode':value.st_ino},
            'size':value.st_size,'mtime_ns':value.st_mtime_ns}


def _state_at(path):
    try:return _state(_no_links(path).stat())
    except OSError:raise MaaQueueReceiptError('file_changed_during_read') from None


def _read_stable(path,maximum=_MAX_BYTES):
    try:
        path=_no_links(path);before=_state(path.stat())
        if not 0<before['size']<=maximum:raise MaaQueueReceiptError('file_size_limit')
        with path.open('rb') as stream:
            opened=_state(os.fstat(stream.fileno()));raw=stream.read(maximum+1)
            after=_state(os.fstat(stream.fileno()))
        if before!=opened or opened!=after or after!=_state(_no_links(path).stat()) or len(raw)!=after['size']:
            raise MaaQueueReceiptError('file_changed_during_read')
        return raw,after
    except OSError:raise MaaQueueReceiptError('file_unreadable') from None


def _product_version(path):
    """Read the fixed DLL's file version resource, not running process memory."""
    if os.name!='nt':raise MaaQueueReceiptError('version_unreadable')
    try:
        version=ctypes.WinDLL('version',use_last_error=True)
        from ctypes import wintypes as w
        version.GetFileVersionInfoSizeW.argtypes=[w.LPCWSTR,ctypes.POINTER(w.DWORD)]
        version.GetFileVersionInfoSizeW.restype=w.DWORD
        version.GetFileVersionInfoW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p]
        version.GetFileVersionInfoW.restype=w.BOOL
        version.VerQueryValueW.argtypes=[ctypes.c_void_p,w.LPCWSTR,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(w.UINT)]
        version.VerQueryValueW.restype=w.BOOL
        dummy=w.DWORD();size=version.GetFileVersionInfoSizeW(str(path),ctypes.byref(dummy))
        if not 0<size<=4*1024*1024:raise ValueError()
        data=ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path),0,size,data):raise ValueError()
        pointer=ctypes.c_void_p();length=w.UINT()
        if not version.VerQueryValueW(data,r'\VarFileInfo\Translation',ctypes.byref(pointer),ctypes.byref(length)) or length.value<4:raise ValueError()
        words=ctypes.cast(pointer,ctypes.POINTER(ctypes.c_ushort))
        query=f'\\StringFileInfo\\{words[0]:04x}{words[1]:04x}\\ProductVersion'
        if not version.VerQueryValueW(data,query,ctypes.byref(pointer),ctypes.byref(length)) or length.value>256:raise ValueError()
        return ctypes.wstring_at(pointer,length.value).rstrip('\0')
    except (OSError,ValueError,AttributeError,TypeError):raise MaaQueueReceiptError('version_unreadable') from None


def discover_queue_binding(process:dict)->dict:
    process=_process(process)
    _no_links(_MAA_EXECUTABLE);dll=_no_links(_MAA_ROOT/'MAA.dll')
    if not _process_matches(process):raise MaaQueueReceiptError('binding_changed')
    version=_product_version(dll)
    if version not in SUPPORTED_VERSIONS:raise MaaQueueReceiptError('version_mismatch')
    config=_no_links(_MAA_ROOT/'config/gui.new.json')
    raw,_=_read_stable(config); projection=project_queue_config(raw)
    return {'process':process,'current_config':projection['current'],
            'config_path':str(config.resolve()),'maa_version':version}


def _binding(binding,*,live=False):
    try:
        expected={'process':_process(binding['process']),'current_config':binding['current_config'],
            'config_path':str((_MAA_ROOT/'config/gui.new.json').resolve()),'maa_version':binding['maa_version']}
        if (binding!=expected or binding['maa_version'] not in SUPPORTED_VERSIONS
                or not _text(binding['current_config'],256)):raise ValueError()
        if live and discover_queue_binding(binding['process'])!=binding:raise MaaQueueReceiptError('binding_changed')
        return expected
    except (KeyError,TypeError,ValueError):raise MaaQueueReceiptError('binding_changed') from None


def _publish(directory,kind,value):
    directory=_no_links(directory).resolve()
    if directory==_MAA_ROOT.resolve() or directory.is_relative_to(_MAA_ROOT.resolve()):
        raise MaaQueueReceiptError('evidence_directory_invalid')
    try:directory.mkdir(parents=True,exist_ok=True)
    except OSError:raise MaaQueueReceiptError('evidence_unwritable') from None
    raw=_bytes(value);digest=_hash(raw)
    path=directory/(kind+'-'+digest+'.json');_no_links(path)
    try:
        with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    except FileExistsError:
        try:
            if _no_links(path).read_bytes()!=raw:raise MaaQueueReceiptError('evidence_changed') from None
        except OSError:raise MaaQueueReceiptError('evidence_unwritable') from None
    except OSError:raise MaaQueueReceiptError('evidence_unwritable') from None
    return {'path':str(path),'sha256':digest}


def _body_lexical_path(value):
    path=Path(value)
    if '..' in path.parts:raise MaaQueueReceiptError('body_path_invalid')
    if not path.is_absolute():path=_MAA_ROOT/path
    allowed=(_MAA_ROOT/'config/copilot').absolute()
    if path.suffix.casefold()!='.json' or not path.is_relative_to(allowed):raise MaaQueueReceiptError('body_path_invalid')
    return path


def _body_path(value):
    # Classify out-of-scope paths before any stat, including UNC paths.
    path=_body_lexical_path(value)
    _no_links(path)
    return _body_lexical_path(path.resolve())


def _row_identity(copilot_id,canonical_path,is_raid,body_sha256):
    if type(copilot_id) is not int or copilot_id<0 or type(is_raid) is not bool or not _HASH.fullmatch(body_sha256):
        raise MaaQueueReceiptError('queue_row_identity_invalid')
    return _hash(_bytes([copilot_id,canonical_path.casefold(),is_raid,body_sha256]))


def capture_queue_snapshot(binding,directory,*,screenshot_refs,visible_complete):
    _binding(binding,live=True)
    if type(visible_complete) is not bool:raise MaaQueueReceiptError('snapshot_invalid')
    frames=_frames(screenshot_refs,live=True)
    frame_observations=[]
    for reference in frames:
        raw_frame,frame_state=_read_stable(reference['path'])
        if _hash(raw_frame)!=reference['sha256']:raise MaaQueueReceiptError('screenshot_changed')
        frame_observations.append({'ref':copy.deepcopy(reference),'file_state':frame_state})
    raw,config_state=_read_stable(binding['config_path']);projection=project_queue_config(raw)
    if projection['current']!=binding['current_config']:raise MaaQueueReceiptError('binding_changed')
    rows=[];proofs={};states=[];total_body_bytes=0
    for ordinal,item in enumerate(projection['rows']):
        row={**item,'ordinal':ordinal,'body_ref':None,'body_sha256':None,'semantic_sha256':None,
             'stage_id':None,'verification':'opaque','identity':'opaque:'+_hash(_bytes(item))}
        try:
            path=_body_path(item['file_path']);body,body_state=_read_stable(path)
            total_body_bytes+=len(body)
            if total_body_bytes>_MAX_SNAPSHOT_BODY_BYTES:
                raise MaaQueueReceiptError('snapshot_body_limit')
            document=_json(body);canonical=canonical_copilot_document(document)
            difficulty=canonical.get('difficulty',1)
            if type(difficulty) is not int or difficulty not in (1,2,3) or not difficulty&(2 if item['is_raid'] else 1):
                raise MaaQueueReceiptError('body_mode_mismatch')
            evidence={'schema':'maa_queue_body/v1','bytes_b64':base64.b64encode(body).decode('ascii'),'sha256':_hash(body)}
            reference=_publish(directory,'maa-queue-body',evidence)
            proofs[reference['sha256']]={**evidence,'ref':reference}
            row.update(file_path=str(path),body_ref=reference,body_sha256=_hash(body),
                semantic_sha256=_hash(_bytes(canonical)),stage_id=canonical['stage_name'],verification='verified')
            row['identity']=_row_identity(item['copilot_id'],str(path),item['is_raid'],row['body_sha256'])
            states.append((path,body_state))
        except MaaQueueReceiptError as error:
            if error.code=='snapshot_body_limit':raise
        except (ValueError,TypeError,UnicodeError,RecursionError,OSError):
            # Preserve unrecognized/external user rows, but never use them as
            # absence, reuse or deletion authority.
            pass
        rows.append(row)
    after,after_state=_read_stable(binding['config_path'])
    if config_state!=after_state or project_queue_config(after)!=projection:
        raise MaaQueueReceiptError('config_changed_during_read')
    if any(_state_at(path)!=state for path,state in states):
        raise MaaQueueReceiptError('body_changed_during_read')
    _binding(binding,live=True)
    result={'schema':'maa_queue_snapshot/v1','binding':copy.deepcopy(binding),'captured_at':time.time(),
        'config_state':config_state,'projection':projection,'projection_sha256':_hash(_bytes(projection)),
        'rows':rows,'body_evidence':list(proofs.values()),'stable':True,
        'screenshot_refs':frames,'screenshot_observations':frame_observations,
        'visible_complete':visible_complete}
    result['evidence_ref']=_publish(directory,'maa-queue-snapshot',result)
    return result


def _frames(refs,*,live=False):
    if not isinstance(refs,list) or len(refs)>100:raise MaaQueueReceiptError('screenshot_invalid')
    result=[]
    for ref in refs:
        if (not isinstance(ref,dict) or set(ref)!={'path','sha256'} or not _text(ref['path'],4096)
                or not isinstance(ref['sha256'],str) or not _HASH.fullmatch(ref['sha256'])):
            raise MaaQueueReceiptError('screenshot_invalid')
        if live:
            raw,_=_read_stable(ref['path'])
            if _hash(raw)!=ref['sha256']:raise MaaQueueReceiptError('screenshot_changed')
        result.append(copy.deepcopy(ref))
    return result


def _check_snapshot(snapshot):
    try:
        if snapshot.get('schema')!='maa_queue_snapshot/v1' or snapshot.get('stable') is not True:raise ValueError()
        _binding(snapshot['binding']);projection=snapshot['projection']
        if (type(snapshot['captured_at']) not in (int,float)
                or not math.isfinite(snapshot['captured_at']) or snapshot['captured_at']<=0):raise ValueError()
        if projection['current']!=snapshot['binding']['current_config'] or snapshot['projection_sha256']!=_hash(_bytes(projection)):raise ValueError()
        reconstructed=project_queue_config(_bytes({'Current':projection['current'],
            'Configurations':{projection['current']:{'Copilot':{'TaskList':[
                {key:row[out] for key,out in zip(_ROW_FIELDS,_LOWER_FIELDS) if row[out] is not None}
                for row in projection['rows']]}}}}))
        if reconstructed!=projection or len(snapshot['rows'])!=len(projection['rows']):raise ValueError()
        _frames(snapshot['screenshot_refs'])
        observations=snapshot.get('screenshot_observations')
        if observations is not None:
            if not isinstance(observations,list) or len(observations)!=len(snapshot['screenshot_refs']):raise ValueError()
            for reference,observation in zip(snapshot['screenshot_refs'],observations):
                if observation['ref']!=reference:raise ValueError()
                state=observation['file_state'];identity=state['file_identity']
                if (type(identity['device']) is not int or identity['device']<0
                        or type(identity['inode']) is not int or identity['inode']<=0
                        or type(state['size']) is not int or not 0<state['size']<=_MAX_BYTES
                        or type(state['mtime_ns']) is not int or state['mtime_ns']<=0):raise ValueError()
        proof_by_hash={}
        if len(snapshot['body_evidence'])>_MAX_ROWS:raise ValueError()
        total_body_bytes=0
        for proof in snapshot['body_evidence']:
            if len(proof['bytes_b64'])>4*((_MAX_BYTES+2)//3):raise ValueError()
            body=base64.b64decode(proof['bytes_b64'],validate=True)
            total_body_bytes+=len(body)
            if total_body_bytes>_MAX_SNAPSHOT_BODY_BYTES:raise ValueError()
            if not 0<len(body)<=_MAX_BYTES or _hash(body)!=proof['sha256']:raise ValueError()
            payload={k:proof[k] for k in ('schema','bytes_b64','sha256')}
            if proof['schema']!='maa_queue_body/v1' or proof['ref']['sha256']!=_hash(_bytes(payload)):raise ValueError()
            proof_by_hash[proof['ref']['sha256']]=(proof,canonical_copilot_document(_json(body)))
        for ordinal,(row,projected) in enumerate(zip(snapshot['rows'],projection['rows'])):
            if row['ordinal']!=ordinal or any(row[k]!=projected[k] for k in _LOWER_FIELDS if k!='file_path'):raise ValueError()
            if row['verification']=='opaque':
                if any(row[k] is not None for k in ('body_ref','body_sha256','semantic_sha256','stage_id')):raise ValueError()
                if row['identity']!='opaque:'+_hash(_bytes(projected)):raise ValueError()
                continue
            if row['verification']!='verified':raise ValueError()
            proof,canonical=proof_by_hash[row['body_ref']['sha256']]
            if row['body_ref']!=proof['ref'] or row['body_sha256']!=proof['sha256']:raise ValueError()
            if row['semantic_sha256']!=_hash(_bytes(canonical)) or row['stage_id']!=canonical['stage_name']:raise ValueError()
            original=_body_lexical_path(projected['file_path'])
            if ntpath.normcase(str(original))!=ntpath.normcase(row['file_path']):raise ValueError()
            mode=canonical.get('difficulty',1)
            if type(mode) is not int or mode not in (1,2,3) or not mode&(2 if row['is_raid'] else 1):raise ValueError()
            if row['identity']!=_row_identity(row['copilot_id'],row['file_path'],row['is_raid'],row['body_sha256']):raise ValueError()
        evidence=dict(snapshot);reference=evidence.pop('evidence_ref')
        if reference['sha256']!=_hash(_bytes(evidence)):raise ValueError()
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError,OverflowError,RecursionError):
        raise MaaQueueReceiptError('snapshot_invalid') from None


def _target(target,source=None):
    value=copy.deepcopy(target)
    if (not isinstance(value,dict) or not _text(value.get('target_key')) or not _text(value.get('stage_id'))
            or type(value.get('difficulty')) is not int or value['difficulty'] not in (1,2)
            or not isinstance(value.get('mystery_code'),str) or not _CODE.fullmatch(value['mystery_code'])):
        raise MaaQueueReceiptError('target_invalid')
    station_id=int(_CODE.fullmatch(value['mystery_code']).group(1))
    expected_id=value.get('expected_copilot_id',station_id)
    if type(expected_id) is not int or expected_id not in (0,station_id):
        raise MaaQueueReceiptError('target_invalid')
    if source is not None:
        canonical=canonical_copilot_document(source);mode=canonical.get('difficulty',1)
        if (canonical['stage_name']!=value['stage_id'] or type(mode) is not int or mode not in (1,2,3)
                or not mode&value['difficulty'] or value.get('source_difficulty',mode)!=mode):
            raise MaaQueueReceiptError('source_binding_mismatch')
        derived_id=expected_queue_copilot_id(source,value['mystery_code'])
        if 'expected_copilot_id' in value and expected_id!=derived_id:
            raise MaaQueueReceiptError('source_binding_mismatch')
        # Preserve old noncorrected receipt shapes; a corrected source always
        # carries the derived identity so a missing flag cannot mask its ID.
        if derived_id==0:
            value['expected_copilot_id']=0
        value['semantic_sha256']=_hash(_bytes(expected_loaded_copilot_document(source)))
    return value


def row_matches_target(row,target):
    """Match a validated plan target against the complete observed body.

    The preparation/completion and each input receipt separately derive the
    saved ID from the referenced source document. A caller-supplied zero flag
    is never an alternative to those boundary checks. Baseline zero-ID reuse
    establishes normalized content equivalence, not historical station origin
    and never grants ownership of an existing row.
    """
    target=_target(target)
    semantic=target.get('semantic_sha256') or target.get('source_semantic_sha256')
    return (row.get('verification')=='verified' and row.get('stage_id')==target['stage_id']
        and row.get('is_raid') is (target['difficulty']==2) and row.get('is_checked') is True
        and row.get('semantic_sha256')==semantic
        and row.get('copilot_id')==target.get('expected_copilot_id',int(_CODE.fullmatch(target['mystery_code']).group(1))))


def _frame_advanced(baseline,current,*,not_before=None):
    """Require a separately captured file, even when the pixel hash is identical."""
    old_observations=baseline.get('screenshot_observations')
    observations=current.get('screenshot_observations')
    if not old_observations or not observations:return False
    old_paths={ntpath.normcase(item['ref']['path']) for item in old_observations}
    old_identities={(item['file_state']['file_identity']['device'],
                     item['file_state']['file_identity']['inode']) for item in old_observations}
    started_at=baseline['captured_at']
    if not_before is not None:
        started_at=max(started_at,not_before)
    started_ns=int(started_at*1e9)
    captured_ns=int(current['captured_at']*1e9)
    for item in observations:
        file_state=item['file_state']
        identity=file_state['file_identity']
        path=ntpath.normcase(item['ref']['path'])
        key=(identity['device'],identity['inode'])
        if path in old_paths or key in old_identities or not started_ns<file_state['mtime_ns']<=captured_ns:
            return False
        old_paths.add(path)
        old_identities.add(key)
    return True


def _absence_observed(baseline,current):
    """Absence needs two independent observations; reuse of a present row does not.

    A new native screenshot may have identical pixels. Its Ref and file time
    must nevertheless belong to the later observation. Screenshot/process/
    geometry association and parsed-idle are additional Task 5/6 gates; this
    result only states observed absence and never authorizes a GUI input.
    """
    return (current['evidence_ref']!=baseline['evidence_ref']
        and current['captured_at']-baseline['captured_at']>=_MIN_ABSENCE_INTERVAL
        and _frame_advanced(baseline,current))


def diff_queue(baseline,current,targets,owned_rows):
    _check_snapshot(baseline);_check_snapshot(current)
    if baseline['binding']!=current['binding']:raise MaaQueueReceiptError('binding_changed')
    old=baseline['rows'];new=current['rows']; counts=Counter(row['identity'] for row in new)
    result={'preserved':[],'reusable':[],'missing':[],'conflicts':[],'extra_owned':[],'external_changes':[]}
    cursor=0;used=set()
    for original in old:
        found=next((i for i in range(cursor,len(new)) if new[i]['identity']==original['identity']),None)
        if found is None:result['external_changes'].append({'code':'baseline_missing_or_reordered','identity':original['identity']});continue
        cursor=found+1;used.add(found);result['preserved'].append(original['identity'])
        if new[found]['is_checked']!=original['is_checked']:
            result['external_changes'].append({'code':'baseline_checked_changed','identity':original['identity']})
        if new[found]['is_nav_name_override']!=original['is_nav_name_override']:
            result['external_changes'].append({'code':'baseline_navigation_changed','identity':original['identity']})
    usable=(baseline['visible_complete'] is True and current['visible_complete'] is True
            and bool(baseline['screenshot_refs']) and bool(current['screenshot_refs']))
    target_matches=set();related_additions=set()
    for target in targets:
        target=_target(target);key=target['target_key']
        candidates=[row for row in new if row['verification']=='verified' and row['stage_id']==target['stage_id']
                    and row['is_raid']==(target['difficulty']==2)]
        opaque=any(row['verification']=='opaque' for row in new)
        semantic=target.get('semantic_sha256') or target.get('source_semantic_sha256')
        expected_id=target.get('expected_copilot_id',int(_CODE.fullmatch(target['mystery_code']).group(1)))
        related_additions.update(row['identity'] for row in new if row['verification']=='verified'
            and row['stage_id']==target['stage_id']
            and row['copilot_id']==expected_id
            and row['semantic_sha256']==semantic
            and (row['is_raid']==(target['difficulty']==2) or target.get('source_difficulty')==3))
        exact=[row for row in candidates if row_matches_target(row,target)]
        if not usable:
            result['conflicts'].append({'target_key':key,'code':'queue_visibility_incomplete'})
        elif opaque:
            result['conflicts'].append({'target_key':key,'code':'queue_opaque_rows'})
        elif len(candidates)==1 and len(exact)==1 and counts[exact[0]['identity']]==1:
            result['reusable'].append(key);target_matches.add(exact[0]['identity'])
        elif candidates:
            result['conflicts'].append({'target_key':key,'code':'queue_target_conflict'})
        elif _absence_observed(baseline,current):result['missing'].append(key)
        else:result['conflicts'].append({'target_key':key,'code':'queue_absence_unverified'})
    old_ids={row['identity'] for row in old}
    attributable=set(related_additions)
    for item in owned_rows:
        identity=item.get('identity') or item.get('row_identity')
        if (item.get('owned') is True and item.get('operation_ids') and identity not in old_ids
                and counts[identity]==1):
            attributable.add(identity)
            if identity not in target_matches:result['extra_owned'].append(identity)
    for index,row in enumerate(new):
        if index not in used and row['identity'] not in attributable:
            result['external_changes'].append({'code':'unattributed_row_added','identity':row['identity']})
    return result


def _log_baseline():
    path=_no_links(_MAA_ROOT/'debug/gui.log')
    try:
        with path.open('rb') as stream:
            before=_state(os.fstat(stream.fileno()));digest=single._prefix_hash(stream,before['size'])
            stream.seek(max(0,before['size']-1));last=stream.read(1)
        if before!=_state(path.stat()):raise MaaQueueReceiptError('log_changed')
        return {**before,'offset':before['size'],'prefix_sha256':digest,
                'ended_with_newline':not before['size'] or last==b'\n'}
    except OSError:raise MaaQueueReceiptError('log_unreadable') from None
    except single.MaaLoadReceiptError as error:raise MaaQueueReceiptError(error.code) from None


def capture_queue_baseline(binding,target,source_document,directory,snapshot):
    _binding(binding,live=True);_check_snapshot(snapshot)
    if snapshot['binding']!=binding:raise MaaQueueReceiptError('binding_changed')
    target=_target(target,source_document)
    if time.time()-snapshot['captured_at']>15 or time.time()<snapshot['captured_at']:raise MaaQueueReceiptError('snapshot_expired')
    raw,_=_read_stable(binding['config_path'])
    if project_queue_config(raw)!=snapshot['projection']:raise MaaQueueReceiptError('baseline_snapshot_changed')
    baseline={'schema':'maa_queue_input_baseline/v1','input_id':uuid.uuid4().hex,
        'binding':copy.deepcopy(binding),'target':target,'source_document':copy.deepcopy(source_document),
        'started_at':time.time(),'snapshot':copy.deepcopy(snapshot),'log':_log_baseline()}
    baseline['evidence_ref']=_publish(directory,'maa-queue-input-baseline',baseline)
    return baseline


def _queue_log_sequence(raw, baseline, captured_at, source):
    """Admit exact post-parse collection Add messages on the request thread."""
    outcome = single._log_sequence(raw,baseline,captured_at,source)
    if outcome.get('code') == 'interleaved_load_metadata':
        text = raw.decode('utf-8')
        prefix = ''
        if not baseline['log']['ended_with_newline']:
            prefix,_,text = text.partition('\n')
            prefix += '\n'
        lines = text.split('\n')[:-1]
        additions = {}
        for index,line in enumerate(lines):
            event = single._PREFIX.fullmatch(line.rstrip('\r'))
            if event and event.group(3) == 'CopilotViewModel' and event.group(5) == 'Copilot item collection changed: "Add"':
                additions[index] = event
        maximum = 2 if source.get('difficulty',1) == 3 else 1
        if not 0 < len(additions) <= maximum:
            return outcome
        retained = [index for index in range(len(lines)) if index not in additions]
        filtered = (prefix+'\n'.join(lines[index] for index in retained)+'\n').encode('utf-8')
        checked = single._log_sequence(filtered,baseline,captured_at,source)
        if not checked.get('verified'):
            return checked
        request_index = retained[checked['log_request_line']]
        metadata = [retained[index] for index in checked['log_metadata_lines']]
        request = single._PREFIX.fullmatch(lines[request_index].rstrip('\r'))
        requested = datetime.strptime(request.group(1),'%Y-%m-%d %H:%M:%S.%f').timestamp()
        for index,event in additions.items():
            occurred = datetime.strptime(event.group(1),'%Y-%m-%d %H:%M:%S.%f').timestamp()
            if (index <= max(metadata) or event.group(2) != 'INF' or event.group(4) != request.group(4)
                    or not requested <= occurred <= captured_at+.002):
                return _result('rejected','queue_collection_event_unverified')
        outcome = {**checked,'log_request_line':request_index,
                   'log_metadata_lines':metadata,'log_queue_lines':list(additions)}
    if outcome.get('verified'):
        expected = expected_loaded_copilot_document(source)
        expected_normalization = single._load_metadata(source)[2]
        if (_bytes(expected) != _bytes(source)
                and outcome.get('cache_normalization') != expected_normalization):
            return _result('rejected','load_normalization_mismatch')
    return outcome


def _log_evidence(baseline,captured_at):
    path=_no_links(_MAA_ROOT/'debug/gui.log');old=baseline['log']
    try:
        with path.open('rb') as stream:
            before=_state(os.fstat(stream.fileno()))
            if before['file_identity']!=old['file_identity']:raise MaaQueueReceiptError('log_rotated')
            if before['size']<old['offset']:raise MaaQueueReceiptError('log_truncated')
            if before['size']-old['offset']>single._MAX_LOG:raise MaaQueueReceiptError('log_limit')
            if single._prefix_hash(stream,old['offset'])!=old['prefix_sha256']:raise MaaQueueReceiptError('log_prefix_changed')
            stream.seek(old['offset']);raw=stream.read(single._MAX_LOG+1);after=_state(os.fstat(stream.fileno()))
        if before!=after or after!=_state(path.stat()):raise MaaQueueReceiptError('log_changed')
        binding={'code':baseline['target']['mystery_code'],'started_at':baseline['started_at'],'log':old}
        outcome=_queue_log_sequence(raw,binding,captured_at,canonical_copilot_document(baseline['source_document']))
        events=[]
        if outcome['verified']:
            text=raw.decode('utf-8')
            if not old['ended_with_newline']:text=text.partition('\n')[2]
            lines=text.splitlines()
            indices=sorted({outcome['log_request_line'],*outcome['log_metadata_lines'],*outcome.get('log_queue_lines',[])})
            events=[lines[index] for index in indices]
        return {'outcome':{k:v for k,v in outcome.items() if k not in ('log_request_line','log_metadata_lines','log_queue_lines')},
                'events':events,'state':after,'end_offset':old['offset']+len(raw),
                'prefix_sha256':old['prefix_sha256'],'appended_sha256':_hash(raw)}
    except OSError:raise MaaQueueReceiptError('log_unreadable') from None
    except single.MaaLoadReceiptError as error:raise MaaQueueReceiptError(error.code) from None


def _result(status,code,**facts):
    return {'status':status,'verified':status=='verified','code':code,
            'matched_rows':[],'added_rows':[],'evidence_refs':[],**facts}


def collect_queue_receipt(baseline,current_binding,directory,*,screenshot_refs,visible_complete=False):
    """Poll only after the same single GUI input; never authorizes a retry."""
    try:
        _binding(current_binding,live=True)
        if baseline['binding']!=current_binding:raise MaaQueueReceiptError('binding_changed')
        now=time.time();logs=_log_evidence(baseline,now)
        snapshot=capture_queue_snapshot(current_binding,directory,screenshot_refs=screenshot_refs,
            visible_complete=visible_complete)
        if _state(_no_links(_MAA_ROOT/'debug/gui.log').stat())!=logs['state']:
            return _result('pending','log_changed_during_queue_read')
        payload={'schema':'maa_queue_input_receipt/v1','baseline':copy.deepcopy(baseline),
            'current_binding':copy.deepcopy(current_binding),'captured_at':now,
            'current_snapshot':snapshot,'log_evidence':logs}
        result=verify_queue_receipt(payload,{'binding':current_binding,'target':baseline['target'],
            'source_document':baseline['source_document'],'input_id':baseline['input_id']})
        reference=_publish(directory,'maa-queue-input-receipt',payload)
        return {**result,'receipt_ref':reference,'evidence_refs':[reference]}
    except MaaQueueReceiptError as error:
        pending={'file_changed_during_read','config_changed_during_read','body_changed_during_read',
                 'log_changed','log_unreadable','file_unreadable','screenshot_changed'}
        return _result('pending' if error.code in pending else 'rejected',error.code)
    except (ValueError,KeyError,TypeError,UnicodeError,OSError,AttributeError):
        return _result('rejected','queue_receipt_invalid')


def verify_queue_receipt(payload,expected):
    """Pure validation of copied evidence; no live MAA or filesystem reads.

    The caller must separately validate artifact refs and *fresh* visual queue
    coverage. This file channel never infers screenshot contents or parse idle.
    """
    try:
        if payload.get('schema')!='maa_queue_input_receipt/v1':raise ValueError()
        baseline=payload['baseline'];current=payload['current_snapshot']
        if baseline.get('schema')!='maa_queue_input_baseline/v1':raise ValueError()
        original=dict(baseline);reference=original.pop('evidence_ref')
        if reference['sha256']!=_hash(_bytes(original)):raise ValueError()
        if (baseline['binding']!=_binding(expected['binding']) or payload['current_binding']!=baseline['binding']
                or current['binding']!=baseline['binding']):raise MaaQueueReceiptError('binding_changed')
        if expected.get('input_id',baseline['input_id'])!=baseline['input_id']:raise ValueError()
        target=_target(expected['target'],expected['source_document'])
        if target!=baseline['target'] or _bytes(canonical_copilot_document(baseline['source_document']))!=_bytes(canonical_copilot_document(expected['source_document'])):
            raise MaaQueueReceiptError('source_binding_mismatch')
        now=payload['captured_at'];started=baseline['started_at']
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (now,started)) or not 0<=now-started<=180:
            raise MaaQueueReceiptError('receipt_window_expired')
        _check_snapshot(baseline['snapshot']);_check_snapshot(current)
        if current['captured_at']<now or current['captured_at']-now>15:raise ValueError()
        log=payload['log_evidence'];old=baseline['log']
        if (log['state']['file_identity']!=old['file_identity'] or log['prefix_sha256']!=old['prefix_sha256']
                or log['end_offset']!=log['state']['size'] or log['state']['size']<old['offset']):raise ValueError()
        outcome=log['outcome']
        if not outcome['verified']:return _result(outcome['status'],outcome['code'])
        events=log['events']
        if not isinstance(events,list) or len(events)>10000 or any(not isinstance(v,str) or '\n' in v or '\r' in v for v in events):raise ValueError()
        raw=(('' if old['ended_with_newline'] else '\n')+'\n'.join(events)+'\n').encode()
        rechecked=_queue_log_sequence(raw,{'code':target['mystery_code'],'started_at':started,'log':old},now,
            canonical_copilot_document(expected['source_document']))
        if not rechecked['verified']:return _result(rechecked['status'],rechecked['code'])
        return _verified_added_rows(baseline,current,target,not_before=started)
    except MaaQueueReceiptError as error:return _result('rejected',error.code)
    except single.MaaLoadReceiptError as error:return _result('rejected',error.code)
    except (ValueError,KeyError,TypeError,UnicodeError,AttributeError,OverflowError,RecursionError):
        return _result('rejected','queue_receipt_invalid')


def _verified_added_rows(baseline, current, target, *, not_before):
    difference = diff_queue(baseline['snapshot'],current,[target],[])
    if difference['external_changes']:
        return _result('rejected','baseline_queue_changed')
    if any(item['code']=='queue_visibility_incomplete' for item in difference['conflicts']):
        return _result('pending','queue_readback_incomplete')
    if any(item['code']=='queue_absence_unverified' for item in difference['conflicts']):
        return _result('pending','queue_absence_unverified')
    if difference['conflicts']:
        return _result('rejected','queue_target_conflict')
    if difference['missing']:
        return _result('pending','queue_not_persisted')
    if not _frame_advanced(baseline['snapshot'],current,not_before=not_before):
        return _result('pending','queue_screenshot_not_advanced')
    rows = current['rows']
    old_counts = Counter(row['identity'] for row in baseline['snapshot']['rows'])
    added = []
    for row in rows:
        if old_counts[row['identity']]:
            old_counts[row['identity']] -= 1
        else:
            added.append(row)
    matched = [row['identity'] for row in rows if row_matches_target(row,target)]
    if not matched or not any(row['identity'] in matched for row in added):
        return _result('pending','queue_generation_not_advanced')
    # Additional rows are factual additions. Target completion and deletion
    # permissions remain bound to the plan and the queue journal.
    return _result('verified','maa_queue_input_verified',matched_rows=matched,
        added_rows=[row['identity'] for row in added],preserved_baseline=True,
        target_key=target['target_key'],input_id=baseline['input_id'],battle_start_submitted=False)


def _input_provenance(payload, expected):
    """Recheck a code GET and its metadata without asserting an enqueue effect."""
    if payload.get('schema') != 'maa_queue_input_receipt/v1':
        raise MaaQueueReceiptError('source_input_invalid')
    baseline = payload['baseline']
    original = dict(baseline)
    reference = original.pop('evidence_ref')
    if (baseline.get('schema') != 'maa_queue_input_baseline/v1'
            or reference['sha256'] != _hash(_bytes(original))):
        raise MaaQueueReceiptError('source_input_invalid')
    target = _target(expected['target'], expected['source_document'])
    binding = _binding(expected['binding'])
    if (baseline['binding'] != binding or payload['current_binding'] != binding
            or baseline['target'] != target
            or _bytes(canonical_copilot_document(baseline['source_document']))
                != _bytes(canonical_copilot_document(expected['source_document']))):
        raise MaaQueueReceiptError('source_binding_mismatch')
    _check_snapshot(baseline['snapshot'])
    _check_snapshot(payload['current_snapshot'])
    if (baseline['snapshot']['binding'] != binding
            or payload['current_snapshot']['binding'] != binding):
        raise MaaQueueReceiptError('binding_changed')
    started, captured = baseline['started_at'], payload['captured_at']
    if (any(type(value) not in (int, float) or not math.isfinite(value)
            for value in (started, captured)) or not 0 <= captured-started <= 180):
        raise MaaQueueReceiptError('receipt_window_expired')
    logs, old = payload['log_evidence'], baseline['log']
    if (logs['state']['file_identity'] != old['file_identity']
            or logs['prefix_sha256'] != old['prefix_sha256']
            or logs['end_offset'] != logs['state']['size']
            or logs['state']['size'] < old['offset']):
        raise MaaQueueReceiptError('source_input_invalid')
    events = logs['events']
    if (not isinstance(events, list) or len(events) > 10000
            or any(not isinstance(line,str) or '\n' in line or '\r' in line for line in events)):
        raise MaaQueueReceiptError('source_input_invalid')
    raw = (('' if old['ended_with_newline'] else '\n')+'\n'.join(events)+'\n').encode()
    outcome = _queue_log_sequence(raw, {'code':target['mystery_code'],
        'started_at':started, 'log':old}, captured,
        canonical_copilot_document(expected['source_document']))
    if not logs['outcome'].get('verified') or not outcome.get('verified'):
        raise MaaQueueReceiptError('source_input_unverified')
    return target


def _committed_recovery_facts(committed):
    fields = ('operation_id','task_id','revision','epoch','submitted_at','payload_hash','document_ref')
    facts = {key:copy.deepcopy(committed[key]) for key in fields}
    if (not _text(facts['operation_id'],128) or not _text(facts['task_id'],128)
            or any(type(facts[key]) is not int or facts[key] < 1 for key in ('revision','epoch'))
            or not isinstance(facts['payload_hash'],str) or not _HASH.fullmatch(facts['payload_hash'])):
        raise MaaQueueReceiptError('recovery_commit_invalid')
    _frames([facts['document_ref']])
    submitted = datetime.fromisoformat(facts['submitted_at'])
    if submitted.tzinfo is None:
        raise MaaQueueReceiptError('recovery_commit_invalid')
    baseline = committed['baseline']
    facts['baseline_ref'] = copy.deepcopy(baseline['evidence_ref'])
    _frames([facts['baseline_ref']])
    if not baseline['started_at'] <= submitted.timestamp() <= baseline['started_at']+180:
        raise MaaQueueReceiptError('recovery_commit_invalid')
    return facts,submitted.timestamp()


def collect_queue_recovery_receipt(committed, current_binding, directory, *, screenshot_refs,
                                   visible_complete=False):
    """Reconcile a persisted input later, retaining its original log time bound.

    The caller supplies immutable operation facts from the journal, validates
    the current source file by hash and performs current native visual coverage.
    Every appended GET is scanned, so a later reload cannot replace the original.
    """
    try:
        facts,submitted = _committed_recovery_facts(committed)
        baseline = committed['baseline']
        _binding(current_binding,live=True)
        if baseline['binding'] != current_binding:
            raise MaaQueueReceiptError('binding_changed')
        captured = time.time()
        window_end = min(baseline['started_at']+180,submitted+180,captured)
        logs = _log_evidence(baseline,window_end)
        current = capture_queue_snapshot(current_binding,directory,screenshot_refs=screenshot_refs,
                                         visible_complete=visible_complete)
        if _state(_no_links(_MAA_ROOT/'debug/gui.log').stat()) != logs['state']:
            return _result('pending','log_changed_during_queue_read')
        payload = {'schema':'maa_queue_recovery_receipt/v1','committed':facts,
            'baseline':copy.deepcopy(baseline),'current_binding':copy.deepcopy(current_binding),
            'captured_at':captured,'current_snapshot':current,'log_evidence':logs}
        expected = {**committed,'binding':current_binding,'target':baseline['target'],
                    'source_document':baseline['source_document']}
        result = verify_queue_recovery_receipt(payload,expected)
        reference = _publish(directory,'maa-queue-recovery-receipt',payload)
        return {**result,'receipt_ref':reference,'evidence_refs':[reference]}
    except (MaaQueueReceiptError,single.MaaLoadReceiptError) as error:
        return _result('rejected',error.code)
    except (ValueError,KeyError,TypeError,UnicodeError,OSError,AttributeError):
        return _result('rejected','queue_recovery_receipt_invalid')


def verify_queue_recovery_receipt(payload, expected):
    """A historical input's effect requires old commit facts and a fresh queue."""
    try:
        if payload.get('schema') != 'maa_queue_recovery_receipt/v1':
            raise ValueError()
        facts,submitted = _committed_recovery_facts(expected)
        baseline = payload['baseline']
        current = payload['current_snapshot']
        now = payload['captured_at']
        if (payload['committed'] != facts or baseline != expected['baseline']
                or baseline['evidence_ref'] != facts['baseline_ref']
                or payload['current_binding'] != _binding(expected['binding'])
                or current['binding'] != baseline['binding']
                or type(now) not in (int,float) or not math.isfinite(now) or now < submitted
                or not now <= current['captured_at'] <= now+15):
            raise MaaQueueReceiptError('recovery_commit_invalid')
        window_end = min(baseline['started_at']+180,submitted+180,now)
        observation = {'schema':'maa_queue_input_receipt/v1','baseline':baseline,
            'current_binding':payload['current_binding'],'captured_at':window_end,
            'current_snapshot':current,'log_evidence':payload['log_evidence']}
        target = _input_provenance(observation,expected)
        request = single._PREFIX.fullmatch(payload['log_evidence']['events'][0])
        requested = datetime.strptime(request.group(1),'%Y-%m-%d %H:%M:%S.%f').timestamp()
        if not submitted-.002 <= requested <= submitted+180:
            raise MaaQueueReceiptError('recovery_request_outside_commit')
        outcome = _verified_added_rows(baseline,current,target,not_before=max(submitted,now-60))
        if outcome['verified']:
            outcome.update(code='maa_queue_recovery_verified',operation_id=facts['operation_id'],
                           original_epoch=facts['epoch'],historical_reconciliation=True)
        return outcome
    except (MaaQueueReceiptError,single.MaaLoadReceiptError) as error:
        return _result('rejected',error.code)
    except (ValueError,KeyError,TypeError,UnicodeError,AttributeError,OverflowError,RecursionError):
        return _result('rejected','queue_recovery_receipt_invalid')


def _effect_rows(snapshot):
    """Exclude transient Index while preserving every other visible row fact."""
    fields = ('identity', 'name', 'file_path', 'copilot_id', 'is_raid',
              'is_checked', 'is_nav_name_override')
    return [tuple(row[key] for key in fields) for row in snapshot['rows']]


def _source_effect(source_input, binding, target, source_document):
    if (not isinstance(source_input,dict) or not _text(source_input.get('operation_id'),128)
            or not isinstance(source_input.get('receipt_ref'),dict)):
        raise MaaQueueReceiptError('source_input_invalid')
    payload = source_input['receipt']
    if source_input['receipt_ref'].get('sha256') != _hash(_bytes(payload)):
        raise MaaQueueReceiptError('source_input_invalid')
    _frames([source_input['receipt_ref']])
    source_target = _target(payload['baseline']['target'],source_document)
    target = _target(target,source_document)
    if (any(source_target[field] != target[field] for field in
            ('stage_id','mystery_code','semantic_sha256','source_difficulty'))
            or source_target['difficulty'] != target['difficulty']
            and source_target['source_difficulty'] != 3):
        raise MaaQueueReceiptError('source_binding_mismatch')
    expected = {'binding':binding,'target':source_target,'source_document':source_document}
    if payload.get('schema') in {'maa_queue_recovery_receipt/v1','maa_queue_absence_receipt/v1'}:
        verified = _verify_source_receipt(payload,expected)
        if verified.get('verified') is not True:
            raise MaaQueueReceiptError('source_input_unverified')
    else:
        _input_provenance(payload, expected)
    return payload


def _verify_source_receipt(payload, expected):
    if payload.get('schema') == 'maa_queue_absence_receipt/v1':
        return verify_queue_absence_receipt(payload,expected)
    if payload.get('schema') == 'maa_queue_recovery_receipt/v1':
        return verify_queue_recovery_receipt(payload,{**expected,**payload['committed'],
            'baseline':payload['baseline']})
    return verify_queue_receipt(payload,expected)


def _delete_ownership(baseline):
    target = baseline['target']
    source = baseline['source_input']
    proof = source['receipt']
    verified = _verify_source_receipt(proof, {'binding':baseline['binding'],
        'target':proof['baseline']['target'], 'source_document':baseline['source_document']})
    identity = baseline['row_identity']
    rows = baseline['snapshot']['rows']
    selected = [row for row in rows if row['identity'] == identity]
    original_ids = {row['identity'] for row in proof['baseline']['snapshot']['rows']}
    if (verified.get('verified') is not True or identity not in verified['added_rows']
            or identity in original_ids or len(selected) != 1
            or selected[0]['verification'] != 'verified'):
        raise MaaQueueReceiptError('queue_delete_ownership_unverified')
    row = selected[0]
    if (row['copilot_id'] != target.get('expected_copilot_id',int(_CODE.fullmatch(target['mystery_code']).group(1)))
            or row['stage_id'] != target['stage_id']
            or row['semantic_sha256'] != target['semantic_sha256']
            or target.get('source_difficulty') != 3):
        raise MaaQueueReceiptError('queue_delete_ownership_unverified')
    planned = baseline['planned_targets']
    if (not isinstance(planned,list) or not planned
            or any(not isinstance(item,dict) for item in planned)):
        raise MaaQueueReceiptError('queue_delete_scope_unverified')
    for item in planned:
        item = _target(item)
        if row['stage_id'] == item['stage_id'] and row['is_raid'] == (item['difficulty'] == 2):
            raise MaaQueueReceiptError('queue_delete_planned_target')
    # The current queue must still contain the original code operation's exact
    # generated row. The journal separately validates its operation ownership.
    generated = [item for item in proof['current_snapshot']['rows'] if item['identity'] == identity]
    if len(generated) != 1 or _effect_rows({'rows':generated}) != _effect_rows({'rows':selected}):
        raise MaaQueueReceiptError('queue_delete_ownership_unverified')


def capture_queue_effect_baseline(binding, phase, directory, snapshot, *, target,
                                  source_document, source_input, row_identity=None,
                                  planned_targets=None):
    """Capture an add/delete input baseline without granting input permission.

    In particular, an add baseline cannot replace a verified parse-ready
    authorization. The journal must obtain that authorization before issuing +.
    """
    _binding(binding, live=True)
    _check_snapshot(snapshot)
    if phase not in {'add','delete'}:
        raise MaaQueueReceiptError('queue_effect_phase_invalid')
    if snapshot['binding'] != binding or snapshot['visible_complete'] is not True:
        raise MaaQueueReceiptError('queue_readback_incomplete')
    target = _target(target, source_document)
    source = _source_effect(source_input,binding,target,source_document)
    now = time.time()
    if (not 0 <= now-snapshot['captured_at'] <= 15
            or source['current_snapshot']['captured_at'] > snapshot['captured_at']
            or source['captured_at'] > now):
        raise MaaQueueReceiptError('snapshot_expired')
    raw, _ = _read_stable(binding['config_path'])
    if project_queue_config(raw) != snapshot['projection']:
        raise MaaQueueReceiptError('baseline_snapshot_changed')
    baseline = {'schema':'maa_queue_effect_baseline/v1','effect_id':uuid.uuid4().hex,
        'phase':phase,'binding':copy.deepcopy(binding),'target':target,
        'source_document':copy.deepcopy(source_document),'source_input':copy.deepcopy(source_input),
        'snapshot':copy.deepcopy(snapshot),'started_at':now,'row_identity':row_identity,
        'planned_targets':copy.deepcopy(planned_targets or [])}
    if phase == 'delete':
        _delete_ownership(baseline)
    elif row_identity is not None:
        raise MaaQueueReceiptError('queue_effect_row_invalid')
    if len(_bytes(baseline)) > 192*1024*1024:
        raise MaaQueueReceiptError('queue_effect_size_limit')
    baseline['evidence_ref'] = _publish(directory,'maa-queue-effect-baseline',baseline)
    return baseline


def collect_queue_effect_receipt(baseline, current_binding, directory, *, previous_snapshot,
                                 screenshot_refs, visible_complete=False, operation_id,
                                 submitted_at):
    """Collect two post-submit observations for one already-issued add/delete."""
    try:
        _binding(current_binding,live=True)
        current = capture_queue_snapshot(current_binding,directory,screenshot_refs=screenshot_refs,
                                         visible_complete=visible_complete)
        payload = {'schema':'maa_queue_effect_receipt/v1','baseline':copy.deepcopy(baseline),
            'operation_id':operation_id,'submitted_at':submitted_at,
            'previous_snapshot':copy.deepcopy(previous_snapshot),'current_snapshot':current}
        expected = {'binding':current_binding,'phase':baseline['phase'],
            'baseline_ref':baseline['evidence_ref'],'effect_id':baseline['effect_id'],
            'operation_id':operation_id,'submitted_at':submitted_at,
            'source_operation_id':baseline['source_input']['operation_id'],
            'target':baseline['target'],'source_document':baseline['source_document'],
            'row_identity':baseline['row_identity']}
        result = verify_queue_effect_receipt(payload,expected)
        reference = _publish(directory,'maa-queue-effect-receipt',payload)
        return {**result,'receipt_ref':reference,'evidence_refs':[reference]}
    except MaaQueueReceiptError as error:
        return _result('pending' if error.code in {'file_changed_during_read','config_changed_during_read',
            'body_changed_during_read','file_unreadable','screenshot_changed'} else 'rejected',error.code)
    except (ValueError,KeyError,TypeError,UnicodeError,OSError,AttributeError):
        return _result('rejected','queue_effect_receipt_invalid')


def verify_queue_effect_receipt(payload, expected):
    """Verify a post-effect delta; its success never authorizes a new action."""
    try:
        if payload.get('schema') != 'maa_queue_effect_receipt/v1':
            raise ValueError()
        baseline = payload['baseline']
        original = dict(baseline)
        reference = original.pop('evidence_ref')
        if (baseline.get('schema') != 'maa_queue_effect_baseline/v1'
                or reference['sha256'] != _hash(_bytes(original))
                or reference != expected['baseline_ref']
                or baseline['effect_id'] != expected['effect_id']
                or baseline['phase'] != expected['phase']
                or baseline['binding'] != _binding(expected['binding'])
                or baseline['source_input']['operation_id'] != expected['source_operation_id']
                or baseline['row_identity'] != expected.get('row_identity')):
            raise ValueError()
        operation_id = expected['operation_id']
        submitted = expected['submitted_at']
        if (not _text(operation_id,128) or operation_id != payload['operation_id']
                or type(submitted) not in (float,int) or not math.isfinite(submitted)
                or submitted != payload['submitted_at']):
            raise ValueError()
        target = _target(expected['target'],expected['source_document'])
        if (baseline['target'] != target or canonical_copilot_document(baseline['source_document'])
                != canonical_copilot_document(expected['source_document'])):
            raise MaaQueueReceiptError('source_binding_mismatch')
        _source_effect(baseline['source_input'],baseline['binding'],target,baseline['source_document'])
        before, previous, current = baseline['snapshot'],payload['previous_snapshot'],payload['current_snapshot']
        for snapshot in (before,previous,current):
            _check_snapshot(snapshot)
            if snapshot['binding'] != baseline['binding']:
                raise MaaQueueReceiptError('binding_changed')
            if snapshot['visible_complete'] is not True or not snapshot['screenshot_refs']:
                return _result('pending','queue_readback_incomplete')
        if not (before['captured_at'] <= baseline['started_at'] <= submitted
                <= previous['captured_at'] <= current['captured_at'] <= submitted+180):
            raise MaaQueueReceiptError('receipt_window_expired')
        if (not _frame_advanced(before,previous,not_before=submitted)
                or not _absence_observed(previous,current)):
            return _result('pending','queue_effect_snapshot_unverified')
        if previous['projection_sha256'] != current['projection_sha256'] or previous['rows'] != current['rows']:
            return _result('pending','queue_effect_unstable')
        if any(row['verification'] != 'verified' for row in current['rows']):
            return _result('rejected','queue_opaque_rows')
        phase = baseline['phase']
        if phase == 'delete':
            _delete_ownership(baseline)
            identity = baseline['row_identity']
            kept = [row for row in before['rows'] if row['identity'] != identity]
            if (_effect_rows({'rows':kept}) != _effect_rows(current)
                    or any(row['identity'] == identity for row in current['rows'])):
                return _result('rejected','queue_delete_delta_unverified')
            return _result('verified','maa_queue_delete_verified',queue_effect='delete_row',
                deleted_rows=[identity],row_identity=identity,preserved_baseline=True,
                operation_id=operation_id,effect_id=baseline['effect_id'],
                source_operation_id=expected['source_operation_id'],battle_start_submitted=False)
        if phase != 'add':
            raise ValueError()
        if any(row['stage_id'] == target['stage_id']
               and row['is_raid'] == (target['difficulty']==2) for row in before['rows']):
            return _result('rejected','queue_add_target_already_present')
        counts = Counter(row['identity'] for row in current['rows'])
        old = Counter(row['identity'] for row in before['rows'])
        if old-counts or sum((counts-old).values()) != 1:
            return _result('rejected','queue_add_delta_unverified')
        added_id = next(iter(counts-old))
        added = [row for row in current['rows'] if row['identity'] == added_id]
        if (len(added) != 1 or added_id in old
                or _effect_rows({'rows':[row for row in current['rows'] if row['identity'] != added_id]})
                    != _effect_rows(before)):
            return _result('rejected','queue_add_delta_unverified')
        row = added[0]
        if not row_matches_target(row,target):
            return _result('rejected','queue_add_target_unverified')
        return _result('verified','maa_queue_add_verified',queue_effect='add_row',
            added_rows=[added_id],deleted_rows=[],matched_rows=[added_id],preserved_baseline=True,
            target_key=target['target_key'],operation_id=operation_id,effect_id=baseline['effect_id'],
            source_operation_id=expected['source_operation_id'],battle_start_submitted=False)
    except MaaQueueReceiptError as error:
        return _result('rejected',error.code)
    except single.MaaLoadReceiptError as error:
        return _result('rejected',error.code)
    except (ValueError,KeyError,TypeError,UnicodeError,AttributeError,OverflowError,RecursionError):
        return _result('rejected','queue_effect_receipt_invalid')


def verify_queue_absence_receipt(payload, expected):
    """Recheck absent rows only with replayable busy→ready native pixels."""
    try:
        if payload.get('schema') != 'maa_queue_absence_receipt/v1':
            raise ValueError()
        target = _input_provenance(payload['input_receipt'],expected)
        source = payload['input_receipt']
        previous,current = payload['previous_snapshot'],payload['current_snapshot']
        _check_snapshot(previous)
        _check_snapshot(current)
        if previous['binding'] != _binding(expected['binding']) or current['binding'] != previous['binding']:
            raise MaaQueueReceiptError('binding_changed')
        if not _absence_observed(previous,current):
            return _result('pending','queue_absence_unverified')
        before = source['baseline']['snapshot']
        if (previous['projection_sha256'] != current['projection_sha256']
                or _effect_rows(before) != _effect_rows(previous)
                or _effect_rows(before) != _effect_rows(current)
                or any(row['verification'] != 'verified' for row in current['rows'])
                or any(row['stage_id']==target['stage_id'] and row['is_raid']==(target['difficulty']==2)
                       for row in current['rows'])):
            return _result('pending','queue_absence_unverified')
        probes = payload.get('parse_probes')
        if not probes:
            return _result('pending','parse_completion_unverified')
        from .maa_queue_parse_ready import verify_parse_ready
        readiness = verify_parse_ready(probes,{'binding':expected['binding'],
            'mystery_code':target['mystery_code'],'submitted_at':payload['submitted_at'],
            'previous_snapshot':previous,'current_snapshot':current})
        if readiness.get('verified') is not True:
            return _result('pending',readiness['code'])
        # Keep the common immutable provenance at the top level so the journal
        # can compare the same committed log baseline and request timestamp.
        if any(payload.get(key) != source[key] for key in
               ('baseline','current_binding','captured_at','log_evidence')):
            raise MaaQueueReceiptError('source_input_invalid')
        return _result('verified','maa_queue_parse_absence_verified',queue_effect='parsed_no_row',
            projection_sha256=current['projection_sha256'],target_key=target['target_key'],
            preserved_baseline=True,battle_start_submitted=False)
    except MaaQueueReceiptError as error:
        return _result('rejected',error.code)
    except single.MaaLoadReceiptError as error:
        return _result('rejected',error.code)
    except (ValueError,KeyError,TypeError,UnicodeError,AttributeError,OverflowError,RecursionError):
        return _result('rejected','queue_absence_receipt_invalid')


def collect_queue_absence_receipt(input_receipt, previous_snapshot, current_snapshot,
                                   parse_probes, directory, *, submitted_at):
    payload = {key:copy.deepcopy(input_receipt[key]) for key in
               ('baseline','current_binding','captured_at','log_evidence')}
    payload.update(schema='maa_queue_absence_receipt/v1',input_receipt=copy.deepcopy(input_receipt),
        previous_snapshot=copy.deepcopy(previous_snapshot),current_snapshot=copy.deepcopy(current_snapshot),
        parse_probes=copy.deepcopy(parse_probes),submitted_at=submitted_at)
    expected = {'binding':input_receipt['current_binding'],'target':input_receipt['baseline']['target'],
                'source_document':input_receipt['baseline']['source_document']}
    result = verify_queue_absence_receipt(payload,expected)
    ref = _publish(directory,'maa-queue-absence-receipt',payload)
    return {**result,'receipt_ref':ref,'evidence_refs':[ref]}
