"""Exercise the import workflow with synthetic public data and visual observations."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from shu import maa_agents
from shu.controller import SessionController
from shu.conversation_acceptance import import_verification
from shu.copilot_catalog import parse_candidate
from shu.copilot_pool import CopilotPool
from shu.maa_facts import RosterStore
from shu.maa_visual_native import MAA_EXECUTABLE
from shu.research_tools import ResearchTools
from tests.test_leader_controller import Model, route
from tests.test_maa_agents import FakeClient, FakeSupervisor


APP = QCoreApplication.instance() or QCoreApplication([])
STAGE = {'stage_key': 'fixture-ordinary', 'display_code': 'A-1', 'stage_id': 'fixture_stage',
         'query_key': 'fixture_stage', 'query_key_verified': True, 'difficulty_verified': True,
         'difficulty': 1, 'group_id': 'g'}
JOB = {'stage_name': 'fixture_stage', 'difficulty': 1, 'opers': [{'name': '甲'}],
       'actions': [{'type': 'Deploy'}], 'doc': {'title': 'A-1 普通的离线作业'}}


class OfflineStation(ResearchTools):
    def __init__(self, config, cache_dir, **kwargs):
        super().__init__(config, cache_dir, transport=self.download_fixture)

    def download_fixture(self, request, **_kwargs):
        if request.full_url != 'https://prts.maa.plus/copilot/get/42':
            raise AssertionError(f'Unexpected public request: {request.full_url}')
        return json.dumps({'status_code': 200, 'data': {'id': 42, 'content': JOB}}).encode()

    def candidates_for_stage(self, stage, **_kwargs):
        if stage['stage_key'] != STAGE['stage_key']:
            raise AssertionError('Research requested an unverified stage')
        candidate = parse_candidate({'id': 42, 'available': True, 'content': json.dumps(JOB)})
        candidate.update(stage_match_verified=True, difficulty_match='compatible')
        candidate['candidate_locator']['resolved_stage'] = stage
        pool = CopilotPool(self.cache_dir / 'offline-candidate-pool')
        coverage = {'collected': 1, 'total': 1, 'complete': True, 'exhausted': True,
                    'next_page': None, 'pages_received': 1, 'raw_fetched': 1,
                    'unverified': 0, 'total_changed': False}
        ref = pool._save({'schema': 1, 'pool_id': pool.identity(stage['stage_id'], None),
            'stage_id': stage['stage_id'], 'document': None, 'candidates': [candidate],
            'unverified_candidates': [], 'sources': ['https://prts.maa.plus/copilot/query'],
            'coverage': coverage})
        return {'pool_ref': ref, 'coverage': coverage, 'pages_fetched': 0}


class SimulatedMaa(FakeSupervisor):
    backend = 'visual'

    def __init__(self, directory):
        super().__init__({'hwnd': 10, 'pid': 20, 'creation_time': 30, 'executable': MAA_EXECUTABLE})
        self.directory = directory
        self.value = 'prts://19'
        self.frames = {}

    def request(self, command, **kwargs):
        if command['op'] == 'snapshot':
            identifier = str(len(self.commands) + 1)
            path = self.directory / ('frame-' + identifier + '.png')
            path.write_text(self.value, encoding='utf-8')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.frames[str(path)] = self.value
            self.snapshots.append({'screenshot_id': identifier, 'image_path': str(path), 'image_sha256': digest,
                                   'window': {**self.target_identity, 'client_rect': {'width': 1000, 'height': 750},
                                              'foreground': True}, 'permission': {'input_allowed': True}})
            return super().request(command, **kwargs)
        action = command['args']
        if action['action'] not in {'focus', 'click', 'clear_code', 'replace_code'}:
            raise AssertionError(f'Unexpected MAA action: {action}')
        response = super().request(command, **kwargs)
        if response['submission_entered']:
            if action['action'] == 'clear_code':
                self.value = ''
            if action['action'] == 'replace_code':
                if action['code'] != 'prts://42' or self.value:
                    raise AssertionError('MAA did not receive the resolved code into a cleared field')
                self.value = action['code']
        return response

    def observe(self, image_path, **_kwargs):
        value = self.frames[image_path]
        loaded = value == 'prts://42'
        return {'ok': True, 'image_sha256': hashlib.sha256(Path(image_path).read_bytes()).hexdigest(),
                'width': 1000, 'height': 750, 'page': 'copilot', 'idle': True, 'single_job': True,
                'field_empty': not value, 'codes': [value] if value else [],
                'expected_title_visible': loaded, 'expected_stage_visible': loaded,
                'preview_signature': value, 'missing_operators': [] if loaded else ['甲'],
                'targets': {'code_input': {'point': {'x': 110, 'y': 120},
                                           'rect': {'x': 100, 'y': 100, 'width': 400, 'height': 40}}},
                'forbidden_regions': [{'x': 100, 'y': 200, 'width': 50, 'height': 25}]}


class ImportChainTests(unittest.TestCase):
    def test_one_request_routes_to_catalog_box_and_current_maa_readback(self):
        with TemporaryDirectory() as temporary, patch.dict('os.environ', {'DEEPSEEK_API_KEY': ''}):
            root = Path(temporary)
            key = root / 'key.txt'
            key.write_text('sk-offline-import-fixture', encoding='utf-8')
            controller = SessionController(root, root / 'data', key_path=key)
            try:
                self.run_import(controller)
            finally:
                controller.close()

    def run_import(self, controller):
        request = '查找示例活动 A-1 普通难度的作业，核对我的 Box 后导入 MAA，只导入，不开始作战。'
        controller.client = controller.intent_client = Model(route(request, request, relation='new', current_deliverable='maa_import',
                                        activity_name='示例活动', target_stage_codes=['A-1'],
                                        task_spec={'kind': 'import', 'stage_code': 'A-1'}))
        controller.screen_capture_allowed = controller.game_input_allowed = True
        store = controller.execution.store
        RosterStore(store).publish('local-default', [{'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2}],
            True, {'kind': 'user-roster-spreadsheet', 'observed_at': 1000, 'evidence_ref': 'fixture:box'},
            explicit_ownership=True)
        simulated = SimulatedMaa(controller.data_dir)
        agents = []

        def factory(*args, **kwargs):
            roles = maa_agents.MaaRoleAgents(*args, **kwargs)
            agents.append(roles)
            roles.supervisor = simulated

            def catalog(context, _cancel):
                stages = roles._publish('offline-public-catalog', {'catalog_complete': True,
                    'event_name': '示例活动', 'source': 'fixture:public', 'raw_sha256': '1' * 64})
                observation = roles._publish('offline-catalog-observation', {'task_id': context['task_id'],
                    'epoch': context['epoch'], 'tool': 'research_activity_stages',
                    'result': {'ok': True, 'stage_catalog_ref': stages}})
                structure = roles._publish('offline-structure', {'ok': True, 'catalog_complete': True,
                    'activity_name': '示例活动', 'activity_id': 'fixture', 'catalog_ref': stages,
                    'stages': [copy.deepcopy(STAGE)], 'excluded_stage_keys': [],
                    'groups': [{'group_id': 'g', 'label': '示例分区', 'stage_keys': [STAGE['stage_key']]}]})
                return {'status': 'completed', 'structure_ref': structure, 'evidence_refs': [observation]}

            roles.handlers['catalog'] = catalog
            return roles

        controller.execution.role_factory = factory
        errors, results = [], []
        controller.error.connect(errors.append)
        controller.execution.finished.connect(results.append)
        with patch('shu.maa_agents.DeepSeekClient', FakeClient), \
             patch.object(maa_agents.MaaRoleAgents, '_import_vision_readback', return_value=simulated), \
             patch.object(maa_agents.MaaRoleAgents, '_import_load_monitor', return_value=None), \
             patch('shu.planned_agents.ResearchTools', OfflineStation), \
             patch('shu.import_preparation.ResearchTools', OfflineStation), \
             patch.object(controller.windows, 'capture', side_effect=AssertionError('Game capture is out of scope')), \
             patch.object(controller.windows, 'set_input_enabled', side_effect=AssertionError('Game input is out of scope')):
            controller.submit(request)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                APP.processEvents()
                if not controller.is_busy and not controller.execution.active:
                    break
                time.sleep(.01)
            APP.processEvents()
        self.assertEqual(errors, [])
        self.assertFalse(controller.execution.active)
        self.assertEqual(controller.last_intent_contract['current_deliverable'], 'maa_import')
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]['status'], 'import_verified', results)
        task = controller.current_task()
        self.assertEqual(task['completed_runs'], 0)
        verification = import_verification(store, task['id'])
        self.assertTrue(verification['verified'], verification)
        self.assertFalse(verification['battle_start_submitted'])
        self.assertEqual([item['control_action'] for item in verification['submitted_operations']],
                         ['focus', 'click', 'clear_code', 'replace_code'])
        self.assertEqual(len(controller.client.requests), 1)
        self.assertTrue(all(not client.requests for agent in agents for client in agent.clients.values()))
