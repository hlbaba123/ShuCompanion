"""Read the actual MAA window through Shu's supervised capture and local OCR."""
import argparse
import json
import multiprocessing
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
for name in ('.agent-runtime','.capture-runtime','.gui-runtime'):
    sys.path.insert(0,str(ROOT.parent/name))


def run_probe(output):
    from shu.gui_supervisor import GuiSupervisor
    from shu.maa_visual_native import discover_maa_windows
    from shu.maa_visual_readback import MaaVisualReadback
    from shu.maa_framework_observer import MaaFrameworkOcr
    from shu.maa_visual_import import VisualCodeImporter
    output=Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new output directory')
    output.mkdir(parents=True)
    report={'mode':'live_maafw_readonly','gui_inputs':0,'model_requests':0,'passed':False}
    supervisor=None
    try:
        windows=discover_maa_windows()
        if len(windows)!=1:
            raise ValueError('MAA window must be unique')
        supervisor=GuiSupervisor(windows[0],backend='visual',directory=output,
                                 lock_path=output/'read-only.lock')
        ocr=MaaFrameworkOcr(ROOT.parent/'.maafw-runtime/5.15.0',
                            ROOT/'assets/maa-framework',output/'framework-logs',timeout=20)
        frame=VisualCodeImporter(supervisor,readback=MaaVisualReadback(ocr=ocr)).observe()
        report['observation']=frame
        report['passed']=frame.get('page')=='copilot' and frame.get('idle') is True
    except Exception as error:
        report.update(code=getattr(error,'code',type(error).__name__),error=str(error)[:300])
    finally:
        if supervisor:
            supervisor.close()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    multiprocessing.freeze_support()
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    result=run_probe(parser.parse_args().output)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
