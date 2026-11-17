"""Compare the actual EXE's embedded Python with current reviewed sources."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import types

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent/'.desktop-build-deps'))


def fingerprint(value):
    if isinstance(value,types.CodeType):
        return [value.co_argcount,value.co_posonlyargcount,value.co_kwonlyargcount,value.co_flags,
            value.co_code.hex(),value.co_names,value.co_varnames,value.co_freevars,value.co_cellvars,
            [fingerprint(v) for v in value.co_consts]]
    if isinstance(value,(set,frozenset)):
        return sorted([fingerprint(v) for v in value],key=repr)
    if isinstance(value,(list,tuple)):return [fingerprint(v) for v in value]
    if isinstance(value,bytes):return value.hex()
    return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('exe',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from PyInstaller.archive.readers import CArchiveReader
    archive=CArchiveReader(str(args.exe.resolve()))
    pyz_name=next(name for name in archive.toc if name.lower().endswith('.pyz'))
    reader=archive.open_embedded_archive(pyz_name)
    items=[]
    for module in sorted(name for name in reader.toc if name.startswith('shu.')):
        path=ROOT/(module.replace('.','/')+'.py')
        if not path.is_file():continue
        actual=reader.extract(module)
        expected=compile(path.read_text(encoding='utf-8'),str(path.resolve()),'exec')
        items.append({'module':module,'compiled_source_matches':fingerprint(actual)==fingerprint(expected),
            'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    report={'exe':str(args.exe.resolve()),'exe_sha256':hashlib.sha256(args.exe.read_bytes()).hexdigest(),
        'check':'embedded bytecode/constants/names compared to compiled current sources; filenames and line metadata excluded',
        'modules':items,'passed':bool(items) and all(item['compiled_source_matches'] for item in items)}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'modules':len(items),'mismatches':[i['module'] for i in items if not i['compiled_source_matches']]}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
