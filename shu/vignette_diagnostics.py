"""Offline acceptance of production skin pixels and the production idle clock.

This module intentionally imports no controller/provider, never reads a Key or
workspace profile, and uses a new diagnostic output directory exclusively.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def run_vignette_diagnostics(resource_root: Path, data_dir: Path, skin_id: str) -> int:
    data_dir=Path(data_dir).resolve();output=data_dir.parent
    # Reject existing profiles and previous evidence without inspecting them.
    if data_dir.exists() or (output.exists() and any(output.iterdir())):
        if sys.stdout:print(json.dumps({'passed':False,'code':'diagnostic_output_exists'}))
        return 2
    data_dir.mkdir(parents=True)
    os.environ['QT_QPA_PLATFORM']='offscreen'
    os.environ.pop('DEEPSEEK_API_KEY',None)
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from . import pet as pet_module
    from .skins import SkinCatalog

    app=QApplication.instance() or QApplication([])
    report={'passed':False,'skin_id':skin_id,'frozen':bool(getattr(sys,'frozen',False)),
            'method':'actual application PetCanvas and packaged assets, simulated monotonic timestamps; production timeout unchanged',
            'data_dir':str(data_dir),'network_requests':0,'game_inputs':0,'maa_inputs':0,
            'hold_frames':0,'hold_frame_ms':None,'hold_unique_frames':0,
            'hold_cycles_verified':0,'hold_cycle_sha256':[],
            'checks':{},'timeline':[],'snapshots':[]}
    canvas=None;original_time=pet_module.time;original_cursor=pet_module.QCursor
    moment=[0.0]
    pet_module.time=SimpleNamespace(monotonic=lambda:moment[0])
    pet_module.QCursor=SimpleNamespace(pos=lambda:QPoint(10000,10000))
    def check(name,condition):
        report['checks'][name]=bool(condition)
        if not condition:raise AssertionError(name)
    def tick(seconds,label=None):
        moment[0]=seconds;canvas._tick()
        if label:report['timeline'].append({'at':round(seconds,6),'label':label,'phase':canvas.idle_vignette_phase,
                                          'playing':canvas.idle_vignette_playing})
    def snapshot(name):
        path=output/f'{name}.png'
        check('snapshot_'+name,canvas.displayed_frame().save(str(path)))
        report['snapshots'].append(path.name)
    try:
        if report['frozen']:
            with Path(sys.executable).open('rb') as binary:report['exe_sha256']=hashlib.file_digest(binary,'sha256').hexdigest()
        catalog=SkinCatalog(Path(resource_root)/'assets/spritesheet.png')
        skin=catalog.get(skin_id)
        check('requested_skin_exists',skin is not None)
        check('vignette_config_exists',skin.idle_vignette is not None)
        spec=skin.idle_vignette
        hold=getattr(spec,'hold',None)
        report.update(idle_seconds=spec.idle_seconds,hold_ms=spec.hold_ms,
                      enter_frames=spec.enter.frames,exit_frames=spec.exit.frames,
                      hold_frames=hold.frames if hold is not None else 0,
                      hold_frame_ms=hold.frame_ms if hold is not None else None)
        check('production_timeout_120',spec.idle_seconds==120)
        check('production_hold_four_seconds',spec.hold_ms==4000)
        check('enter_twenty_frames',spec.enter.frames==20)
        check('exit_ten_frames',spec.exit.frames==10)
        canvas=pet_module.PetCanvas(catalog.legacy.atlas,native_pixels=True);canvas.timer.stop()
        check('skin_selected',canvas.set_skin(skin,animate=False) and canvas.skin_id==skin_id)
        check('all_standard_cells_loaded',len(canvas.frames)==73)
        check('all_enter_cells_loaded',len(canvas._vignette_frames.get('enter',[]))==20)
        check('all_exit_cells_loaded',len(canvas._vignette_frames.get('exit',[]))==10)
        if hold is not None:
            check('hold_eight_frames',hold.frames==8)
            check('hold_frame_125ms',hold.frame_ms==125)
            check('all_hold_cells_loaded',len(canvas._vignette_frames.get('hold',[]))==8)
        snapshot('normal-before')
        tick(119.99,'before threshold');check('not_before_120',not canvas.idle_vignette_playing)
        tick(120,'threshold');check('starts_at_120',canvas.idle_vignette_playing and canvas.idle_vignette_phase=='enter')
        snapshot('enter-00')
        for index in range(1,spec.enter.frames):
            tick(120+index*spec.enter.frame_ms/1000+.001)
            check(f'enter_frame_{index:02}',canvas.displayed_frame() is canvas._vignette_frames['enter'][index])
        snapshot('enter-19')
        hold_at=120+spec.enter.frames*spec.enter.frame_ms/1000
        tick(hold_at if hold is not None else hold_at+.001,'hold begins')
        check('enters_hold',canvas.idle_vignette_phase=='hold')
        snapshot('hold')
        if hold is not None:
            frames=canvas._vignette_frames['hold'];hashes=[]
            check('hold_at_exact_start',canvas.idle_vignette_phase=='hold' and canvas.displayed_frame() is frames[0])
            def frame_hash(frame):
                pixels=frame.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
                return hashlib.sha256(pixels.constBits()).hexdigest()
            expected_hashes=[frame_hash(frame) for frame in frames]
            report['hold_frame_sha256']=hashes
            for cycle in range(4):
                cycle_name=f'hold_cycle_{cycle+1:02}';cycle_hashes=[]
                report['hold_cycle_sha256'].append(cycle_hashes)
                for index in range(hold.frames):
                    boundary=hold_at+(cycle*hold.frames+index)*hold.frame_ms/1000
                    tick(boundary,f'hold cycle {cycle+1} frame {index:02}')
                    matches=canvas.idle_vignette_phase=='hold' and canvas.displayed_frame() is frames[index]
                    check(f'{cycle_name}_frame_{index:02}',matches)
                    if cycle==0:
                        check(f'hold_frame_{index:02}',matches)
                        if index%2==0:snapshot(f'hold-{index:02}')
                    elif index==0:
                        check(f'{cycle_name}_wraps',matches)
                        if cycle==1:check('hold_loop_wraps',matches)
                    digest=frame_hash(canvas.displayed_frame());cycle_hashes.append(digest)
                    check(f'{cycle_name}_pixels_{index:02}',digest==expected_hashes[index])
                    if cycle==0:hashes.append(digest)
                    tick(boundary+hold.frame_ms/1000-.001)
                    matches=canvas.idle_vignette_phase=='hold' and canvas.displayed_frame() is frames[index]
                    check(f'{cycle_name}_before_frame_{index+1:02}',matches)
                    if cycle==0:check(f'hold_before_frame_{index+1:02}',matches)
                if cycle==0:
                    report['hold_unique_frames']=len(set(hashes))
                    check('hold_pixels_change',report['hold_unique_frames']>=2)
                report['hold_cycles_verified']=cycle+1
        exit_at=hold_at+spec.hold_ms/1000
        tick(exit_at-.001,'hold still present');check('holds_full_four_seconds',canvas.idle_vignette_phase=='hold')
        if hold is not None:
            tick(exit_at,'exact four-second cutoff')
            check('exit_at_exact_four_seconds',canvas.idle_vignette_phase=='exit'
                  and canvas.displayed_frame() is canvas._vignette_frames['exit'][0])
        tick(exit_at+.001,'exit begins');check('enters_exit',canvas.idle_vignette_phase=='exit')
        snapshot('exit-00')
        for index in range(1,spec.exit.frames):
            tick(exit_at+index*spec.exit.frame_ms/1000+.001)
            check(f'exit_frame_{index:02}',canvas.displayed_frame() is canvas._vignette_frames['exit'][index])
        finished=exit_at+spec.exit.frames*spec.exit.frame_ms/1000+.001
        tick(finished,'normal restored')
        check('normal_after_exit',not canvas.idle_vignette_playing and canvas.displayed_frame() is canvas.frames[canvas.current_cell])
        snapshot('normal-after')

        def restart_idle_scene():
            canvas.play('idle');moment[0]+=120;canvas._tick()
            check('restart_scene_'+str(len(report['checks'])),canvas.idle_vignette_playing)
        tick(finished+119.99,'cooldown');check('full_cooldown',not canvas.idle_vignette_playing)
        tick(finished+120.001,'next cycle');check('rearms_after_full_idle',canvas.idle_vignette_playing)
        canvas.play('running');check('action_cancels',not canvas.idle_vignette_playing and canvas.animation.state=='running')
        restart_idle_scene();canvas.dragging=True;tick(moment[0]+.033,'drag')
        check('drag_cancels',not canvas.idle_vignette_playing);canvas.dragging=False
        restart_idle_scene();canvas.set_animated(False)
        check('pause_cancels',not canvas.idle_vignette_playing)
        tick(moment[0]+120);check('paused_stays_daily',not canvas.idle_vignette_playing)
        canvas.set_animated(True);restart_idle_scene();canvas.set_idle_vignette_blocked(True)
        check('dialog_block_cancels',not canvas.idle_vignette_playing)
        canvas.set_idle_vignette_blocked(False);restart_idle_scene();canvas.notify_interaction()
        check('interaction_cancels',not canvas.idle_vignette_playing)
        restart_idle_scene();canvas.set_skin(catalog.legacy,animate=False)
        check('skin_change_cancels',not canvas.idle_vignette_playing)
        tick(moment[0]+120,'legacy');check('legacy_has_no_scene',not canvas.idle_vignette_playing and canvas._vignette_clock is None)
        report['passed']=all(report['checks'].values())
    except Exception as error:
        report.update(error_type=type(error).__name__,error=str(error)[:300])
    finally:
        if canvas is not None:canvas.close();canvas.deleteLater();app.processEvents()
        pet_module.time=original_time;pet_module.QCursor=original_cursor
    (output/'vignette-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if sys.stdout:print(json.dumps({'passed':report['passed'],'skin_id':skin_id,'checks':len(report['checks']),
                                    'report':str(output/'vignette-check.json')},ensure_ascii=True))
    return 0 if report['passed'] else 1
