"""Build a reproducible direct-API desktop baseline without user credentials."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-deps", type=Path, help="Optional local PyInstaller dependency directory")
    parser.add_argument('--version',default=VERSION,help='Isolated release name when another branch is preparing a build')
    parser.add_argument('--incremental',action='store_true',help='Reuse the build cache after source-only changes')
    parser.add_argument('--build-cache',type=Path,help='Reuse an existing build-owned cache under this project build directory')
    parser.add_argument('--maafw-runtime',type=Path,help='Explicit audited optional MaaFramework 5.15.0 runtime')
    parser.add_argument('--assets-dir',type=Path,help='Explicit public asset set; local originals remain untouched')
    args = parser.parse_args()
    import re
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9]+)?',args.version):parser.error('Invalid release version')
    if args.build_deps:
        sys.path.insert(0, str(args.build_deps.resolve()))
    capture_dependencies=ROOT.parent/'.capture-runtime'
    if capture_dependencies.exists():sys.path.insert(0,str(capture_dependencies))
    agent_dependencies=ROOT.parent/'.agent-runtime'
    gui_dependencies=ROOT.parent/'.gui-runtime'
    for dependency in (agent_dependencies,gui_dependencies):
        if dependency.exists():sys.path.insert(0,str(dependency))
    # External tool directories can contain an incompatible ICU with the same
    # filename as Windows' ICU. Qt must resolve its Windows dependency, rather
    # than a Poppler or image conversion tool's DLL from the caller's PATH.
    windows_root = Path(os.environ.get("WINDIR", "C:/Windows"))
    os.environ["PATH"] = os.pathsep.join(str(p) for p in (
        Path(sys.executable).parent, Path(sys.executable).parent / "DLLs",
        windows_root / "System32", windows_root))
    # QImage uses only QtGui; no running desktop or credential access is required.
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    assets = (args.assets_dir or ROOT / 'assets').resolve(strict=True)
    icon_path = assets / "shu.ico"
    image = QImage(str(assets / "spritesheet.png"))
    if image.isNull():
        raise SystemExit("Missing pet atlas")
    icon = image.copy(0, 0, 192, 208).scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)
    if not icon.save(str(icon_path), "ICO"):
        raise SystemExit("Unable to write application icon")
    from PyInstaller.__main__ import run
    # Keep the concurrently maintained voice package's cache/stage separate.
    build = (args.build_cache or ROOT / "build" / f"workflow-{args.version}").resolve()
    if not build.is_relative_to((ROOT / 'build').resolve()) or build == (ROOT / 'build').resolve():
        parser.error('Build cache must be a child of the project build directory')
    if args.build_cache and not args.incremental:
        parser.error('--build-cache requires --incremental')
    release = ROOT / "release"
    for path in (build, release):
        path.mkdir(parents=True, exist_ok=True)
    # Only the source entry point and public assets enter the analysis/data list.
    # Optional agent runtime and Key/data are not collected implicitly.
    run([str(ROOT / "main.py"), "--noconfirm", *([] if args.incremental else ['--clean']), "--onedir", "--windowed",
         "--name", "ShuCompanion", "--icon", str(icon_path),
         "--distpath", str(build / "dist"), "--workpath", str(build / "work"),
         "--specpath", str(build), "--paths", str(ROOT),
         "--paths",str(capture_dependencies),"--collect-all","windows_capture",
         "--paths",str(agent_dependencies),"--paths",str(gui_dependencies),
         "--collect-submodules","langgraph","--collect-submodules","langchain_core",
         "--collect-submodules","comtypes","--copy-metadata","langgraph",
         "--copy-metadata","langgraph-checkpoint-sqlite",
         "--add-data", f"{assets};assets",
         "--add-data", f"{ROOT / 'tools/winrt_ocr.ps1'};tools",
         "--exclude-module", "deepseek_harness", "--exclude-module", "deepseek_harness_runtime",
         "--exclude-module", "maa", "--exclude-module", "PySide6.QtTest",
         "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets"])
    destination = release / f"ShuCompanion-{args.version}-win-x64"
    existing_key = destination / "DeepSeek_API_Key.txt"
    if existing_key.exists():
        existing_lines = existing_key.read_text(encoding="utf-8-sig").splitlines()
        if any(line.strip() and not line.lstrip().startswith("#") for line in existing_lines):
            raise SystemExit("Release folder contains a filled Key file; choose a new release version")
    if (destination / "data").exists():
        raise SystemExit("Release folder contains runtime data; choose a new release version")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(build / "dist/ShuCompanion", destination, dirs_exist_ok=True)
    if args.maafw_runtime:
        runtime=args.maafw_runtime.resolve(strict=True)
        metadata=runtime/'maafw-5.15.0.dist-info/METADATA'
        if not metadata.is_file() or not set(metadata.read_text(encoding='utf-8').splitlines()) & {'Version: 5.15.0','Version: v5.15.0'}:
            raise SystemExit('Expected audited MaaFramework 5.15.0')
        target=destination/'_internal/optional-runtime/5.15.0'
        for name in ('maa','strenum','maafw-5.15.0.dist-info','StrEnum-0.4.15.dist-info'):
            shutil.copytree(runtime/name,target/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'),dirs_exist_ok=True)
        inventory={str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in target.rglob('*') if p.is_file()}
        (destination/'maafw-runtime-manifest.json').write_text(json.dumps(
            {'version':'5.15.0','files':inventory},indent=2),encoding='utf-8')
    # An analysis can capture old code before a source edit finishes. A newer
    # PYZ timestamp alone cannot establish that its modules match the source.
    subprocess.run([sys.executable,str(ROOT/"tools/verify_frozen_sources.py"),
                    str(destination/"ShuCompanion.exe"),"--output",str(build/"frozen-sources.json")],
                   check=True)
    # Remove only the two known stale tool DLLs from an earlier build-owned stage.
    for name in ("icuuc.dll", "icudt78.dll"):
        staged = (destination / "_internal" / name).resolve()
        if not staged.is_relative_to(destination.resolve()):
            raise SystemExit("Invalid staging path")
        if staged.exists() and not (build / "dist/ShuCompanion/_internal" / name).exists():
            staged.unlink()
    # Never copy the real local Key. Generate a fresh commented template every build.
    (destination / "DeepSeek_API_Key.txt").write_text(
        "# 在下一行填写 DeepSeek API Key，保存后在设置中重新读取。\n"
        "# 请勿将填写后的文件上传或发送给别人。\n\n", encoding="utf-8")
    guide_name = ("docs/毛衣与猫咪新皮肤_20261116.md" if args.version.endswith(('-catskin','-catmotion')) else
                  "docs/导航与单次MAA启动_20261112.md" if args.version.endswith(("-battle","-navfix","-background","-readback","-foreground","-control")) else
                  "docs/任务切换与旧输入屏障修复_20261112.md" if args.version.endswith("-recovery") else
                  "docs/启动旧库迁移修复_20261112.md" if args.version.endswith("-startup") else
                  "docs/双思考模式验收版使用说明.md" if args.version.endswith("-thinking") else
                  "docs/连续任务与本地作业版使用说明.md" if args.version.endswith("-continuity") else
                  "docs/多作业导入验收版使用说明.md" if args.version.endswith("-batch") else
                  "docs/MAA自动切窗版使用说明.md" if args.version.endswith("-focus") else
                  "docs/MAA授权修复版使用说明.md" if args.version.endswith("-access") else
                  "docs/实时单项导入版使用说明.md" if args.version.endswith("-live") else
                  "docs/目录与滚动修复版使用说明.md" if args.version.endswith("-catalog") else
                  "docs/导入准备验收版使用说明.md" if args.version.endswith("-handoff") else "README.md")
    guide_path = ROOT / guide_name
    guide = guide_path.read_text(encoding="utf-8")
    guide = (guide.replace('docs/截图视觉导入进展_20261109.md', '截图视觉导入进展.md')
             .replace('docs/项目导航.md', '项目导航.md')
             .replace('docs/查询与导入范围及多作业验收_20261111.md', '查询与导入范围及多作业验收.md'))
    (destination / "使用说明.md").write_text(guide, encoding="utf-8")
    if args.version.endswith('-thinking'):
        result_doc = ROOT / 'docs/双思考模式三轮正式复验_20261112.md'
        if result_doc.is_file():
            shutil.copy2(result_doc, destination / result_doc.name)
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", destination / "THIRD_PARTY_NOTICES.md")
    licenses = destination / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(Path(sys.executable).parent / "LICENSE.txt", licenses / "Python.txt")
    for package in ("PySide6_Essentials", "shiboken6", "pyinstaller","windows-capture","numpy","opencv-python",
                    "langgraph","langgraph-checkpoint","langgraph-checkpoint-sqlite","langchain-core","comtypes"):
        distribution = importlib.metadata.distribution(package)
        for entry in distribution.files or ():
            # Preserve original paths: flattening overwrites different files
            # called LICENSE and loses nested third-party attributions.
            parts = [part.casefold() for part in Path(entry).parts]
            if (any(part in {'license','licenses','licence','licences'} for part in parts)
                or Path(entry).name.upper().startswith(('LICENSE','LICENCE','COPYING','COPYRIGHT','NOTICE'))):
                license_path = Path(distribution.locate_file(entry))
                if license_path.is_file():
                    target = licenses / package / Path(entry)
                    if not target.resolve().is_relative_to(licenses.resolve()):
                        raise SystemExit('Invalid dependency license path')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(license_path, target)
    (licenses / "Qt-Open-Source.md").write_text(
        "Qt for Python package metadata declares LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only.\n"
        "Upstream license texts and source: https://code.qt.io/cgit/pyside/pyside-setup.git/tree/LICENSES\n"
        "Qt source and licenses: https://code.qt.io/cgit/qt/qtbase.git/tree/LICENSES\n",
        encoding="utf-8")
    # Ship current navigation and GUI scope, rather than superseded execution
    # designs. The guide explicitly distinguishes source trials from EXE features.
    for source,title in [
        (ROOT/'docs/查询与导入范围及多作业验收_20261111.md','查询与导入范围及多作业验收.md'),
        (ROOT/'docs/MaaFramework辅助识别接入调研与建议_20261110.md','MaaFramework辅助识别接入调研与建议_20261110.md'),
        (ROOT/'docs/MAA自动激活与加载核验修复_20261111.md','MAA自动激活与加载核验修复.md'),
        (ROOT/'docs/MAA授权与任务续接修复验收_20261110.md','MAA授权与任务续接修复验收.md'),
        (ROOT/'docs/MaaFramework实时单项导入验收_20261110.md','MaaFramework实时单项导入验收.md'),
        (ROOT/'docs/目录与滚动及识别验收_20261110.md','目录与滚动及识别验收.md'),
        (ROOT/'docs/MaaFramework截图识别验证_20261110.md','MaaFramework截图识别验证.md'),
        (ROOT/'docs/查询结果与导入准备交接验收_20261110.md','查询结果与导入准备交接验收.md'),
        (ROOT/'docs/当前开发进度与验收顺序_20261110.md','当前开发进度与验收顺序.md'),
        (ROOT/'docs/任务续接与回收箱验收_20261110.md','任务续接与回收箱验收.md'),
        (ROOT/'docs/superpowers/specs/2026-11-09-task-dialogue-and-batch-import-design.md','任务续接与多作业设计.md'),
        (ROOT/'docs/作业导入阶段验收_20261109.md','作业导入阶段验收.md'),
        (ROOT/'docs/superpowers/specs/2026-11-09-vision-import-revision.md','截图与视觉导入设计.md'),
        (ROOT/'docs/截图视觉导入进展_20261109.md','截图视觉导入进展.md'),
        (ROOT/'docs/主备代码与动态活动验收_20261108.md','主备代码与动态活动验收.md'),
        (ROOT/'docs/superpowers/specs/2026-11-07-leader-plan-code-delivery-design.md','动态活动与主备代码设计.md'),
        (ROOT/'docs/项目导航.md','项目导航.md'),
        (ROOT/'docs/分步验收_联网阶段_20261106.md','联网分步验收.md'),
        (ROOT/'docs/桌宠入口与任务进度验收_20261106.md','桌宠入口与任务进度验收.md'),
        (ROOT/'docs/Leader理解与动态调度验收_20261107.md','Leader理解与动态调度验收.md'),
        (ROOT/'docs/作业站只读获取与理解修复_20261107.md','作业站只读获取与理解修复.md'),
        (ROOT/'docs/作业查询效率与分组进度验收_20261107.md','作业查询效率与分组进度验收.md'),
        (ROOT/'docs/真实交付与阵容筛选验收_20261107.md','真实交付与阵容筛选验收.md'),
        (ROOT/'docs/superpowers/specs/2026-11-07-proof-backed-research.md','真实交付设计.md'),
        (ROOT/'docs/superpowers/specs/2026-11-06-shu-langgraph-maa-multiagent-design.md','GUI主线设计.md'),
    ]:
        if source.exists():shutil.copy2(source,destination/title)
    # The release folder is build-owned. Reject unexpected private data instead of copying it.
    forbidden = [p for p in destination.rglob("*") if p.suffix in {'.sqlite3','.db'} or p.name in {"settings.json", "session.lock","workspace-profile.json","profile-preparation.json"}]
    if forbidden or (destination / "data").exists():
        raise SystemExit("Release staging contains runtime data; use a new version directory")
    if args.version.endswith('-public'):
        # The Widgets application does not use these optional plugins. Do not
        # reintroduce the unneeded GPL-only virtual keyboard on future builds.
        for relative in ('plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll',
                'Qt6VirtualKeyboard.dll','Qt6Qml.dll','Qt6QmlMeta.dll',
                'Qt6QmlModels.dll','Qt6QmlWorkerScript.dll','Qt6Quick.dll',
                'plugins/imageformats/qpdf.dll','Qt6Pdf.dll'):
            target=(destination/'_internal/PySide6'/relative).resolve()
            if not target.is_relative_to((destination/'_internal/PySide6').resolve()):
                raise SystemExit('Invalid optional Qt path')
            target.unlink(missing_ok=True)
        if (ROOT/'licenses').is_dir():
            shutil.copytree(ROOT/'licenses',licenses,dirs_exist_ok=True)
        if (ROOT/'THIRD_PARTY_INVENTORY.json').is_file():
            shutil.copy2(ROOT/'THIRD_PARTY_INVENTORY.json',destination/'THIRD_PARTY_INVENTORY.json')
        for document in ('PRIVACY.md','DEPENDENCY_SOURCES.md','PUBLICATION_REVIEW.md'):
            if (ROOT/'docs'/document).is_file():
                shutil.copy2(ROOT/'docs'/document,destination/document)
    archive = release / f"ShuCompanion-{args.version}-win-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(release))
    report = {"version": args.version, "target": "Windows x64", "backend": "DeepSeek direct API",
              "harness_bundled": False, "user_key_bundled": False,
              "zip": archive.name, "zip_bytes": archive.stat().st_size,
              "sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()}
    (release / f"release-manifest-{args.version}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
