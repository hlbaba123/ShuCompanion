"""Offline optional idle-vignette timing and sprite loading tests."""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from shu.pet import FloatingPet, PetCanvas
from shu.skins import SkinCatalog
from tests.test_skins import atlas_fixture


def clock(**kwargs):
    from shu.idle_vignette import IdleVignetteClock
    return IdleVignetteClock(now=0,**kwargs)


class VignetteClockTests(unittest.TestCase):
    def test_optional_clock_is_implemented_separately_from_standard_actions(self):
        self.assertIsNotNone(importlib.util.find_spec('shu.idle_vignette'))

    def test_uses_full_120_seconds_of_uninterrupted_idle(self):
        c=clock()
        self.assertIsNone(c.advance(119.999))
        self.assertEqual(c.advance(120),('enter',0))
        self.assertEqual(c.advance(120.1),('enter',1))

    def test_sequence_holds_the_final_enter_frame_then_exits_and_rearms(self):
        c=clock(enter_frames=2,exit_frames=2,enter_frame_ms=100,exit_frame_ms=100,hold_ms=4000)
        self.assertEqual(c.advance(120),('enter',0))
        self.assertEqual(c.advance(120.1),('enter',1))
        self.assertEqual(c.advance(120.201),('enter',1));self.assertEqual(c.phase,'hold')
        self.assertEqual(c.advance(124.199),('enter',1))
        self.assertEqual(c.advance(124.201),('exit',0))
        self.assertEqual(c.advance(124.301),('exit',1))
        self.assertIsNone(c.advance(124.401));self.assertEqual(c.phase,'idle')
        self.assertIsNone(c.advance(244.4))
        self.assertEqual(c.advance(244.401),('enter',0))

    def test_interaction_cancels_and_requires_another_complete_idle_window(self):
        c=clock();c.advance(120);c.interact(120.04)
        self.assertEqual(c.phase,'idle');self.assertIsNone(c.advance(240.039))
        self.assertEqual(c.advance(240.04),('enter',0))

    def test_busy_hidden_paused_or_dialog_time_is_not_idle_credit(self):
        c=clock();self.assertIsNone(c.advance(130,eligible=False))
        self.assertIsNone(c.advance(249.999))
        self.assertEqual(c.advance(250),('enter',0))
        self.assertIsNone(c.advance(250.01,eligible=False));self.assertEqual(c.phase,'idle')

    def test_long_animation_stall_cancels_instead_of_skipping_the_whole_scene(self):
        c=clock();c.advance(120)
        self.assertIsNone(c.advance(130));self.assertEqual(c.phase,'idle')
        self.assertIsNone(c.advance(249.999));self.assertEqual(c.advance(250),('enter',0))

    def test_backwards_clock_resets_safely(self):
        c=clock();c.advance(120)
        self.assertIsNone(c.advance(10));self.assertEqual(c.phase,'idle')

    def test_optional_hold_clip_advances_and_wraps_without_extending_four_second_hold(self):
        c=clock(hold_frames=8,hold_frame_ms=125)
        c.advance(120)
        self.assertEqual(c.advance(122),('hold',0))
        self.assertEqual(c.advance(122.125),('hold',1))
        self.assertEqual(c.advance(122.875),('hold',7))
        self.assertEqual(c.advance(123),('hold',0))
        self.assertEqual(c.advance(125.999),('hold',7))
        self.assertEqual(c.phase,'hold')
        self.assertEqual(c.advance(126),('exit',0))
        self.assertEqual(c.phase,'exit')

    def test_optional_hold_handles_irregular_ticks_and_partial_final_cycle(self):
        c=clock(enter_frames=2,hold_frames=8,hold_frame_ms=125,hold_ms=1370)
        c.advance(120)
        self.assertEqual(c.advance(120.2),('hold',0))
        self.assertEqual(c.advance(120.581),('hold',3))
        self.assertEqual(c.advance(121.561),('hold',2))
        self.assertEqual(c.advance(121.57),('exit',0))

    def test_zero_hold_skips_optional_loop_and_enters_exit_on_time(self):
        c=clock(enter_frames=2,hold_frames=8,hold_frame_ms=125,hold_ms=0)
        c.advance(120)
        self.assertEqual(c.advance(120.2),('exit',0))
        self.assertEqual(c.phase,'exit')

    def test_hold_loop_can_be_cancelled_and_uses_full_new_quiet_window(self):
        c=clock(hold_frames=8,hold_frame_ms=125)
        c.advance(120);c.advance(122.5);c.interact(122.6)
        self.assertIsNone(c.frame_reference)
        self.assertIsNone(c.advance(242.599))
        self.assertEqual(c.advance(242.6),('enter',0))

    def test_optional_hold_rejects_invalid_timing(self):
        for values in ({'hold_frames':-1},{'hold_frame_ms':0}):
            with self.subTest(values=values),self.assertRaises(ValueError):clock(**values)


