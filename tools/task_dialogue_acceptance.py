"""Source CLI shim for the packaged, isolated task-dialogue acceptance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for component in ('.agent-runtime', '.capture-runtime', '.gui-runtime', '.companion-runtime'):
    sys.path.insert(0, str(ROOT.parent / component))


def main():
    from main import APP_VERSION
    from shu.task_dialogue_acceptance import run_task_dialogue_acceptance
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=600)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--seeded-component-only', action='store_true')
    modes.add_argument('--seeded-lifecycle-only', action='store_true')
    args = parser.parse_args()
    profile = json.loads((ROOT / 'workspace-profile.json').read_text(encoding='utf-8'))
    mode = ('seeded-lifecycle' if args.seeded_lifecycle_only else
            'seeded-component' if args.seeded_component_only else 'natural')
    return run_task_dialogue_acceptance(ROOT, ROOT, args.output, ROOT / profile['key_file'],
                                       ROOT / profile['data_dir'], timeout=args.timeout,
                                       mode=mode, version=APP_VERSION)


if __name__ == '__main__':
    raise SystemExit(main())
