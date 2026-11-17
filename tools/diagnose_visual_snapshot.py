"""Read-only supervisor boundary diagnostic. Never submits GUI input."""
from pathlib import Path
import json
import os
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name in ('.gui-runtime', '.capture-runtime', '.agent-runtime', '.companion-runtime'):
    sys.path.insert(0, str(ROOT.parent / name))

def main():
    from shu.gui_supervisor import GuiSupervisor
    from shu.maa_visual_native import discover_maa_windows
    output = ROOT / 'artifacts/20261109-model-import/snapshot-diagnostic.json'
    result = {'pid': os.getpid(), 'read_only': True}
    def atomic_probe(directory):
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / (uuid.uuid4().hex + '.tmp')
        destination = directory / (uuid.uuid4().hex + '.json')
        try:
            source.write_text('diagnostic', encoding='utf-8')
            os.replace(source, destination)
            return {'ok': True, 'path': str(directory)}
        except Exception:
            result = {'ok': False, 'path': str(directory), 'traceback': traceback.format_exc()}
            try:
                with destination.open('w', encoding='utf-8') as stream:
                    stream.write('{"diagnostic": true}')
                    stream.flush()
                    os.fsync(stream.fileno())
                result['inplace_fsync'] = json.loads(destination.read_text(encoding='utf-8')) == {'diagnostic': True}
            except Exception:
                result['inplace_traceback'] = traceback.format_exc()
            return result
        finally:
            source.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
    original = GuiSupervisor._unknown_result
    def traced(self, request, reason):
        result['traceback'] = traceback.format_exc()
        result['reason'] = reason
        return original(self, request, reason)
    GuiSupervisor._unknown_result = traced
    supervisor = None
    try:
        windows = discover_maa_windows()
        result['windows'] = windows
        if len(windows) != 1:
            raise RuntimeError('Expected one MAA window')
        supervisor = GuiSupervisor(windows[0], backend='visual', directory=output.parent / 'snapshot-diagnostic')
        result['lock_path'] = str(supervisor.lock_path)
        result['state_path'] = str(supervisor._state_path)
        result['atomic_before'] = [atomic_probe(output.parent / 'atomic-probe'),
                                  atomic_probe(supervisor.lock_path.parent / 'atomic-probe')]
        result['snapshot'] = supervisor.request({'op': 'snapshot'}, timeout=15)
        if '--reconcile-idle' in sys.argv and result['snapshot'].get('status') == 'completed':
            from shu.maa_visual_import import VisualCodeImporter
            importer = VisualCodeImporter(supervisor)
            frame = importer.observe()
            if not (frame.get('page') == 'copilot' and frame.get('idle') is True
                    and frame.get('running') is False and frame.get('single_job') is True
                    and not frame.get('errors')):
                raise RuntimeError('Actual MAA idle import page was not verified; barrier retained')
            evidence = importer.publish('operator-idle-reconciliation', frame)
            result['reconciliation'] = supervisor.reconcile(
                {'reconciled': True, 'evidence_ref': evidence,
                 'kind': 'operator_verified_idle_import_page', 'no_gui_input': True}, resume=True)
        result['atomic_after'] = [atomic_probe(output.parent / 'atomic-probe'),
                                 atomic_probe(supervisor.lock_path.parent / 'atomic-probe')]
        result['health'] = supervisor.health()
    except Exception:
        result['outer_traceback'] = traceback.format_exc()
    finally:
        if supervisor:
            supervisor.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
