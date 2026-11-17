"""Positional parser corrections require exact source and ordered load proof."""
import copy
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from shu import maa_load_receipt as single
from shu import maa_queue_receipt as queue
from tests.test_maa_load_receipt import SOURCE
from tests import test_maa_queue_receipt as fixtures


class ActionLoadNormalizationTests(unittest.TestCase):
    def source(self):
        source = single._canonical(copy.deepcopy(SOURCE))
        # Duplicate identical warnings are deliberate: each action is required.
        source['actions'] = [
            {'type': kind, 'name': '斑点', 'role': 'Warrior', 'location': [6, 2],
             'kills': index, 'custom_field': {'must_remain': True}}
            for index, kind in enumerate(('Skill', 'Skill', 'Retreat', 'BulletTime', 'SkillUsage'))]
        source['actions'].append({'type':'Deploy', 'name':'斑点', 'location':[8,2], 'role':'Warrior'})
        return source

    def log(self, source, *, warnings=None, warning_level='WRN', warning_thread='2'):
        moment = datetime(2026,11,11,19,0).timestamp()
        stamp = datetime.fromtimestamp(moment).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        def line(logger,message,level='INF',thread='2'):
            return f'[{stamp}][{level}][{logger}] <{thread}> {message}\n'
        predicted = single._load_metadata(source)[1]
        lines = line('HttpResponseLoggingExtension','HTTP: OK GET https://prts.maa.plus/copilot/get/123 null null 5ms')
        for warning in predicted if warnings is None else warnings:
            level = 'INF' if warning.endswith('已自动补全') else warning_level
            lines += line('CopilotViewModel',warning,level,warning_thread)
        lines += line('CopilotViewModel',source['doc']['title'])
        lines += line('CopilotViewModel',source['doc']['details'])
        lines += line('CopilotViewModel','-'*20)
        lines += line('CopilotViewModel',f"共 {len(source['opers'])+len(source['groups'])} 名干员")
        lines += line('CopilotViewModel','Copilot item collection changed: "Add"')
        baseline={'code':'prts://123','started_at':moment,'log':{'ended_with_newline':True}}
        return lines.encode('utf8'),baseline,moment

    def test_only_four_positional_action_types_lose_redundant_name_and_role(self):
        source=self.source()
        before=copy.deepcopy(source)
        result,messages,normalization,corrected=single._load_metadata(source)
        self.assertEqual(source,before)
        self.assertEqual(normalization,'maa_parser_corrections_v2')
        self.assertTrue(corrected)
        self.assertEqual(len(messages),5)
        self.assertEqual(messages[0],messages[1])
        for original,observed in zip(before['actions'][:5],result['actions'][:5]):
            expected={key:value for key,value in original.items() if key not in {'name','role'}}
            self.assertEqual(observed,expected)
        self.assertEqual(result['actions'][5],before['actions'][5])
        self.assertEqual(queue.expected_queue_copilot_id(source,'prts://123'),0)

    def test_exact_repeated_warning_sequence_verifies_and_requires_warning_thread(self):
        source=self.source()
        raw,baseline,moment=self.log(source)
        result=queue._queue_log_sequence(raw,baseline,moment,source)
        self.assertTrue(result['verified'],result)
        self.assertEqual(result['cache_normalization'],'maa_parser_corrections_v2')
        warnings=single._load_metadata(source)[1]
        for options in ({'warnings':[]}, {'warnings':warnings[1:]}, {'warnings':warnings[:-1]},
                        {'warnings':list(reversed(warnings))}):
            with self.subTest(options=options):
                raw,baseline,moment=self.log(source,**options)
                self.assertFalse(queue._queue_log_sequence(raw,baseline,moment,source)['verified'])
        for options in ({'warning_thread':'9'},{'warning_level':'INF'}):
            with self.subTest(options=options),self.assertRaises(single.MaaLoadReceiptError):
                raw,baseline,moment=self.log(source,**options)
                queue._queue_log_sequence(raw,baseline,moment,source)

    def test_corrupted_location_extra_deletion_and_unsupported_action_stay_different(self):
        source=self.source()
        expected=queue.expected_loaded_copilot_document(source)
        variants=[]
        mutated=copy.deepcopy(expected);mutated['actions'][0]['location']=[9,9];variants.append(mutated)
        mutated=copy.deepcopy(expected);del mutated['actions'][0]['custom_field'];variants.append(mutated)
        mutated=copy.deepcopy(expected);mutated['actions'].pop();variants.append(mutated)
        mutated=copy.deepcopy(expected);del mutated['actions'][5]['name'];variants.append(mutated)
        for variant in variants:
            self.assertNotEqual(queue.canonical_copilot_document(variant),expected)
        deploy=copy.deepcopy(source);deploy['actions']=deploy['actions'][5:]
        self.assertEqual(single._load_metadata(deploy)[0],deploy)
        self.assertEqual(queue.expected_queue_copilot_id(deploy,'prts://123'),123)

    def test_unsupported_coordinate_types_cannot_trigger_correction(self):
        for location in ([True,2],[6,'2'],[6],[6,2,3],{'x':6,'y':2}):
            source=self.source();source['actions'][0]['location']=location
            with self.subTest(location=location),self.assertRaises(single.MaaLoadReceiptError):
                single._load_metadata(source)

    def test_complete_fresh_queue_receipt_reverifies_action_correction_and_rejects_cache_tamper(self):
        source=copy.deepcopy(fixtures.SOURCE)
        source['actions'].append({'type':'Skill','name':'合成干员','role':'Warrior','location':[6,2]})
        source_patch=patch.object(fixtures,'SOURCE',source)
        source_patch.start();self.addCleanup(source_patch.stop)
        files=fixtures.QueueReceiptTests();files.setUp();self.addCleanup(files.doCleanups)
        baseline=files.baseline([])
        files.now+=1
        raw,_,_=self.log(single._canonical(source))
        # Use the receipt's own observation window and actual fixture clock.
        old=datetime(2026,11,11,19,0).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        new=datetime.fromtimestamp(files.now).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        raw=raw.replace(old.encode(),new.encode())
        with files.log.open('ab') as stream:stream.write(raw)
        row=files.row(0)
        body=queue.expected_loaded_copilot_document(source)
        Path(row['FilePath']).write_text(json.dumps(body,ensure_ascii=False),encoding='utf8')
        files.config.write_bytes(fixtures.raw_config([row]))
        result=files.collect(baseline)
        self.assertTrue(result['verified'],result)
        payload=files.payload(result)
        self.assertTrue(queue.verify_queue_receipt(payload,files.expected(baseline))['verified'])
        self.assertEqual(payload['log_evidence']['outcome']['cache_normalization'],'maa_parser_corrections_v2')
        body['actions'][-1]['location']=[9,9]
        Path(row['FilePath']).write_text(json.dumps(body,ensure_ascii=False),encoding='utf8')
        self.assertFalse(files.collect(baseline)['verified'])


if __name__=='__main__':
    unittest.main()
