"""Run a real MAA code import, then the independent pet conversation chain."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts'
HISTORICAL_REPORT = ARTIFACTS / '20261108-maa-gui' / 'import-admin-20261109-151848-17696' / 'report.json'
QUESTION = (
    '查找《明日方舟》时间啊，向前！活动 TL-2 普通难度的作业，按我的 Box 核验后，'
    '把适合的主选作业的神秘代码输入 MAA 并核实加载结果。只导入，不开始作战。'
)
_CODE = re.compile(r'prts://[1-9][0-9]{0,11}')
_MAX_JSON = 8 * 1024 * 1024


def _read_json(path, *, root=None, digest=None):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or (root is not None and not path.is_relative_to(root)):
        raise ValueError('验收凭据不在指定报告目录。')
    with path.open('rb') as stream:
        data = stream.read(_MAX_JSON + 1)
    if len(data) > _MAX_JSON:
        raise ValueError('验收凭据超出大小限制。')
    if digest is not None and hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('验收凭据哈希不匹配。')
    document = json.loads(data)
    if not isinstance(document, dict):
        raise ValueError('验收凭据需要 JSON 对象。')
    return document


def _read_ref(ref, root):
    digest = ref['sha256']
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('验收凭据缺少有效 SHA-256。')
    return _read_json(ref['path'], root=root, digest=digest)


def _read_fixture(report_path):
    report_path = Path(report_path).resolve(strict=True)
    root = report_path.parent
    historical = _read_json(report_path)
    turns = historical.get('turns', [])
    if historical.get('passed') is not True or len(turns) != 1:
        raise ValueError('单项验收需要一份已通过的历史黍导入报告。')
    turn = turns[0]
    receipt = turn['import_verification']
    if receipt.get('verified') is not True or receipt.get('battle_start_submitted') is not False:
        raise ValueError('历史报告缺少可信的导入凭据。')
    refs = [ref for ref in receipt['evidence_refs'] if Path(ref['path']).name.startswith('import-verified-')]
    if len(refs) != 1:
        raise ValueError('历史报告未唯一确定作业来源。')
    proof = _read_ref(refs[0], root)
    source = _read_ref(proof['source_binding']['evidence_ref'], root)
    stage = proof['goal_stage']
    if (_read_ref(stage['evidence_ref'], root) != {key: value for key, value in stage.items() if key != 'evidence_ref'}
            or source['code_ref'] != proof['source_binding']['code_ref']
            or source['resolved_stage']['stage_id'] != proof['stage_name']
            or stage['stage_id'] != proof['stage_name']
            or source['resolved_stage']['display_code'] != stage['code']
            or stage['difficulty'] != proof['difficulty']):
        raise ValueError('历史作业的来源与关卡凭据不一致。')
    code = 'prts://' + proof['copilot_id']
    if not _CODE.fullmatch(code) or proof['source_url'] != f"https://prts.maa.plus/copilot/get/{proof['copilot_id']}":
        raise ValueError('历史报告没有有效的神秘代码来源。')
    items = turn['delivery']['items']
    if sum(item.get('mystery_code') == code and item.get('code') == stage['code'] for item in items) != 1:
        raise ValueError('历史交付结果与导入凭据中的神秘代码不一致。')
    document = _read_ref({'path': proof['path'], 'sha256': proof['sha256']}, root)
    if (document['doc']['title'] != proof['title'] or document['stage_name'] != proof['stage_name']
            or not document.get('actions') or len(document['actions']) != proof['action_count']):
        raise ValueError('历史作业的标题、关卡或动作清单未通过核对。')
    return {'code': code, 'title': proof['title'], 'stage_code': stage['code'],
            'difficulty': proof['difficulty'], 'operator_names': [item['name'] for item in document.get('opers', [])],
            'source_document': document}


def _write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _environment():
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONUTF8='1')
    # Source subprocesses and the spawned GUI helper share the same local runtime.
    runtimes = [ROOT.parent / name for name in ('.capture-runtime', '.agent-runtime', '.gui-runtime', '.companion-runtime')]
    entries = [str(path) for path in runtimes if path.is_dir()]
    if env.get('PYTHONPATH'):
        entries.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(entries)
    for path in reversed(runtimes):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return env


def _run_application(command, args, env, timeout):
    # A console selection can suspend writes even after an acceptance report
    # has been saved. Capture diagnostics in the run folder and never let a
    # child console take focus from MAA.
    output = Path(args[args.index('--maa-readiness-output') + 1]).parent if '--maa-readiness-output' in args else Path(
        args[args.index('--conversation-output') + 1]).parent
    log = output / ('preflight-process.log' if '--maa-readiness-output' in args else 'chain-process.log')
    with log.open('ab') as stream:
        return subprocess.run([*command, *map(str, args)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                              stdout=stream, stderr=subprocess.STDOUT, check=False, timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode


def _run_component(output, target, expected):
    from shu.gui_supervisor import GuiSupervisor
    from shu.maa_visual_import import VisualCodeImporter
    from shu.maa_visual_readback import MaaVisualReadback
    from shu.maa_vision_readback import MaaVisionReadback
    from shu import maa_load_receipt
    from shu.providers import DeepSeekClient, ProviderConfig

    output.mkdir()
    profile = _read_json(ROOT / 'workspace-profile.json')
    data_path = (ROOT / profile['data_dir']).resolve()
    key_path = (ROOT / profile['key_file']).resolve()
    if not key_path.is_relative_to(ROOT.resolve()) or not data_path.is_relative_to(ROOT.resolve()):
        raise ValueError('验收 Key 路径不在项目配置内')
    settings = _read_json(data_path / 'settings.json')
    client = DeepSeekClient(ProviderConfig(api_key_file=key_path, model='deepseek-flash',
                                         base_url=settings.get('base_url', 'https://api.deepseek.com'),
                                         max_tokens=8192, thinking=False, timeout_seconds=45))
    client.TRANSIENT_RETRIES = 0
    metrics = []
    readback = MaaVisionReadback(client=client, local_refiner=MaaVisualReadback(), preview_closeup=False,
                                metrics_callback=metrics.append)
    supervisor = GuiSupervisor(target, backend='visual', directory=output)
    try:
        result = VisualCodeImporter(supervisor, readback=readback, load_monitor=maa_load_receipt).run(expected)
        return {**result, 'vision_metrics': metrics}
    finally:
        supervisor.close()


def _component_verified(result, expected, directory):
    if (result.get('status') != 'import_verified' or result.get('mystery_code') != expected['code']
            or result.get('battle_start_submitted') is not False):
        return False
    ref = result['receipt_ref']
    if ref not in result.get('evidence_refs', []):
        return False
    proof = _read_ref(ref, directory)
    return (proof.get('transport') == 'screenshot_ocr' and proof.get('start_clicked') is False
            and proof.get('code') == expected['code'] and proof.get('expected') == expected
            and len(set(proof.get('after', []))) == 2
            and [a.get('code') for a in proof.get('actions', []) if a.get('action') == 'replace_code']
            == [expected['code']])


def _chain_verified(report):
    if (report.get('passed') is not True or report.get('battle_start_submissions') != 0
            or report.get('verified_import_count') != 1 or len(report.get('turns', [])) != 1):
        return False
    turn = report['turns'][0]
    verification = turn.get('import_verification', {})
    return (turn.get('question') == QUESTION and verification.get('verified') is True
            and verification.get('transport') == 'screenshot_keyboard'
            and verification.get('battle_start_submitted') is False
            and isinstance(verification.get('mystery_code'), str)
            and bool(_CODE.fullmatch(verification['mystery_code'])))


def run(output, source_report, *, executable=None, component_only=False, preflight_only=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    command = [str(executable)] if executable else [sys.executable, str(ROOT / 'main.py')]
    summary = {'mode': 'visual_import_acceptance', 'phase': 'starting', 'passed': False, 'battle_start_authorized': False,
               'output': str(output), 'application': command, 'stages': {}}

    def record(phase, **facts):
        summary['phase'] = phase
        summary['stages'][phase] = facts
        _write_json(output / 'acceptance.json', summary)

    print(f'验收报告 {output / "acceptance.json"}', flush=True)
    try:
        env = _environment()
        record('preflight')
        preflight = output / 'preflight'
        code = _run_application(command, ['--maa-readiness-output', preflight,
                                         '--maa-readiness-wait', 45], env, timeout=75)
        details = _read_json(preflight / 'maa-readiness.json')
        ready = code == 0 and details.get('passed') is True and details.get('mode') == 'maa_visual_readiness'
        record('preflight', passed=ready, code=details.get('code'), message=details.get('error_message'),
               report=str(preflight / 'maa-readiness.json'))
        if not ready:
            print(f"前置检查未通过。{details.get('error_message') or details.get('code')}", flush=True)
            return 2
        if preflight_only:
            summary['passed'] = True
            record('complete', scope='preflight')
            return 0

        record('component')
        expected = _read_fixture(source_report)
        print('第 1 步 验收截图识别、输入神秘代码和作业预览回读。', flush=True)
        component = output / 'component'
        result = _run_component(component, details['window'], expected)
        _write_json(component / 'report.json', result)
        passed = _component_verified(result, expected, component)
        record('component', passed=passed, source_report=str(Path(source_report).resolve()),
               mystery_code=expected['code'], report=str(component / 'report.json'),
               code=result.get('code'), message=result.get('summary'))
        if not passed:
            print(f"单项未通过。{result.get('summary', '')}", flush=True)
            return 3
        if component_only:
            summary['passed'] = True
            record('complete', scope='component')
            return 0

        print('第 2 步 由黍从新任务开始，自主查询、核对阵容、导入并验证。', flush=True)
        questions = output / 'questions.json'
        _write_json(questions, [QUESTION])
        chain = output / 'chain'
        record('chain', report=str(chain / 'report.json'))
        code = _run_application(command, ['--conversation-test', questions, '--conversation-output', chain,
                                         '--allow-maa-import-test'], env, timeout=470)
        details = _read_json(chain / 'report.json')
        passed = code == 0 and _chain_verified(details)
        record('chain', passed=passed, report=str(chain / 'report.json'), exit_code=code)
        if not passed:
            print('整链未通过。请查看整链报告中的任务阶段与凭据。', flush=True)
            return 4
        summary['passed'] = True
        record('complete', scope='component_and_chain')
        print('单项和整链均通过。', flush=True)
        return 0
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, ImportError, subprocess.TimeoutExpired) as error:
        phase = summary['phase']
        record(phase, passed=False, error=type(error).__name__, message=str(error)[:500])
        print(f'验收停在 {phase}。{type(error).__name__}', flush=True)
        return 5


def main(argv=None):
    parser = argparse.ArgumentParser(description='黍的截图键鼠作业导入验收')
    parser.add_argument('--source-report', type=Path, default=HISTORICAL_REPORT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--allow-import', action='store_true', help='允许向已打开的 MAA 输入作业代码')
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument('--preflight-only', action='store_true', help='仅只读检查窗口、截图识别和权限')
    phases.add_argument('--component-only', action='store_true', help='仅验收代码导入')
    args = parser.parse_args(argv)
    if not args.preflight_only and not args.allow_import:
        parser.error('实机代码导入需要 --allow-import')
    output = args.output or (ARTIFACTS / '20261109-visual-import' /
                             (time.strftime('acceptance-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]))
    return run(output, args.source_report, executable=args.executable,
               component_only=args.component_only, preflight_only=args.preflight_only)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
