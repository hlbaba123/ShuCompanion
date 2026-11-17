import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
for name in ('.agent-runtime','.capture-runtime','.gui-runtime'):sys.path.insert(0,str(ROOT.parent/name))
from shu.conversation_acceptance import run_conversation_acceptance
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--questions',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--version',default='0.3.9-retry')
    parser.add_argument('--allow-maa-import',action='store_true',help='Explicit isolated import-only GUI opt-in; never battle start')
    parser.add_argument('--allow-maa-queue-import',action='store_true',help='Allow multiple retained documents; requires import opt-in')
    parser.add_argument('--timeout',type=int,default=420)
    args=parser.parse_args();profile=json.loads((ROOT/'workspace-profile.json').read_text(encoding='utf-8'))
    return run_conversation_acceptance(ROOT,ROOT,args.output,args.questions,ROOT/profile['key_file'],ROOT/profile['data_dir'],version=args.version,
        allow_maa_import=args.allow_maa_import,allow_maa_queue_import=args.allow_maa_queue_import,timeout_seconds=args.timeout)


if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
