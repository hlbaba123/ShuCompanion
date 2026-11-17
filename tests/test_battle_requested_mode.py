"""Explicit current user difficulty cannot be replaced by an imported mode."""
import unittest

from shu.battle_import_binding import ImportBindingError, load_related_import_binding
from tests import test_battle_cross_turn_binding as fixtures


class RequestedModeBindingTests(unittest.TestCase):
    def check_binding(self, imported_mode, wording, *, scope=None):
        fixture_case = fixtures.CrossTurnImportBindingTests('runTest')
        self.addCleanup(fixture_case.doCleanups)
        fixture, store = fixture_case.setup_import(imported_mode)
        request = '现在开始MAA自动战斗，运行刚才导入的UT-EX-1' + wording + '一次，不使用理智药或源石。'
        runner = fixture_case.runner(fixture, store, request=request)
        if scope is not None:
            # Rebuild a valid independent gameplay task with a declared mode;
            # this scope never grants permission to change the source mode.
            contract = dict(runner.context['contract'], import_scope={'difficulty': scope})
            state = store.submit('Explicit mode review', contract)
            dispatch = store.ensure_dispatch(state['task_id'], 'maa', 'battle', {'contract': state['contract']})
            store.claim_dispatch(dispatch['dispatch_id'], 'mode-review')
            runner.context = {key: state[key] for key in ('task_id', 'revision', 'epoch', 'contract')}
            runner.context['dispatch_id'] = dispatch['dispatch_id']
        return load_related_import_binding(store, runner.context)

    def test_current_explicit_mode_conflicting_with_source_is_rejected(self):
        for source, requested in ((1, '突袭'), (2, '普通')):
            with self.subTest(source=source, requested=requested), self.assertRaises(ImportBindingError):
                self.check_binding(source, requested)

    def test_same_explicit_mode_and_unspecified_mode_preserve_import_evidence(self):
        for source, expected, wording in ((1, 'normal', '普通'), (2, 'raid', '突袭'),
                                          (1, 'normal', ''), (2, 'raid', '')):
            with self.subTest(source=source, wording=wording):
                self.assertEqual(self.check_binding(source, wording).difficulty, expected)

    def test_negated_other_mode_is_not_a_request_for_both(self):
        for source, expected, wording in ((2, 'raid', '，不是普通，是突袭'),
                                          (1, 'normal', '，不是突袭，是普通'),
                                          (2, 'raid', '，不要普通，打突袭'),
                                          (1, 'normal', '，别打突袭，打普通')):
            with self.subTest(source=source, wording=wording):
                self.assertEqual(self.check_binding(source, wording).difficulty, expected)

    def test_corrected_mode_still_rejects_the_wrong_import(self):
        for source, wording in ((1, '，不是普通，是突袭'), (2, '，不是突袭，是普通')):
            with self.subTest(source=source), self.assertRaises(ImportBindingError):
                self.check_binding(source, wording)

    def test_ambiguous_both_modes_and_conflicting_structured_scope_are_not_silently_narrowed(self):
        with self.assertRaises(ImportBindingError): self.check_binding(1, '普通和突袭都运行')
        with self.assertRaises(ImportBindingError): self.check_binding(1, '', scope=2)
        with self.assertRaises(ImportBindingError): self.check_binding(2, '', scope=1)


if __name__ == '__main__':
    unittest.main()
