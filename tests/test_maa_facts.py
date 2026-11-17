"""Roster publication and feasibility against real SQLite and hand-checked boxes."""
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest

from shu.core import TaskEngine
from shu.maa_facts import RosterStore, match_copilot, parse_maa_export


class MaaExportTests(unittest.TestCase):
    def test_partial_export_does_not_assert_unowned_or_invent_skill_fields(self):
        parsed = parse_maa_export({
            'done': False,
            'all_opers': [{'id': 'a', 'name': '甲', 'own': True},
                          {'id': 'b', 'name': '乙', 'own': False}],
            'own_opers': [{'id': 'a', 'name': '甲', 'own': True,
                           'elite': 2, 'level': 70, 'potential': 2}],
        })
        self.assertFalse(parsed['complete'])
        self.assertEqual(parsed['operators'][0]['elite'], 2)
        self.assertIsNone(parsed['operators'][0]['skill_level'])
        self.assertEqual(parsed['operators'][0]['mastery'], {})
        self.assertEqual(parsed['operators'][0]['modules'], {})
        self.assertIsNone(parsed['operators'][1]['owned'])

    def test_complete_export_preserves_explicit_absence(self):
        parsed = parse_maa_export({'details': {
            'done': True, 'own_opers': [],
            'all_opers': [{'id': 'b', 'name': '乙', 'own': False}],
        }})
        self.assertTrue(parsed['complete'])
        self.assertIs(parsed['operators'][0]['owned'], False)

    def test_malformed_completed_export_cannot_publish_a_complete_empty_box(self):
        with self.assertRaises(ValueError):
            parse_maa_export({'done': True, 'all_opers': []})


class RosterPublicationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = TaskEngine(Path(self.folder.name))
        self.store = RosterStore(self.engine)
        self.source = {'kind': 'maa_export', 'source_version': '6.21.4',
                       'observed_at': 1000, 'evidence_ref': 'fixture:scan-1'}

    def tearDown(self):
        self.engine.close()
        self.folder.cleanup()

    def test_restart_retains_version_history_and_isolates_profiles(self):
        first = self.store.publish('account-a', [{'id': 'a', 'name': '甲',
                                  'owned': True, 'elite': 2}], True, self.source)
        second = self.store.publish('account-a', [{'id': 'b', 'name': '乙',
                                   'owned': True}], False,
                                   dict(self.source, evidence_ref='fixture:scan-2'))
        self.assertEqual(second['box_revision'], 2)
        self.engine.close()
        self.engine = TaskEngine(Path(self.folder.name))
        reopened = RosterStore(self.engine)
        self.assertEqual(reopened.current('account-a')['batch_id'], second['batch_id'])
        self.assertEqual(reopened.batch('account-a', first['batch_id'])['box_revision'], 1)
        self.assertIsNone(reopened.current('account-b'))
        self.assertEqual(len(reopened.batch('account-a', first['batch_id'])['operators']), 1)

    def test_partial_update_retains_known_mastery_and_module_with_their_sources(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                           'elite': 2, 'level': 90, 'skill_level': 7,
                           'mastery': {'3': 3}, 'modules': {'1': 3}}],
                           True, dict(self.source, kind='user_provided'))
        box = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                             'elite': 2, 'level': 90, 'skill_level': None,
                             'mastery': {}, 'modules': {}}], False,
                             dict(self.source, evidence_ref='fixture:partial'))
        oper = box['operators'][0]
        self.assertEqual((oper['skill_level'], oper['mastery'], oper['modules']),
                         (7, {'3': 3}, {'1': 3}))
        self.assertEqual(oper['field_sources']['mastery.3']['kind'], 'user_provided')

    def test_complete_screenshot_refresh_preserves_unobserved_practice_for_present_operator(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                           'skill_level': 7, 'mastery': {'3': 3},
                           'modules': {'1': 3}}], True,
                           dict(self.source, kind='user_provided'))
        refreshed = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                                       'level': 90}], True,
                                       dict(self.source, observed_at=1100))
        self.assertEqual(refreshed['operators'][0]['mastery'], {'3': 3})

    def test_out_of_order_scan_does_not_downgrade_fresher_known_fields(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                                 'elite': 2, 'level': 90}], True,
                           dict(self.source, observed_at=1100))
        delayed = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                                         'elite': 1, 'level': 50}], False, self.source)
        self.assertEqual((delayed['operators'][0]['elite'], delayed['operators'][0]['level']),
                         (2, 90))

    def test_delayed_complete_scan_cannot_erase_newer_operator_evidence(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                           True, dict(self.source, observed_at=1100))
        delayed = self.store.publish('p', [], True, self.source)
        self.assertEqual(delayed['operators'][0]['owned'], True)
        self.assertEqual(match_copilot({'opers': [{'name': '甲'}]}, delayed)
                         ['classification'], 'satisfied')

    def test_delayed_positive_cannot_revive_absence_from_newer_complete_scan(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                           True, self.source)
        absence = self.store.publish('p', [], True,
                                     dict(self.source, observed_at=1200))
        delayed = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                                     False, dict(self.source, observed_at=1100))
        result = match_copilot({'opers': [{'name': '甲'}]}, delayed)
        self.assertNotEqual(result['classification'], 'satisfied')
        self.assertEqual(self.store.batch('p', absence['batch_id'])['operators'], [])

    def test_complete_absence_survives_intervening_newer_partial_batch(self):
        self.store.publish('p', [], True, self.source)
        self.store.publish('p', [{'id': 'b', 'name': '乙', 'owned': True}], False,
                           dict(self.source, observed_at=1200))
        delayed = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                                     False, dict(self.source, observed_at=900))
        self.assertNotEqual(match_copilot({'opers': [{'name': '甲'}]}, delayed)
                            ['classification'], 'satisfied')
        self.assertEqual(match_copilot({'opers': [{'name': '乙'}]}, delayed)
                         ['classification'], 'satisfied')

    def test_delayed_complete_absence_overrides_only_older_positive_evidence(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                           False, dict(self.source, observed_at=900))
        self.store.publish('p', [{'id': 'b', 'name': '乙', 'owned': True}], False,
                           dict(self.source, observed_at=1200))
        delayed = self.store.publish('p', [], True, self.source)
        self.assertNotEqual(match_copilot({'opers': [{'name': '甲'}]}, delayed)
                            ['classification'], 'satisfied')
        self.assertEqual(match_copilot({'opers': [{'name': '乙'}]}, delayed)
                         ['classification'], 'satisfied')

    def test_partial_unknown_does_not_unblock_positive_older_than_complete_absence(self):
        self.store.publish('p', [], True, self.source)
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'elite': 2}], False,
                           dict(self.source, observed_at=1200))
        delayed = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                                     False, dict(self.source, observed_at=900))
        self.assertNotEqual(match_copilot({'opers': [{'name': '甲'}]}, delayed)
                            ['classification'], 'satisfied')

    def test_legacy_batches_recover_complete_absence_without_rewriting_history(self):
        self.store.publish('p', [], True, self.source)
        self.store.publish('p', [{'id': 'b', 'name': '乙', 'owned': True}], False,
                           dict(self.source, observed_at=1200))
        # Persist the previous release's valid format, which had no watermark.
        history = {}
        for row in self.engine._db.execute('SELECT batch_id,payload FROM workflow_roster_batches'):
            legacy = json.loads(row['payload'])
            legacy.pop('latest_complete_source', None)
            payload = json.dumps(legacy, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':'))
            history[row['batch_id']] = payload
            self.engine._db.execute('UPDATE workflow_roster_batches SET payload=?,content_hash=? WHERE batch_id=?',
                (payload, hashlib.sha256(payload.encode('utf-8')).hexdigest(), row['batch_id']))
        delayed = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                                     False, dict(self.source, observed_at=900))
        self.assertNotEqual(match_copilot({'opers': [{'name': '甲'}]}, delayed)
                            ['classification'], 'satisfied')
        for batch_id, payload in history.items():
            self.assertEqual(self.engine._db.execute(
                'SELECT payload FROM workflow_roster_batches WHERE batch_id=?',
                (batch_id,)).fetchone()['payload'], payload)

    def test_older_source_cannot_fill_an_advanced_elite_stage_with_prior_stage_level(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                                 'elite': 1, 'level': 80}], True, self.source)
        refreshed = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True,
                                           'elite': 2}], False,
                                       dict(self.source, observed_at=1100))
        result = match_copilot({'opers': [{'name': '甲',
                               'requirements': {'elite': 2, 'level': 60}}]}, refreshed)
        self.assertEqual(result['classification'], 'unknown')
        self.assertEqual(result['missing_requirements'][0]['field'], 'level')

    def test_outbox_failure_rolls_back_new_version_and_current_pointer(self):
        first = self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                                   True, self.source)
        self.engine._db.execute('''CREATE TRIGGER reject_fact_event
            BEFORE INSERT ON workflow_outbox WHEN NEW.type='box.updated'
            BEGIN SELECT RAISE(ABORT, 'fixture event failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.publish('p', [], True, dict(self.source, observed_at=1100))
        self.assertEqual(self.store.current('p')['batch_id'], first['batch_id'])
        self.assertEqual(self.engine._db.execute(
            'SELECT count(*) FROM workflow_roster_batches').fetchone()[0], 1)

    def test_scan_identity_cannot_be_reused_for_changed_payload(self):
        source = dict(self.source, scan_id='scan-one')
        self.store.publish('p', [], False, source)
        with self.assertRaises(ValueError):
            self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}], False, source)

    def test_corrupt_published_payload_is_not_adopted_as_facts(self):
        box = self.store.publish('p', [], True, self.source)
        self.engine._db.execute('UPDATE workflow_roster_batches SET payload=? WHERE batch_id=?',
                                ('{}', box['batch_id']))
        with self.assertRaises(ValueError):
            self.store.current('p')

    def test_full_scan_replaces_absence_even_after_partial_increment(self):
        self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                           True, self.source)
        partial = self.store.publish('p', [], False,
                                     dict(self.source, evidence_ref='fixture:partial'))
        self.assertFalse(partial['complete'])
        self.assertEqual(match_copilot({'opers': [{'name': '乙'}]}, partial)
                         ['classification'], 'unknown')
        complete = self.store.publish('p', [], True,
                                      dict(self.source, evidence_ref='fixture:full'))
        self.assertEqual(match_copilot({'opers': [{'name': '甲'}, {'name': '乙'}]},
                                      complete)['classification'], 'unsatisfied')

    def test_batch_current_and_outbox_roll_back_together(self):
        self.engine._db.execute('''CREATE TRIGGER reject_roster_current
            BEFORE INSERT ON workflow_roster_current
            BEGIN SELECT RAISE(ABORT, 'fixture failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.publish('p', [{'id': 'a', 'name': '甲', 'owned': True}],
                               True, self.source)
        self.assertIsNone(self.store.current('p'))
        self.assertEqual(self.engine._db.execute(
            'SELECT count(*) FROM workflow_roster_batches').fetchone()[0], 0)
        self.assertEqual(len(self.store.workflow.pending_events()), 0)

    def test_same_scan_replay_has_one_revision_and_one_event(self):
        operators = [{'id': 'a', 'name': '甲', 'owned': True}]
        first = self.store.publish('p', operators, True, self.source)
        repeated = self.store.publish('p', operators, True, self.source)
        self.assertEqual(repeated['batch_id'], first['batch_id'])
        self.assertEqual(repeated['box_revision'], 1)
        events = self.store.workflow.pending_events()
        self.assertEqual(len(events), 1)


class FeasibilityTests(unittest.TestCase):
    def box(self, operators=(), *, complete=True):
        return {'profile_id': 'p', 'box_revision': 1, 'complete': complete,
                'operators': list(operators)}

    def support(self, name, **updates):
        return dict({'id': 'support-' + name, 'name': name, 'owned': True,
                     'verified': True, 'profile_id': 'p', 'observed_at': 900,
                     'source': 'fixture:friend-list', 'evidence_ref': 'fixture:screen',
                     'elite': 2, 'level': 90, 'skill_level': 7,
                     'mastery': {'3': 3}, 'modules': {'1': 3}}, **updates)

    def test_unknown_skill_and_module_are_concrete_missing_facts(self):
        box = self.box([{'id': 'a', 'name': '甲', 'owned': True, 'elite': 2,
                         'level': 90}])
        result = match_copilot({'opers': [{'name': '甲', 'skill': 3,
                                'requirements': {'skill_level': 10, 'module': 1,
                                                 'module_level': 3}}]}, box)
        self.assertEqual(result['classification'], 'unknown')
        self.assertEqual({item['field'] for item in result['missing_requirements']},
                         {'mastery.3', 'module.1'})

    def test_verified_support_covers_one_missing_operator(self):
        result = match_copilot({'opers': [{'name': '甲'}, {'name': '乙', 'skill': 3,
                                'requirements': {'skill_level': 10}}]},
                              self.box([{'id': 'a', 'name': '甲', 'owned': True}]),
                              [self.support('乙')], now=1000)
        self.assertEqual(result['classification'], 'needs_verified_support')
        self.assertEqual([x['origin'] for x in result['assignments']], ['owned', 'support'])

    def test_one_global_support_cannot_fill_two_missing_requirements(self):
        result = match_copilot({'opers': [{'name': '甲'}, {'name': '乙'}]}, self.box(),
                              [self.support('甲'), self.support('乙')], now=1000)
        self.assertEqual(result['classification'], 'unsatisfied')

    def test_name_only_stale_and_wrong_account_support_do_not_prove_availability(self):
        for support in [{'name': '甲'}, self.support('甲', observed_at=1),
                        self.support('甲', profile_id='other'),
                        self.support('甲', evidence_ref='')]:
            with self.subTest(support=support):
                result = match_copilot({'opers': [{'name': '甲'}]}, self.box(),
                                      [support], now=1000)
                self.assertEqual(result['classification'], 'unknown')
                self.assertTrue(result['missing_requirements'])

    def test_groups_need_distinct_operators_and_backtrack_to_valid_assignment(self):
        copilot = {'opers': [{'name': '甲'}], 'groups': [
            {'name': '治疗', 'opers': [{'name': '甲'}, {'name': '乙'}]}]}
        box = self.box([{'id': 'a', 'name': '甲', 'owned': True},
                        {'id': 'b', 'name': '乙', 'owned': True}])
        result = match_copilot(copilot, box)
        self.assertEqual(result['classification'], 'satisfied')
        self.assertEqual([row['name'] for row in result['assignments']], ['甲', '乙'])
        result = match_copilot(copilot, self.box([box['operators'][0]]))
        self.assertEqual(result['classification'], 'unknown')
        self.assertEqual(result['missing_requirements'][0]['field'], 'support')

    def test_module_branch_does_not_compare_as_module_level(self):
        box = self.box([{'id': 'a', 'name': '甲', 'owned': True,
                         'modules': {'2': 3}}])
        result = match_copilot({'opers': [{'name': '甲',
                              'requirements': {'module': 1}}]}, box)
        self.assertEqual(result['classification'], 'unknown')
        self.assertEqual(result['missing_requirements'][0]['field'], 'module.1')

    def test_skill_number_selects_mastery_instead_of_being_a_level(self):
        box = self.box([{'id': 'a', 'name': '甲', 'owned': True,
                         'skill_level': 7, 'mastery': {'2': 3, '3': 0}}])
        result = match_copilot({'opers': [{'name': '甲', 'skill': 3,
                              'requirements': {'skill_level': 10}}]}, box)
        self.assertEqual(result['classification'], 'unsatisfied')
        self.assertEqual(result['missing_requirements'][0]['actual'], 7)

    def test_same_name_forms_require_role_or_id_evidence(self):
        box = self.box([{'id': 'a', 'name': '同名', 'role': 'guard', 'owned': True},
                        {'id': 'b', 'name': '同名', 'role': 'caster', 'owned': True}])
        ambiguous = match_copilot({'opers': [{'name': '同名'}]}, box)
        self.assertEqual(ambiguous['classification'], 'unknown')
        explicit = match_copilot({'opers': [{'name': '同名', 'role': 'caster'}]}, box)
        self.assertEqual(explicit['classification'], 'satisfied')
        self.assertEqual(explicit['assignments'][0]['operator_id'], 'b')

    def test_expired_field_evidence_cannot_claim_owned_conditions_satisfied(self):
        box = self.box([{'id': 'a', 'name': '甲', 'owned': True,
                         'field_sources': {'owned': {'expires_at': 950}}}])
        result = match_copilot({'opers': [{'name': '甲'}]}, box, now=1000)
        self.assertEqual(result['classification'], 'unknown')

    def test_known_low_main_skill_cannot_become_unknown_high_mastery(self):
        result = match_copilot({'opers': [{'name': '甲', 'skill': 3,
                              'requirements': {'skill_level': 10}}]},
                              self.box([{'id': 'a', 'name': '甲', 'owned': True,
                                         'skill_level': 6}]))
        self.assertEqual(result['classification'], 'unsatisfied')

    def test_catalogue_summary_is_not_a_complete_feasibility_document(self):
        result = match_copilot({'operators': [{'name': '甲'}],
                               'requires_download': True}, self.box())
        self.assertEqual(result['classification'], 'unknown')
        self.assertEqual(result['missing_requirements'][0]['field'], 'full_document')

    def test_verified_support_name_does_not_resolve_ambiguous_forms(self):
        box = self.box([{'id': 'a', 'name': '同名', 'role': 'guard', 'owned': True},
                        {'id': 'b', 'name': '同名', 'role': 'caster', 'owned': True}])
        result = match_copilot({'opers': [{'name': '同名'}]}, box,
                              [self.support('同名')], now=1000)
        self.assertEqual(result['classification'], 'unknown')


if __name__ == '__main__':
    unittest.main()
