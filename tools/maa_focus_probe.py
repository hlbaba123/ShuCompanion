"""Explicit mechanical activation acceptance: no model, code entry or battle."""
import argparse
import json
import multiprocessing
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
for name in ('.capture-runtime','.gui-runtime','.agent-runtime'):
    sys.path.insert(0,str(ROOT.parent/name))


def minimized_fixture(command, enter_submission):
    """Component setup only, inside the same owned GUI worker and ledger."""
    import ctypes
    import time
    from ctypes import wintypes as w
    from shu.gui_supervisor import _MaaVisualHandler
    from shu.maa_visual_native import discover_maa_windows
    windows=discover_maa_windows()
    if len(windows)!=1:raise ValueError('Unique MAA required')
    target=windows[0]
    if command.get('args',{}).get('action')!='focus':raise ValueError('Focus only fixture')
    handler=_MaaVisualHandler(target,command['args']['fixture_directory'])
    handler.adapter._current(permit_minimized=True)
    handler.adapter._permission(required=True)
    enter_submission()
    function=ctypes.WinDLL('user32',use_last_error=True).ShowWindowAsync
    function.argtypes=[w.HWND,ctypes.c_int];function.restype=w.BOOL
    if not function(target['hwnd'],6):raise ValueError('Cannot minimize test MAA')
    deadline=time.monotonic()+2
    while time.monotonic()<deadline and not handler.adapter._current(permit_minimized=True).minimized:
        time.sleep(.02)
    if not handler.adapter._current(permit_minimized=True).minimized:raise ValueError('Minimize not confirmed')
    injected=command['args'].get('force_caption_fixture') is True
    if injected:
        from shu.windows import WindowError
        def denied(hwnd):
            function(hwnd,9)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline and handler.adapter._current(permit_minimized=True).minimized:
                time.sleep(.02)
            raise WindowError('component injected SetForeground denial','focus_denied')
        handler.adapter.backend.native.focus_window=denied
    result=handler(command,enter_submission)
    return {**result,'fixture_minimized_confirmed':True,'injected_foreground_denial':injected}


def main():
    from shu.core import TaskEngine
    from shu.workflow_store import WorkflowStore
    from shu.gui_supervisor import GuiSupervisor
    from shu.maa_visual_native import discover_maa_windows
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--minimize-first',action='store_true',help='Mechanical fixture: confirm minimized in same worker before production focus')
    parser.add_argument('--force-caption-fixture',action='store_true',help='With --minimize-first, inject conventional activation denial to exercise real caption path')
    args=parser.parse_args();output=args.output.resolve()
    if output.exists():raise ValueError('Use a new output directory')
    output.mkdir(parents=True)
    engine=TaskEngine(output);store=WorkflowStore(engine);supervisor=None
    result={'scope':'MAA activation only; no model/code input/battle','passed':False}
    try:
        targets=discover_maa_windows()
        if len(targets)!=1:raise ValueError('Unique MAA required')
        result['before']=targets[0]
        state=store.submit('验收自动激活 MAA，不输入作业，不开始作战',
                           {'allow_start':False,'attempt_limit':0,'task_spec':{'kind':'import'}})
        dispatch=store.ensure_dispatch(state['task_id'],'maa','activation',{})
        operation=store.prepare_operation(state['task_id'],dispatch['dispatch_id'],'activation',
                                           {'kind':'maa_import','action':'focus'})
        store.issue_operation(operation['operation_id'])
        supervisor=GuiSupervisor(targets[0],backend='visual',directory=output,
            test_handler=minimized_fixture if args.minimize_first else None)
        response=supervisor.request({'op':'act','args':{'action':'focus','fixture_directory':str(output),
            'force_caption_fixture':args.force_caption_fixture}},timeout=10,
            admit_callback=lambda:store.enter_submission(operation['operation_id']))
        result['response']=response
        store.finish_operation(operation['operation_id'],
            'verified' if response.get('status')=='completed' else 'unknown',response)
        result['after']=discover_maa_windows()
        result['passed']=response.get('status')=='completed' and response.get('result',{}).get('focus_acquired') is True
        result['operation']=store.get_operation(operation['operation_id'])
    except Exception as error:result['error']=str(error)
    finally:
        if supervisor:supervisor.close()
        engine.close()
    (output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'method':result.get('response',{}).get('result',{}).get('focus_method'),
                      'error':result.get('error')},ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
