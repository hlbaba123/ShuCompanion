"""Archive an explicitly supplied XLSX roster into the configured local profile.

Offline: no Key, game process, GUI, model, network or job matching is involved.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shu.core import TaskEngine
from shu.maa_roster_tools import RosterTools
from shu.roster_xlsx import import_user_roster


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--profile-id', default='local-default')
    arguments = parser.parse_args()
    directory = arguments.data_dir
    if directory is None:
        # Read only the data directory, never the Key or any settings contents.
        profile = json.loads((ROOT / 'workspace-profile.json').read_text(encoding='utf-8-sig'))
        directory = ROOT / profile['data_dir']
    engine = TaskEngine(directory.resolve())
    try:
        receipt = import_user_roster(arguments.source, RosterTools(engine, directory),
                                     arguments.profile_id)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    finally:
        engine.close()


if __name__ == '__main__':
    main()