def grid_fixture(path,columns,count):
    rows=(count+columns-1)//columns
    image=QImage(columns*192,rows*208,QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent);p=QPainter(image)
    for index in range(count):
        p.fillRect((index%columns)*192+60,(index//columns)*208+20,70,180,
                   QColor(30+index*3,100,160))
    p.end();image.save(str(path))


class VignetteCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if not isinstance(cls.app,QApplication):raise unittest.SkipTest('QApplication required')
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.assets=self.root/'assets'
        self.original=self.assets/'spritesheet.png';atlas_fixture(self.original,'#aabbcc')
        self.folder=self.assets/'skins/sweater';self.folder.mkdir(parents=True)
        atlas_fixture(self.folder/'spritesheet.png','#8aaa88')
        grid_fixture(self.folder/'cat-enter.png',5,20);grid_fixture(self.folder/'cat-exit.png',5,10)
        self.entry={'id':'sweater','label':'sweater','atlas':'sweater/spritesheet.png',
                    'idle_vignette':{'enter':{'file':'sweater/cat-enter.png','columns':5,'frames':20,'frame_ms':100},
                                     'exit':{'file':'sweater/cat-exit.png','columns':5,'frames':10,'frame_ms':100},
                                     'idle_seconds':120,'hold_ms':4000}}
        self.manifest=self.assets/'skins/manifest.json';self.write_manifest()
        self.canvas=None;self.pet=None

    def write_manifest(self):
        self.manifest.write_text(json.dumps({'version':1,'default_skin':'sweater','skins':[self.entry]}),encoding='utf-8')

    def make_canvas(self):
        with patch('shu.pet.time.monotonic',return_value=0):
            self.canvas=PetCanvas(self.original);self.canvas.timer.stop()
            self.assertTrue(self.canvas.set_skin(SkinCatalog(self.original).get('sweater'),animate=False))
        return self.canvas

    def tick(self,seconds):
        with patch('shu.pet.time.monotonic',return_value=seconds):self.canvas._tick()

    def tearDown(self):
        for widget in ((self.pet,) if self.pet is not None else (self.canvas,)):
            if widget is not None:widget.close();widget.deleteLater()
        self.app.processEvents();self.temp.cleanup()

    def test_catalog_loads_optional_clip_and_retains_legacy(self):
        catalog=SkinCatalog(self.original);skin=catalog.get('sweater')
        self.assertTrue(hasattr(skin,'idle_vignette'))
        self.assertEqual(skin.idle_vignette.enter.frames,20)
        self.assertEqual(skin.idle_vignette.enter.columns,5)
        self.assertEqual(skin.idle_vignette.idle_seconds,120)
        self.assertIsNone(catalog.legacy.idle_vignette)

    def add_hold_clip(self):
        grid_fixture(self.folder/'cat-hold.png',8,8)
        self.entry['idle_vignette']['hold']={'file':'sweater/cat-hold.png','columns':8,'frames':8,'frame_ms':125}
        self.write_manifest()

    def test_optional_hold_clip_is_loaded_and_changes_displayed_pixels_during_hold(self):
        self.add_hold_clip()
        skin=SkinCatalog(self.original).get('sweater')
        self.assertIsNotNone(getattr(skin.idle_vignette,'hold',None))
        self.assertEqual(skin.idle_vignette.hold.frames,8)
        c=self.make_canvas();self.tick(120);self.tick(122)
        self.assertEqual(c.idle_vignette_phase,'hold')
        first=c.displayed_frame().toImage()
        self.tick(122.125)
        second=c.displayed_frame().toImage()
        self.assertNotEqual(first.pixelColor(60,20),second.pixelColor(60,20))
        self.tick(123)
        self.assertEqual(c.displayed_frame().toImage(),first)
        self.tick(125.999);self.assertEqual(c.idle_vignette_phase,'hold')
        self.tick(126);self.assertEqual(c.idle_vignette_phase,'exit')

    def test_invalid_optional_hold_metadata_does_not_escape_the_skin_root(self):
        self.add_hold_clip();self.entry['idle_vignette']['hold']['file']='../../spritesheet.png';self.write_manifest()
        skin=SkinCatalog(self.original).get('sweater')
        self.assertIsNone(skin.idle_vignette)

    def test_corrupt_optional_hold_image_keeps_daily_skin_and_disables_scene(self):
        self.add_hold_clip();(self.folder/'cat-hold.png').write_bytes(b'invalid image')
        c=self.make_canvas();self.tick(120)
        self.assertEqual(c.skin_id,'sweater')
        self.assertFalse(c.idle_vignette_playing)

    def test_hold_loop_action_preemption_is_immediate(self):
        self.add_hold_clip();c=self.make_canvas();self.tick(120);self.tick(122.25)
        self.assertEqual(c.idle_vignette_phase,'hold')
        with patch('shu.pet.time.monotonic',return_value=122.3):c.play('running')
        self.assertFalse(c.idle_vignette_playing)
        self.assertEqual(c.animation.state,'running')

    def test_optional_paths_cannot_leave_the_skin_root(self):
        self.entry['idle_vignette']['enter']['file']='../../spritesheet.png';self.write_manifest()
        skin=SkinCatalog(self.original).get('sweater')
        self.assertTrue(hasattr(skin,'idle_vignette'));self.assertIsNone(skin.idle_vignette)

    def test_invalid_optional_metadata_is_ignored_without_losing_daily_skin(self):
        for field,value in [('columns',0),('frames',True),('frame_ms',-10)]:
            with self.subTest(field=field):
                self.entry['idle_vignette']['enter'][field]=value;self.write_manifest()
                skin=SkinCatalog(self.original).get('sweater')
                self.assertTrue(hasattr(skin,'idle_vignette'));self.assertIsNone(skin.idle_vignette)
                self.entry['idle_vignette']['enter'].update(columns=5,frames=20,frame_ms=100)

    def test_20_frame_scene_only_after_timeout_and_action_immediately_preempts_it(self):
        c=self.make_canvas();self.tick(119.999)
        self.assertTrue(hasattr(c,'idle_vignette_playing'));self.assertFalse(c.idle_vignette_playing)
        self.tick(120);self.assertTrue(c.idle_vignette_playing)
        self.assertEqual(c.displayed_frame().toImage().pixelColor(60,20).red(),30)
        self.tick(120.101)
        self.assertEqual(c.displayed_frame().toImage().pixelColor(60,20).red(),33)
        with patch('shu.pet.time.monotonic',return_value=120.15):c.play('running')
        self.assertFalse(c.idle_vignette_playing);self.assertEqual(c.animation.state,'running')

    def test_interaction_and_pause_cancel_scene_and_reset_timer(self):
        c=self.make_canvas();self.tick(120)
        self.assertTrue(hasattr(c,'notify_interaction'))
        with patch('shu.pet.time.monotonic',return_value=120.1):c.notify_interaction()
        self.assertFalse(c.idle_vignette_playing);self.tick(240.099);self.assertFalse(c.idle_vignette_playing)
        self.tick(240.1);self.assertTrue(c.idle_vignette_playing)
        with patch('shu.pet.time.monotonic',return_value=240.2):c.set_animated(False)
        self.assertFalse(c.idle_vignette_playing)

    def test_hidden_or_busy_surface_does_not_start_or_keep_a_scene(self):
        c=self.make_canvas();self.assertTrue(hasattr(c,'set_idle_vignette_blocked'))
        with patch('shu.pet.time.monotonic',return_value=100):c.set_idle_vignette_blocked(True)
        self.tick(400);self.assertFalse(c.idle_vignette_playing)
        with patch('shu.pet.time.monotonic',return_value=401):c.set_idle_vignette_blocked(False)
        self.tick(520.999);self.assertFalse(c.idle_vignette_playing)
        self.tick(521);self.assertTrue(c.idle_vignette_playing)

    def test_invalid_grid_padding_disables_only_optional_scene(self):
        self.entry['idle_vignette']['enter'].update(frames=19);self.write_manifest()
        c=self.make_canvas();self.tick(120)
        self.assertTrue(hasattr(c,'idle_vignette_playing'));self.assertFalse(c.idle_vignette_playing)
        self.assertEqual(c.skin_id,'sweater')

    def test_skin_change_cancels_scene_and_legacy_never_starts_one(self):
        c=self.make_canvas();self.tick(120)
        with patch('shu.pet.time.monotonic',return_value=120.1):c.set_skin(SkinCatalog(self.original).legacy,animate=False)
        self.assertTrue(hasattr(c,'idle_vignette_playing'));self.assertFalse(c.idle_vignette_playing)
        self.tick(500);self.assertFalse(c.idle_vignette_playing)

    def make_pet(self):
        with patch('shu.pet.time.monotonic',return_value=0):
            self.pet=FloatingPet(self.original);self.canvas=self.pet.canvas
            self.canvas.timer.stop();self.pet._hover_timer.stop()
            self.pet.configure_skins(catalog=SkinCatalog(self.original));self.pet.show()
            self.app.processEvents();self.pet._update_hover()
        return self.pet

    def test_hidden_parent_requires_full_quiet_time_after_showing_again(self):
        pet=self.make_pet();self.tick(120);self.assertTrue(self.canvas.idle_vignette_playing)
        with patch('shu.pet.time.monotonic',return_value=120.1):pet.hide()
        self.tick(400);self.assertFalse(self.canvas.idle_vignette_playing)
        with patch('shu.pet.time.monotonic',return_value=401):pet.show();pet._update_hover()
        self.tick(520.999);self.assertFalse(self.canvas.idle_vignette_playing)
        self.tick(521);self.assertTrue(self.canvas.idle_vignette_playing)

    def test_chat_blocks_scene_even_when_daily_animation_has_returned_to_idle(self):
        pet=self.make_pet()
        with patch('shu.pet.time.monotonic',return_value=100):pet.show_composer()
        with patch('shu.pet.time.monotonic',return_value=110):self.canvas.play('idle');pet._update_hover()
        self.tick(400);self.assertFalse(self.canvas.idle_vignette_playing)
        with patch('shu.pet.time.monotonic',return_value=401):pet._dismiss_input();pet._update_hover()
        self.tick(521);self.assertTrue(self.canvas.idle_vignette_playing)

    def test_opening_menu_cancels_scene_before_the_next_timer_tick(self):
        pet=self.make_pet();self.tick(120);self.assertTrue(self.canvas.idle_vignette_playing)
        with patch('shu.pet.time.monotonic',return_value=120.1):pet.show_menu()
        self.assertFalse(self.canvas.idle_vignette_playing)
        pet._update_hover();self.tick(400);self.assertFalse(self.canvas.idle_vignette_playing)

    def test_cursor_moving_elsewhere_does_not_reset_but_near_pet_movement_does(self):
        c=self.make_canvas()
        with patch('shu.pet.QCursor.pos',return_value=QPoint(10000,10000)):self.tick(119.9)
        with patch('shu.pet.QCursor.pos',return_value=QPoint(10001,10000)):self.tick(120)
        self.assertTrue(c.idle_vignette_playing)
        with patch('shu.pet.QCursor.pos',return_value=c.mapToGlobal(QPoint(90,90))):self.tick(120.1)
        self.assertFalse(c.idle_vignette_playing)


if __name__=='__main__':unittest.main()
