"""Read-only single-run guard for the fixed MAA GUI's public settings.

Audited MAA 901e01c CopilotViewModel.cs:2346-2347 passes the runtime Loop
flag's configured count to a single task, while hardcoding sanity potion false.
The flag is not persisted, so requiring the literal saved count 1 is safe in
either flag state. This module neither changes MAA nor starts its task queue.
"""
from __future__ import annotations

import hashlib
import json

from . import maa_queue_receipt as receipts


# Do not extend this whitelist on the basis of a matching settings schema:
# the single-task medicine and loop behavior must also be audited in source.
_AUDITED_VERSIONS=frozenset({'v6.22.0+901e01c600f4895ac4e7e48a9b471fc9f52af301'})


class MaaBattleSettingsError(RuntimeError):
    def __init__(self):
        self.code='single_run_settings_unverified'
        super().__init__('MAA单次运行设置未核实；当前配置的循环次数必须明确为1，未启动作战。')


def verify_single_run(identity):
    """Return a small non-secret proof only for a pinned, stable single limit.

    Unrelated GUI geometry, queue history and remote-control secrets are never
    included in this fingerprint; a selected profile switch still invalidates
    it. Native process identity is checked again after the stable file read.
    """
    try:
        process=receipts._process(identity)
        if not receipts._process_matches(process):raise ValueError('binding')
        receipts._no_links(receipts._MAA_EXECUTABLE)
        dll=receipts._no_links(receipts._MAA_ROOT/'MAA.dll')
        version=receipts._product_version(dll)
        if version not in _AUDITED_VERSIONS:raise ValueError('version')
        raw,_=receipts._read_stable(receipts._MAA_ROOT/'config/gui.new.json')
        document=receipts._json(raw)
        current=document['Current']
        if not receipts._text(current,256):raise ValueError('current config')
        limit=document['Configurations'][current]['Copilot']['LoopTimes']
        if type(limit) is not int or limit!=1:raise ValueError('loop limit')
        if not receipts._process_matches(process):raise ValueError('binding')
        value={'current':current,'loop_limit':limit,'maa_version':version}
        encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        return {'settings_sha256':hashlib.sha256(encoded).hexdigest(),'loop_limit':limit}
    except (receipts.MaaQueueReceiptError,KeyError,ValueError,TypeError,AttributeError,
            OSError,UnicodeError,RecursionError,OverflowError):
        raise MaaBattleSettingsError() from None
