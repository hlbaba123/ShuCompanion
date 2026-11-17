"""Offline local-appearance tests; no account, model, voice or game operations."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from shu.pet import FloatingPet, PetCanvas
from shu.pet_core import STATES
from shu.skins import LEGACY_SKIN, SkinCatalog, SkinChange, load_skin_preference, save_skin_preference


def atlas_fixture(path,color):
    """Geometric test pixels only, never used as production character artwork."""
    image=QImage(1536,2288,QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent);painter=QPainter(image)
    for row in range(11):
        count=len(list(STATES.values())[row][1]) if row<9 else 8
        for column in range(count):painter.fillRect(column*192+60,row*208+20,72,180,QColor(color))
    painter.end();path.parent.mkdir(parents=True,exist_ok=True);image.save(str(path))


def transition_fixture(path,count,color):
    image=QImage(192*count,208,QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent);painter=QPainter(image)
    for index in range(count):painter.fillRect(index*192+65+index,20,62,180,QColor(color))
    painter.end();image.save(str(path))


class SkinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if not isinstance(cls.app,QApplication):raise unittest.SkipTest('GUI tests require QApplication')
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.assets=self.root/'assets'
        self.original=self.assets/'spritesheet.png';atlas_fixture(self.original,'#aabbcc')
        self.skins=self.assets/'skins';self.skins.mkdir()
        entries=[]
        for skin_id,color in [('default-corrected','#e4bf70'),('purple','#963c81')]:
            folder=self.skins/skin_id;folder.mkdir();atlas_fixture(folder/'spritesheet.png',color)
            transition_fixture(folder/'transition-out.png',4,color)
            transition_fixture(folder/'transition-in.png',5,color)
            entries.append({'id':skin_id,'label':skin_id,'atlas':f'{skin_id}/spritesheet.png',
                            'transition_out':f'{skin_id}/transition-out.png','transition_in':f'{skin_id}/transition-in.png'})
        self.manifest=self.skins/'manifest.json'
        self.manifest.write_text(json.dumps({'version':1,'default_skin':'default-corrected','skins':entries}),encoding='utf-8')
        self.catalog=SkinCatalog(self.original)
        self.pet=None

    def tearDown(self):
        if self.pet is not None:self.pet.close();self.pet.deleteLater();self.app.processEvents()
        self.temp.cleanup()

    def make_pet(self):
        self.pet=FloatingPet(self.original);self.pet.canvas.timer.stop()
        self.preference=self.root/'profile'/'pet-appearance.json'
        self.pet.configure_skins(self.preference,catalog=self.catalog)
        return self.pet

    def test_catalog_retains_legacy_and_rejects_paths_outside_skin_folder(self):
        raw=json.loads(self.manifest.read_text())
        raw['skins'] += [{'id':'outside','label':'outside','atlas':'../spritesheet.png'},
                         {'id':'missing','label':'missing','atlas':'absent.png'},
                         {'id':'purple','label':'duplicate','atlas':'default-corrected/spritesheet.png'}]
        self.manifest.write_text(json.dumps(raw))
        catalog=SkinCatalog(self.original)
        self.assertEqual(set(catalog.skins),{LEGACY_SKIN,'purple','default-corrected'})
        self.assertEqual(catalog.get('purple').label,'purple')
        self.assertEqual(catalog.ordered()[-1].id,LEGACY_SKIN)

    def test_invalid_manifest_has_the_original_as_the_only_skin(self):
        self.manifest.write_text('{broken')
        catalog=SkinCatalog(self.original)
        self.assertEqual(catalog.default_id,LEGACY_SKIN)
        self.assertEqual(list(catalog.skins),[LEGACY_SKIN])

    def test_manifest_can_hide_legacy_and_migrate_an_old_saved_skin(self):
        raw=json.loads(self.manifest.read_text())
        raw['include_legacy']=False
        self.manifest.write_text(json.dumps(raw))
        self.catalog=SkinCatalog(self.original)
        preference=self.root/'profile'/'pet-appearance.json'
        save_skin_preference(preference,LEGACY_SKIN)
        pet=self.make_pet()
        self.assertEqual(set(pet.skin_actions),{'default-corrected','purple'})
        self.assertIsNone(self.catalog.get(LEGACY_SKIN))
        self.assertEqual(pet.canvas.skin_id,'default-corrected')
        self.assertEqual(load_skin_preference(preference),'default-corrected')

    def test_empty_opt_out_manifest_keeps_a_working_fallback(self):
        self.manifest.write_text(json.dumps({'version':1,'include_legacy':False,'skins':[]}))
        catalog=SkinCatalog(self.original)
        self.assertEqual(catalog.default_id,LEGACY_SKIN)
        self.assertEqual([skin.id for skin in catalog.ordered()],[LEGACY_SKIN])

    def test_initial_selection_persists_only_local_appearance(self):
        other=self.root/'profile'/'settings.json';other.parent.mkdir();other.write_text('{"model":"test","voice":"preserve"}')
        before=other.read_bytes();pet=self.make_pet()
        self.assertEqual(pet.canvas.skin_id,'default-corrected')
        self.assertEqual(load_skin_preference(self.preference),'default-corrected')
        self.assertEqual(other.read_bytes(),before)
        self.assertEqual(len(pet.skin_actions),3)

    def test_reconfiguring_the_current_skin_keeps_its_selection_and_preference(self):
        pet=self.make_pet();before=self.preference.read_bytes();changes=[]
        pet.skin_changed.connect(changes.append)
        pet.configure_skins(self.preference,catalog=self.catalog)
        self.assertEqual(pet.canvas.skin_id,'default-corrected')
        self.assertTrue(pet.skin_actions['default-corrected'].isChecked())
        self.assertEqual(self.preference.read_bytes(),before)
        self.assertEqual(changes,[])

    def test_reconfiguring_the_same_id_loads_new_catalog_resources(self):
        pet=self.make_pet()
        replacement=self.skins/'default-corrected'/'replacement.png'
        atlas_fixture(replacement,'#27ae60')
        raw=json.loads(self.manifest.read_text(encoding='utf-8'))
        raw['skins'][0]['atlas']='default-corrected/replacement.png'
        self.manifest.write_text(json.dumps(raw),encoding='utf-8')
        pet.configure_skins(self.preference,catalog=SkinCatalog(self.original))
        self.assertEqual(pet.canvas.skin_id,'default-corrected')
        self.assertEqual(pet.canvas.frames[0,0].toImage().pixelColor(60,20).name(),'#27ae60')
        self.assertEqual(load_skin_preference(self.preference),'default-corrected')

    def test_failed_atomic_preference_replace_keeps_old_data_and_removes_staging(self):
        preference=self.root/'profile'/'pet-appearance.json'
        save_skin_preference(preference,'default-corrected');before=preference.read_bytes()
        with patch.object(Path,'replace',side_effect=PermissionError('preference is locked')):
            with self.assertRaises(OSError):save_skin_preference(preference,'purple')
        self.assertEqual(preference.read_bytes(),before)
        self.assertEqual(list(preference.parent.iterdir()),[preference])

    def test_preference_save_failure_reports_status_without_reverting_the_visual(self):
        pet=self.make_pet();before=self.preference.read_bytes()
        with patch.object(Path,'replace',side_effect=PermissionError('preference is locked')):
            self.assertTrue(pet.canvas.set_skin(self.catalog.get('purple'),animate=False))
        self.assertEqual(pet.canvas.skin_id,'purple')
        self.assertEqual(self.preference.read_bytes(),before)
        self.assertIn('未能保存',pet._status)
        self.assertEqual(list(self.preference.parent.iterdir()),[self.preference])

    def test_nine_frame_change_is_atomic_and_resumes_a_queued_busy_state(self):
        pet=self.make_pet();canvas=pet.canvas
        changes=[];canvas.skin_changed.connect(changes.append)
        with patch('shu.pet.time.monotonic',return_value=10):
            self.assertTrue(pet.select_skin('purple'))
        self.assertTrue(canvas.skin_switching);self.assertEqual(canvas.skin_id,'default-corrected')
        self.assertEqual(load_skin_preference(self.preference),'default-corrected')
        self.assertTrue(all(not a.isEnabled() for a in pet.skin_actions.values()))
        self.assertFalse(pet.select_skin(LEGACY_SKIN))
        canvas.play('running')
        for index in range(9):
            with patch('shu.pet.time.monotonic',return_value=10+index*.1+.001):canvas._tick()
            self.assertEqual(canvas._skin_switch['clock'].frame,index)
            self.assertEqual(canvas.skin_id,'default-corrected')
            self.assertFalse(canvas.displayed_frame().isNull())
        with patch('shu.pet.time.monotonic',return_value=10.901):canvas._tick()
        self.assertFalse(canvas.skin_switching);self.assertEqual(canvas.skin_id,'purple')
        self.assertEqual(canvas.animation.state,'running');self.assertEqual(changes,['purple'])
        self.assertEqual(load_skin_preference(self.preference),'purple')
        self.assertTrue(pet.skin_actions['purple'].isChecked())

    def test_paused_animation_stays_paused_after_outfit_change(self):
        pet=self.make_pet();canvas=pet.canvas;canvas.set_animated(False)
        with patch('shu.pet.time.monotonic',return_value=20):pet.select_skin('purple')
        self.assertTrue(canvas.skin_switching)
        with patch('shu.pet.time.monotonic',return_value=21):canvas._tick()
        self.assertEqual(canvas.skin_id,'purple');self.assertFalse(canvas.animated)
        self.assertEqual(canvas.current_cell,(0,0))

    def test_corrupt_target_or_transition_never_changes_the_selected_skin(self):
        pet=self.make_pet();target=self.catalog.get('purple')
        target.transition_in.write_bytes(b'broken')
        self.assertFalse(pet.select_skin('purple'));self.assertFalse(pet.canvas.skin_switching)
        self.assertEqual(pet.canvas.skin_id,'default-corrected')
        self.assertEqual(load_skin_preference(self.preference),'default-corrected')

    def test_missing_optional_skin_falls_back_without_modifying_original(self):
        before=self.original.read_bytes()
        self.catalog.get('default-corrected').atlas.write_bytes(b'not-an-atlas')
        pet=self.make_pet()
        self.assertEqual(pet.canvas.skin_id,LEGACY_SKIN)
        self.assertEqual(len(pet.canvas.frames),73);self.assertEqual(self.original.read_bytes(),before)

    def test_drag_and_click_do_not_interrupt_transition(self):
        pet=self.make_pet()
        with patch('shu.pet.time.monotonic',return_value=30):pet.select_skin('purple')
        QTest.mousePress(pet,Qt.MouseButton.LeftButton,pos=QPoint(60,70))
        QTest.mouseDClick(pet,Qt.MouseButton.LeftButton,pos=QPoint(60,70))
        self.assertIsNone(pet.offset);self.assertFalse(pet.canvas.dragging)
        self.assertTrue(pet.canvas.skin_switching)

    def test_transition_clock_stops_at_nine_frames_after_a_long_stall(self):
        clock=SkinChange(0);clock.advance(10000)
        self.assertTrue(clock.done);self.assertEqual(clock.frame,9)

    def test_pet_shell_icon_updates_after_commit_and_chat_survives(self):
        from shu.companion_ui import CompanionShell
        from tests.test_companion_ui import FakeController
        from PySide6.QtWidgets import QSystemTrayIcon
        controller=FakeController(self.root/'profile')
        window=None
        try:
            with patch.object(QSystemTrayIcon,'isSystemTrayAvailable',return_value=False):
                window=CompanionShell(controller,Path(__file__).resolve().parents[1])
            window.pet.configure_skins(self.root/'profile/skin-test-selection.json',catalog=self.catalog)
            window.pet.canvas.timer.stop();window.pet.chat.input.setPlainText('保留换肤前的草稿')
            self.assertEqual(window.pet.canvas.skin_id,'default-corrected')
            before=window.windowIcon().pixmap(64,64).toImage()
            self.assertFalse(before.isNull())
            with patch('shu.pet.time.monotonic',return_value=50):window.pet.select_skin('purple')
            self.assertEqual(window.pet.canvas.skin_id,'default-corrected')
            with patch('shu.pet.time.monotonic',return_value=51):window.pet.canvas._tick()
            self.assertEqual(window.pet.canvas.skin_id,'purple')
            self.assertEqual(window.pet.chat.input.toPlainText(),'保留换肤前的草稿')
            self.assertFalse(window.windowIcon().isNull())
            self.assertNotEqual(window.windowIcon().pixmap(64,64).toImage(),before)
        finally:
            if window is not None:window.quit_application();window.deleteLater();self.app.processEvents()
            controller.engine.close()


if __name__=='__main__':unittest.main()
