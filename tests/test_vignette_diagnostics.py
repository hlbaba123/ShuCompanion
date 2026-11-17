"""Read-only CLI boundary and isolated production-widget vignette probe tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from tests import test_idle_vignette as fixtures

ROOT=Path(__file__).resolve().parents[1]


class VignetteCliBoundaryTests(unittest.TestCase):
    def run_cli(self,*args):
        return subprocess.run([sys.executable,str(ROOT/'main.py'),*args],capture_output=True,text=True,timeout=15)

    def test_probe_requires_self_test_before_loading_ui_or_profile(self):
        result=self.run_cli('--verify-skin-vignette','sweater')
        self.assertEqual(result.returncode,2)
        self.assertIn('--verify-skin-vignette requires --self-test and an explicit --data-dir',result.stderr)

    def test_probe_requires_explicit_data_directory(self):
        result=self.run_cli('--self-test','--verify-skin-vignette','sweater')
        self.assertEqual(result.returncode,2)
        self.assertIn('--verify-skin-vignette requires --self-test and an explicit --data-dir',result.stderr)

    def test_probe_refuses_unrelated_key_or_input_flags(self):
        result=self.run_cli('--self-test','--verify-skin-vignette','sweater','--data-dir','unused',
                            '--key-file','do-not-open.txt')
        self.assertEqual(result.returncode,2)
        self.assertIn('--verify-skin-vignette accepts only --self-test and --data-dir',result.stderr)

    def test_empty_skin_does_not_fall_through_to_normal_application_startup(self):
        with TemporaryDirectory() as directory:
            result=self.run_cli('--self-test','--verify-skin-vignette','',
                                '--data-dir',str(Path(directory)/'data'))
            self.assertEqual(result.returncode,2)
            self.assertIn('--verify-skin-vignette requires a valid skin ID',result.stderr)


class VignetteProbeTests(unittest.TestCase):
    """Reuse only generated fixture setup; these are never production sprites."""
    setUpClass=classmethod(fixtures.VignetteCanvasTests.setUpClass.__func__)
    setUp=fixtures.VignetteCanvasTests.setUp
    tearDown=fixtures.VignetteCanvasTests.tearDown
    write_manifest=fixtures.VignetteCanvasTests.write_manifest
    def test_probe_refuses_existing_parent_images_without_overwriting_or_creating_data(self):
        from shu.vignette_diagnostics import run_vignette_diagnostics
        output=self.root/'existing-parent';output.mkdir()
        image=output/'normal-before.png';image.write_bytes(b'previous evidence')
        data=output/'new-data'
        result=run_vignette_diagnostics(self.root,data,'sweater')
        self.assertEqual(result,2)
        self.assertEqual(image.read_bytes(),b'previous evidence')
        self.assertFalse(data.exists())
        self.assertEqual(sorted(p.name for p in output.iterdir()),['normal-before.png'])

    def test_probe_module_is_present(self):
        self.assertIsNotNone(importlib.util.find_spec('shu.vignette_diagnostics'))

    def test_probe_uses_real_catalog_frames_and_full_lifecycle_without_credentials(self):
        from shu.vignette_diagnostics import run_vignette_diagnostics
        output=self.root/'diagnostic'
        result=run_vignette_diagnostics(self.root,output/'data','sweater')
        self.assertEqual(result,0)
        report=json.loads((output/'vignette-check.json').read_text(encoding='utf-8'))
        self.assertTrue(report['passed']);self.assertEqual(report['skin_id'],'sweater')
        self.assertEqual(report['network_requests'],0);self.assertEqual(report['game_inputs'],0)
        self.assertEqual(report['enter_frames'],20);self.assertEqual(report['exit_frames'],10)
        self.assertIn('hold_frames',report)
        self.assertEqual(report['hold_frames'],0);self.assertIsNone(report['hold_frame_ms'])
        self.assertEqual(report['hold_unique_frames'],0)
        self.assertEqual(len(report['checks']),65)
        self.assertTrue(all(report['checks'].values()))
        self.assertFalse((output/'data/DeepSeek_API_Key.txt').exists())
        self.assertTrue((output/'normal-before.png').exists())
        self.assertTrue((output/'hold.png').exists())
        self.assertTrue((output/'normal-after.png').exists())

    def add_hold_clip(self):
        fixtures.grid_fixture(self.folder/'cat-hold.png',8,8)
        self.entry['idle_vignette']['hold']={'file':'sweater/cat-hold.png','columns':8,'frames':8,'frame_ms':125}
        self.write_manifest()

    def test_probe_checks_eight_real_hold_frames_loop_and_exact_four_second_cutoff(self):
        from shu.vignette_diagnostics import run_vignette_diagnostics
        self.add_hold_clip();output=self.root/'animated-hold'
        result=run_vignette_diagnostics(self.root,output/'data','sweater')
        self.assertEqual(result,0)
        report=json.loads((output/'vignette-check.json').read_text(encoding='utf-8'))
        self.assertTrue(report['passed']);self.assertIn('hold_frames',report)
        self.assertEqual(report['hold_frames'],8)
        self.assertEqual(report['hold_frame_ms'],125);self.assertEqual(report['hold_unique_frames'],8)
        self.assertIn('hold_cycle_sha256',report)
        self.assertEqual(report['hold_cycles_verified'],4)
        self.assertEqual(report['hold_cycle_sha256'],[report['hold_frame_sha256']]*4)
        for index in range(8):
            self.assertTrue(report['checks'][f'hold_frame_{index:02}'])
        for index in range(1,9):
            self.assertTrue(report['checks'][f'hold_before_frame_{index:02}'])
        for name in ('all_hold_cells_loaded','hold_pixels_change','hold_loop_wraps',
                     'hold_at_exact_start','exit_at_exact_four_seconds','holds_full_four_seconds'):
            self.assertTrue(report['checks'][name],name)
        for index in (0,2,4,6):self.assertTrue((output/f'hold-{index:02}.png').exists())
        for cycle in range(1,5):
            for index in range(8):
                self.assertTrue(report['checks'][f'hold_cycle_{cycle:02}_frame_{index:02}'])
                self.assertTrue(report['checks'][f'hold_cycle_{cycle:02}_pixels_{index:02}'])
            for index in range(1,9):
                self.assertTrue(report['checks'][f'hold_cycle_{cycle:02}_before_frame_{index:02}'])
            if cycle>1:self.assertTrue(report['checks'][f'hold_cycle_{cycle:02}_wraps'])
        self.assertTrue(all(report['checks'].values()))

    def test_probe_rejects_changed_display_pixels_in_later_hold_cycles(self):
        from PySide6.QtGui import QColor
        from shu import pet as pet_module
        from shu.vignette_diagnostics import run_vignette_diagnostics
        self.add_hold_clip();output=self.root/'later-cycle-corruption'
        real_tick=pet_module.PetCanvas._tick
        def corrupt_second_cycle(canvas):
            real_tick(canvas)
            if canvas.idle_vignette_phase=='hold' and 123<=pet_module.time.monotonic()<124:
                canvas._vignette_frames['hold'][0].fill(QColor(250,20,20))
        with patch.object(pet_module.PetCanvas,'_tick',corrupt_second_cycle):
            self.assertEqual(run_vignette_diagnostics(self.root,output/'data','sweater'),1)
        report=json.loads((output/'vignette-check.json').read_text(encoding='utf-8'))
        self.assertFalse(report['checks']['hold_cycle_02_pixels_00'])
        self.assertEqual(report['error'],'hold_cycle_02_pixels_00')

    def test_probe_rejects_hold_cells_that_reuse_identical_pixels(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage, QPainter
        from shu.vignette_diagnostics import run_vignette_diagnostics
        self.add_hold_clip()
        image=QImage(192*8,208,QImage.Format.Format_ARGB32);image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image)
        for index in range(8):painter.fillRect(index*192+60,20,70,180,QColor(30,100,160))
        painter.end();self.assertTrue(image.save(str(self.folder/'cat-hold.png')))
        output=self.root/'static-hold'
        self.assertEqual(run_vignette_diagnostics(self.root,output/'data','sweater'),1)
        report=json.loads((output/'vignette-check.json').read_text(encoding='utf-8'))
        self.assertFalse(report['passed']);self.assertEqual(report['hold_unique_frames'],1)
        self.assertFalse(report['checks']['hold_pixels_change'])
        self.assertEqual(report['error'],'hold_pixels_change')

    def test_final_exe_wrapper_requires_complete_hold_motion_evidence(self):
        from shu.vignette_diagnostics import run_vignette_diagnostics
        self.add_hold_clip();output=self.root/'wrapper-evidence'
        self.assertEqual(run_vignette_diagnostics(self.root,output/'data','sweater'),0)
        report=json.loads((output/'vignette-check.json').read_text(encoding='utf-8'))
        spec=importlib.util.spec_from_file_location('verify_skin_vignette',ROOT/'tools/verify_skin_vignette.py')
        verifier=importlib.util.module_from_spec(spec)
        with patch.object(sys,'path',[str(ROOT/'tools'),*sys.path]):spec.loader.exec_module(verifier)
        verify=getattr(verifier,'animated_hold_verified',None)
        self.assertIsNotNone(verify,'Final EXE wrapper must reject static or incomplete hold evidence')
        self.assertTrue(verify(report))
        for field,value in (('hold_frames',0),('hold_frame_ms',100),('hold_unique_frames',1),
                            ('hold_ms',3999),('hold_frame_sha256',['same']*8),
                            ('hold_cycles_verified',1),('hold_cycle_sha256',[]),
                            ('hold_cycle_sha256',[report['hold_frame_sha256']]*3)):
            with self.subTest(field=field):self.assertFalse(verify({**report,field:value}))
        for name in ('hold_loop_wraps','hold_frame_07','hold_before_frame_08',
                     'exit_at_exact_four_seconds','hold_pixels_change','hold_cycle_03_wraps',
                     'hold_cycle_04_frame_07','hold_cycle_02_pixels_05','hold_cycle_04_before_frame_08'):
            with self.subTest(missing_check=name):
                checks={key:value for key,value in report['checks'].items() if key!=name}
                self.assertFalse(verify({**report,'checks':checks}))
        altered=[list(report['hold_frame_sha256']) for _ in range(4)]
        altered[3][7]=report['hold_frame_sha256'][0]
        self.assertFalse(verify({**report,'hold_cycle_sha256':altered}))

    def test_probe_refuses_existing_data_directory_without_writing_into_it(self):
        from shu.vignette_diagnostics import run_vignette_diagnostics
        output=self.root/'existing';data=output/'data';data.mkdir(parents=True)
        marker=data/'preserve.txt';marker.write_text('preserve')
        result=run_vignette_diagnostics(self.root,data,'sweater')
        self.assertEqual(result,2);self.assertEqual(marker.read_text(),'preserve')
        self.assertEqual(sorted(p.name for p in data.iterdir()),['preserve.txt'])
        self.assertFalse((output/'vignette-check.json').exists())


if __name__=='__main__':unittest.main()
