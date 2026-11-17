"""Verify embedded runtime code, then exercise the final EXE in fresh profiles.

No Key argument is accepted. All launches are offline self-tests; a running Shu
instance is reported, never closed automatically. The target EXE is unmodified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent/'.desktop-build-deps'))
from verify_frozen_sources import fingerprint

REQUIRED=('shu.pet','shu.pet_core','shu.skins','shu.idle_vignette','shu.vignette_diagnostics')


def animated_hold_verified(scene):
    """Require actual displayed pixels throughout all four timed hold loops."""
    hashes=scene.get('hold_frame_sha256',[])
    cycles=scene.get('hold_cycle_sha256',[])
    checks=scene.get('checks',{})
    required=('hold_eight_frames','hold_frame_125ms','all_hold_cells_loaded','hold_at_exact_start',
              'hold_loop_wraps','hold_pixels_change','holds_full_four_seconds','exit_at_exact_four_seconds')
    required+=tuple(f'hold_frame_{index:02}' for index in range(8))
    required+=tuple(f'hold_before_frame_{index:02}' for index in range(1,9))
    required+=tuple(f'hold_cycle_{cycle:02}_frame_{index:02}' for cycle in range(1,5) for index in range(8))
    required+=tuple(f'hold_cycle_{cycle:02}_pixels_{index:02}' for cycle in range(1,5) for index in range(8))
    required+=tuple(f'hold_cycle_{cycle:02}_before_frame_{index:02}' for cycle in range(1,5) for index in range(1,9))
    required+=tuple(f'hold_cycle_{cycle:02}_wraps' for cycle in range(2,5))
    return (scene.get('hold_frames')==8 and scene.get('hold_frame_ms')==125 and scene.get('hold_ms')==4000
            and isinstance(hashes,list) and len(hashes)==8 and all(isinstance(value,str) for value in hashes)
            and len(set(hashes))>=2 and scene.get('hold_unique_frames')==len(set(hashes))
            and scene.get('hold_cycles_verified')==4 and isinstance(cycles,list) and len(cycles)==4
            and all(cycle==hashes for cycle in cycles)
            and isinstance(checks,dict) and all(checks.get(name) is True for name in required))


def _run(command,*,env=None,timeout=90):
    options={'capture_output':True,'text':True,'timeout':timeout,'env':env}
    if os.name=='nt':
        options['creationflags']=subprocess.CREATE_NO_WINDOW
        startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=0;options['startupinfo']=startup
    return subprocess.run([str(item) for item in command],**options)


def _existing_shu_processes():
    if os.name!='nt':return []
    powershell=Path(os.environ.get('WINDIR','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    query=_run([powershell,'-NoProfile','-Command',
                "Get-Process -Name ShuCompanion -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],timeout=15)
    return [int(line) for line in query.stdout.splitlines() if line.strip().isdigit()]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('exe',type=Path)
    parser.add_argument('--output',type=Path,required=True,help='A NEW isolated evidence directory')
    parser.add_argument('--skins',nargs='+',required=True,help='New skin IDs from the packaged manifest')
    parser.add_argument('--timeout',type=int,default=90)
    args=parser.parse_args()
    exe=args.exe.resolve();output=args.output.resolve()
    if not exe.is_file():parser.error('EXE does not exist')
    if output.exists():parser.error('Output must be a new directory; existing evidence is preserved')
    if args.timeout<10 or any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',skin) for skin in args.skins):
        parser.error('Invalid timeout or skin ID')
    output.mkdir(parents=True)
    report={'passed':False,'exe':str(exe),'output':str(output),'skins':args.skins,
            'network_requests':0,'game_inputs':0,'maa_inputs':0,'modules':[],'runs':[]}
    with exe.open('rb') as binary:report['exe_sha256']=hashlib.file_digest(binary,'sha256').hexdigest()
    try:
        from PyInstaller.archive.readers import CArchiveReader
        archive=CArchiveReader(str(exe));pyz_name=next(name for name in archive.toc if name.lower().endswith('.pyz'))
        reader=archive.open_embedded_archive(pyz_name)
        missing=[module for module in REQUIRED if module not in reader.toc]
        report['missing_modules']=missing
        if missing:raise AssertionError('Required vignette runtime is not packaged')
        for module in REQUIRED:
            source=ROOT/(module.replace('.','/')+'.py')
            actual=reader.extract(module);expected=compile(source.read_text(encoding='utf-8'),str(source),'exec')
            report['modules'].append({'module':module,'matches_reviewed_source':fingerprint(actual)==fingerprint(expected),
                                      'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
        if not all(item['matches_reviewed_source'] for item in report['modules']):
            raise AssertionError('Packaged modules differ from reviewed source')
        existing=_existing_shu_processes();report['existing_shu_process_ids']=existing
        if existing:raise RuntimeError('ShuCompanion is already running; it was preserved and no duplicate EXE was launched')
        env=os.environ.copy();env['QT_QPA_PLATFORM']='offscreen';env.pop('DEEPSEEK_API_KEY',None)
        temp=output/'temp';temp.mkdir();env['TEMP']=env['TMP']=str(temp)
        if os.name=='nt':
            windows=Path(os.environ.get('WINDIR','C:/Windows'))
            env['PATH']=os.pathsep.join(str(path) for path in (exe.parent,windows/'System32',windows))
        # Existing general self-test verifies the normal final-EXE catalog and
        # full GUI while staying in its own empty offline profile.
        self_root=output/'self-test';data=self_root/'data';data.mkdir(parents=True)
        (data/'pet-appearance.json').write_text(json.dumps({'skin_id':args.skins[0]}),encoding='utf-8')
        run=_run([exe,'--self-test','--data-dir',data,'--snapshot',self_root/'activity.png'],env=env,timeout=args.timeout)
        self_report=json.loads((self_root/'self-test.json').read_text(encoding='utf-8'))
        checks={item['skin_id']:item for item in self_report.get('skin_checks',[])}
        self_ok=(run.returncode==0 and self_report.get('offline') is True
                 and self_report.get('network_requests')==0 and self_report.get('game_inputs')==0
                 and not self_report.get('ui_exceptions')
                 and all(checks.get(skin,{}).get('selected') and checks[skin].get('preview_saved') for skin in args.skins))
        report['runs'].append({'kind':'final-exe-self-test','returncode':run.returncode,'passed':bool(self_ok),
                               'report':str(self_root/'self-test.json')})
        if not self_ok:raise AssertionError('Final EXE general self-test failed')
        for skin in args.skins:
            scene_root=output/f'vignette-{skin}'
            run=_run([exe,'--self-test','--data-dir',scene_root/'data','--verify-skin-vignette',skin],
                     env=env,timeout=args.timeout)
            scene=json.loads((scene_root/'vignette-check.json').read_text(encoding='utf-8'))
            passed=(run.returncode==0 and scene.get('passed') is True and scene.get('frozen') is True
                    and scene.get('exe_sha256')==report['exe_sha256'] and scene.get('idle_seconds')==120
                    and scene.get('enter_frames')==20 and scene.get('exit_frames')==10
                    and animated_hold_verified(scene)
                    and scene.get('network_requests')==0 and scene.get('game_inputs')==0
                    and scene.get('checks') and all(scene['checks'].values()))
            report['runs'].append({'kind':'final-exe-vignette','skin':skin,'returncode':run.returncode,
                                   'passed':bool(passed),'report':str(scene_root/'vignette-check.json')})
            if not passed:raise AssertionError('Final EXE vignette acceptance failed: '+skin)
        report['passed']=True
    except Exception as error:
        report.update(error_type=type(error).__name__,error=str(error)[:500])
    (output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'report':str(output/'verification.json'),
                      'runs':len(report['runs']),'error':report.get('error')},ensure_ascii=True))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
