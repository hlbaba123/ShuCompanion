"""Launch the isolated import acceptance with durable startup diagnostics."""
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(*, startup_only=False, single_turn=False):
    evidence = ROOT / 'artifacts' / '20261108-maa-gui'
    evidence.mkdir(parents=True, exist_ok=True)
    identity = time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid())
    output = evidence / ('import-admin-' + identity)
    log = evidence / 'latest-import-startup.json'

    def show(message):
        if sys.stdout:
            print(message, flush=True)

    def record(stage, **facts):
        value = {'stage': stage, 'output': str(output), 'pid': os.getpid(), **facts}
        temporary = log.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(log)

    record('starting')
    show('黍 · TL-2 仅导入验收（不开始作战）')
    show('验收日志：' + str(log))
    if startup_only:
        record('startup_checked', model_requests=0, gui_inputs=0)
        show('启动检查通过；未调用模型、未操作窗口。')
        return 0
    try:
        temporary_directory = ROOT.parent / '.pet-temp'
        temporary_directory.mkdir(exist_ok=True)
        os.environ['TEMP'] = os.environ['TMP'] = str(temporary_directory)
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        import main as application
        version = getattr(application, 'APP_VERSION', '')
        candidate = ROOT / 'release' / ('ShuCompanion-' + version + '-win-x64') / 'ShuCompanion.exe'
        executable = candidate if version and candidate.is_file() else Path(sys.executable)

        def run_application(arguments):
            if executable == candidate:
                return subprocess.run([str(candidate), *arguments], cwd=ROOT, check=False).returncode
            original_argv = sys.argv
            try:
                sys.argv = [str(ROOT / 'main.py'), *arguments]
                return application.main()
            finally:
                sys.argv = original_argv

        readiness_output = evidence / ('readiness-' + identity)
        readiness_report = readiness_output / 'maa-readiness.json'
        record('checking_maa', readiness_report=str(readiness_report), executable=str(executable))
        readiness_exit = run_application(['--maa-readiness-output', str(readiness_output)])
        if readiness_exit != 0:
            readiness_code = 'readiness_unavailable'
            if readiness_report.is_file():
                readiness_code = json.loads(readiness_report.read_text(encoding='utf-8')).get('code', readiness_code)
            record('maa_preflight_failed', readiness_code=readiness_code, readiness_report=str(readiness_report),
                   model_requests=0, gui_inputs=0)
            show('MAA 前置检查未通过。检查报告：' + str(readiness_report))
            details = json.loads(readiness_report.read_text(encoding='utf-8')) if readiness_report.is_file() else {}
            if details.get('error_message'):
                show('具体原因：' + details['error_message'])
            return readiness_exit
        questions = evidence / 'import-questions.json'
        if single_turn:
            questions = evidence / ('import-questions-' + identity + '.json')
            questions.write_text(json.dumps([
                '查找《明日方舟》时间啊，向前！活动 TL-2 普通难度的作业，按我的 Box 核验后，'
                '将适合的主选作业导入 MAA 并核实加载结果。只导入，不开始作战。'
            ], ensure_ascii=False, indent=2), encoding='utf-8')
        record('application_loading', readiness_report=str(readiness_report), executable=str(executable))
        show('正在启动黍，由黍自主查询作业并操作 MAA。请保持 MAA 打开。')
        show('运行程序：' + str(executable))
        code = run_application(['--conversation-test', str(questions),
                                '--conversation-output', str(output), '--allow-maa-import-test'])
        record('finished', exit_code=code, readiness_report=str(readiness_report), executable=str(executable))
        show('验收已结束，退出码：' + str(code) + '。报告目录：' + str(output))
        return code
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        record('startup_failed', error_type=type(error).__name__,
               frames=[{'file': Path(frame.filename).name, 'line': frame.lineno,
                        'function': frame.name} for frame in frames[-8:]])
        show('验收启动失败：' + type(error).__name__ + '。已保存启动日志。')
        return 2


def show_result(result, *, single_turn=False):
    """Reload the fixed acceptance child on explicit user retry, retaining elevation."""
    while True:
        try:
            choice = input('按 R 重新加载修复后重试，或按回车关闭：').strip().casefold()
        except EOFError:
            return result
        if choice != 'r':
            return result
        command = [sys.executable, str(Path(__file__).resolve())]
        if single_turn:
            command.append('--single-turn')
        result = subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--startup-only', action='store_true')
    parser.add_argument('--single-turn', action='store_true', help='在同一条要求中完成活动查询、Box 核验和作业导入')
    parser.add_argument('--show-result', action='store_true')
    arguments = parser.parse_args()
    result = main(startup_only=arguments.startup_only, single_turn=arguments.single_turn)
    if arguments.show_result and sys.stdin and sys.stdout:
        result = show_result(result, single_turn=arguments.single_turn)
    raise SystemExit(result)
