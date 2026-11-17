"""Run the product's real conversation after the user activates MAA."""
import argparse
import json
import multiprocessing
import subprocess
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
for name in ('.agent-runtime','.capture-runtime','.gui-runtime','.companion-runtime'):
    sys.path.insert(0,str(ROOT.parent/name))


def main():
    from shu.maa_visual_native import discover_maa_windows
    from shu.conversation_acceptance import run_conversation_acceptance
    parser=argparse.ArgumentParser()
    parser.add_argument('--questions',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--wait-foreground',type=int,default=120)
    parser.add_argument('--exe',type=Path,help='Run the final release EXE through the same natural acceptance mode')
    parser.add_argument('--permission-handoff',action='store_true',help='Start unauthorized and exercise the task-page grant button')
    parser.add_argument('--automatic-focus',action='store_true',help='Let Shu activate MAA itself; do not wait for the user to foreground it')
    args=parser.parse_args()
    deadline=time.monotonic()+min(300,max(1,args.wait_foreground))
    while time.monotonic()<deadline:
        windows=discover_maa_windows()
        if len(windows)==1 and (args.automatic_focus or windows[0]['foreground'] and not windows[0]['minimized']):
            break
        time.sleep(.1)
    else:
        print('MAA foreground wait expired; no import task started')
        return 2
    if args.exe:
        executable=args.exe.resolve(strict=True)
        if not executable.is_relative_to((ROOT/'release').resolve()) or executable.name!='ShuCompanion.exe':
            raise ValueError('Expected the project release executable')
        return subprocess.run([str(executable),'--conversation-test',str(args.questions.resolve()),
            '--conversation-output',str(args.output.resolve()),'--allow-maa-import-test',
            *(['--maa-permission-handoff-test'] if args.permission_handoff else [])],
            creationflags=subprocess.CREATE_NO_WINDOW).returncode
    profile=json.loads((ROOT/'workspace-profile.json').read_text(encoding='utf-8'))
    return run_conversation_acceptance(ROOT,ROOT,args.output,args.questions,
        ROOT/profile['key_file'],ROOT/profile['data_dir'],version='0.3.19-focus',
        allow_maa_import=True,permission_handoff=args.permission_handoff)


if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
