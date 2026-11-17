from types import SimpleNamespace
import unittest
from shu.controller import _record_persona_metrics

class MetricClient:
    def __init__(self, history):
        self.metrics_history = history

class PersonaMetricsDrainTests(unittest.TestCase):
    def test_all_network_continuations_are_recorded_once_including_failure(self):
        rows=[]
        controller=SimpleNamespace(runtime=SimpleNamespace(record_call=lambda *args:rows.append(args)))
        client=MetricClient([{'request_number':1,'status':'completed'},
            {'request_number':2,'status':'failed'},{'request_number':3,'status':'completed'}])
        _record_persona_metrics(controller,client,'req')
        _record_persona_metrics(controller,client,'req')
        self.assertEqual(len(rows),3)
        self.assertEqual([r[1]['request_number'] for r in rows],[1,2,3])
        self.assertTrue(all(r[1]['request_id']=='req' for r in rows))

    def test_replaced_client_has_independent_cursor_and_old_client_is_released(self):
        rows = []
        controller = SimpleNamespace(runtime=SimpleNamespace(record_call=lambda *args: rows.append(args)))
        client = MetricClient([{'request_number': 34}])
        _record_persona_metrics(controller, client, 'old')
        del client
        self.assertEqual(len(controller._metrics_seen), 0)
        replacement = MetricClient([{'request_number': 1}])
        _record_persona_metrics(controller, replacement, 'new')
        self.assertEqual([r[1]['request_id'] for r in rows], ['old', 'new'])
