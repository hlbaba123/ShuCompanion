"""Run the isolated production MAA queue probe or natural-language acceptance."""
import argparse
import json
import multiprocessing
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
for name in ('.agent-runtime','.capture-runtime','.gui-runtime','.companion-runtime'):
    sys.path.insert(0,str(ROOT.parent/name))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--request-file',type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--probe-only',dest='mode',action='store_const',const='probe')
    modes.add_argument('--component-only',dest='mode',action='store_const',const='component')
    modes.add_argument('--natural',dest='mode',action='store_const',const='natural')
    parser.set_defaults(mode='probe')
    parser.add_argument('--allow-queue-import',action='store_true')
    parser.add_argument('--executable',type=Path)
    parser.add_argument('--timeout',type=int,default=900)
    args = parser.parse_args(argv)
    from shu.maa_queue_acceptance import run_queue_acceptance
    # Profile contains references only; never read or print the credential file.
    profile = json.loads((ROOT/'workspace-profile.json').read_text(encoding='utf-8'))
    try:
        return run_queue_acceptance(ROOT,ROOT,args.output,ROOT/profile['key_file'],
            ROOT/profile['data_dir'],request_file=args.request_file,mode=args.mode,
            allow_queue_import=args.allow_queue_import,executable=args.executable,
            timeout=args.timeout,version='source')
    except (OSError,ValueError):
        print(json.dumps({'passed':False,'code':'acceptance_output_rejected'}))
        return 2


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
