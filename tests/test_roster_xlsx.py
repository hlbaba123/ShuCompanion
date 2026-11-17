"""Offline user-table provenance and ownership semantics; no Excel automation."""
import io
import json
from contextlib import closing
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from shu.core import TaskEngine
from shu.maa_roster_tools import RosterTools
from shu.maa_facts import _json
from shu.roster_xlsx import parse_roster_xlsx, import_user_roster

HEADERS = ['干员名称', '星级', '职业', '干员 ID', '分支', '是否已招募', '等级',
           '精英化等级', '潜能等级', '通用技能等级', '1技能名称', '1技能专精等级',
           '2技能名称', '2技能专精等级', '3技能名称', '3技能专精等级',
           'χ模组名称', 'χ模组等级', 'γ模组名称', 'γ模组等级', 'Δ模组名称',
           'Δ模组等级', 'α模组名称', 'α模组等级', 'β模组名称', 'β模组等级']
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def workbook(rows, *, formula=False, reverse=False):
    out = io.BytesIO()
    root = ET.Element('worksheet', xmlns=NS)
    data = ET.SubElement(root, 'sheetData')
    order = list(range(len(HEADERS)))
    if reverse:
        order.reverse()
    for number, values in enumerate([HEADERS] + rows, 1):
        row = ET.SubElement(data, 'row', r=str(number))
        for index, field in enumerate(order):
            if field >= len(values) or values[field] is None:
                continue
            cell = ET.SubElement(row, 'c', r=chr(65 + index) + str(number), t='inlineStr')
            ET.SubElement(ET.SubElement(cell, 'is'), 't').text = str(values[field])
            if formula and number == 2 and index == 0:
                ET.SubElement(cell, 'f').text = 'HYPERLINK("https://example.invalid")'
    with ZipFile(out, 'w') as zipped:
        zipped.writestr('xl/workbook.xml', '<workbook xmlns="' + NS + '" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="干员表" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zipped.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        zipped.writestr('xl/worksheets/sheet1.xml', ET.tostring(root, encoding='utf-8'))
    return out.getvalue()


def owned():
    return ['甲', 6, '近卫', 'char_a', '领主', '是', 60, 2, 1, 7,
            '技能一', 0, '技能二', 3, None, 0, 'χ模组', 2, None, 0,
            'Δ模组', 1, 'α模组', 3, None, 0]


class RosterXlsxTests(unittest.TestCase):
    def test_actual_attributes_and_module_symbol_mapping_survive_column_reorder(self):
        result = parse_roster_xlsx(workbook([owned()], reverse=True))
        row = result['operators'][0]
        self.assertEqual((row['elite'], row['level'], row['rarity']), (2, 60, 6))
        self.assertEqual(row['mastery'], {'1': 0, '2': 3})
        self.assertEqual(row['modules'], {'1': 2, '3': 3, '4': 1})
        self.assertFalse(result['complete'])
        self.assertEqual(result['source_rows'][0]['cells']['干员名称'], 'Z2')

    def test_absent_operator_zero_export_values_are_not_owned_practice(self):
        row = ['乙', 5, '先锋', 'char_b', '', '否', 0, 0, 0, 0]
        result = parse_roster_xlsx(workbook([row]))['operators'][0]
        self.assertIs(result['owned'], False)
        self.assertIsNone(result['elite'])
        self.assertIsNone(result['level'])
        self.assertIsNone(result['skill_level'])

    def test_unknown_cells_do_not_become_zero_or_owned(self):
        row = owned()
        row[5:10] = ['', '', '', '', '']
        result = parse_roster_xlsx(workbook([row]))['operators'][0]
        self.assertIsNone(result['owned'])
        self.assertIsNone(result['elite'])
        self.assertEqual(result['mastery'], {})

    def test_invalid_cells_or_duplicate_identity_rejected(self):
        for value in ('也许', 'TRUE'):
            row = owned()
            row[5] = value
            with self.assertRaises(ValueError):
                parse_roster_xlsx(workbook([row]))
        with self.assertRaises(ValueError):
            parse_roster_xlsx(workbook([owned(), owned()]))
        with self.assertRaises(ValueError):
            parse_roster_xlsx(workbook([owned()], formula=True))

    def test_bounded_archive_and_xml_are_not_executed(self):
        with self.assertRaises(ValueError):
            parse_roster_xlsx(b'not-xlsx')
        with self.assertRaises(ValueError):
            parse_roster_xlsx(b'0' * (4 * 1024 * 1024 + 1))

    def test_import_archive_is_immutable_and_queryable_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / 'user.xlsx'
            raw = workbook([owned(), ['乙', 5, '先锋', 'char_b', '', '否', 0, 0, 0, 0]])
            source.write_bytes(raw)
            with closing(TaskEngine(directory / 'data')) as engine:
                tools = RosterTools(engine, directory / 'data')
                receipt = import_user_roster(source, tools)
                self.assertEqual(source.read_bytes(), raw)
                self.assertEqual(Path(receipt['archived_source']).read_bytes(), raw)
                self.assertEqual(receipt['ownership'], {'owned': 1, 'unowned': 1})
                repeat = import_user_roster(source, tools)
                self.assertEqual(repeat, receipt)
                os.utime(source, (source.stat().st_atime, source.stat().st_mtime + 5))
                self.assertEqual(import_user_roster(source, tools), receipt)
            with closing(TaskEngine(directory / 'data')) as engine:
                box = RosterTools(engine, directory / 'data').current('local-default')
                self.assertFalse(box['complete'])
                self.assertIs(box['operators'][1]['owned'], False)
                self.assertFalse(any(row['id'] == 'char_new' for row in box['operators']))
                snapshot = json.loads(Path(receipt['normalized']).read_text(encoding='utf-8'))
                self.assertEqual(snapshot['source_rows'][0]['row'], 2)

    def test_corrupt_archived_original_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / 'user.xlsx'
            source.write_bytes(workbook([owned()]))
            with closing(TaskEngine(directory / 'data')) as engine:
                tools = RosterTools(engine, directory / 'data')
                receipt = import_user_roster(source, tools)
                Path(receipt['archived_source']).write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    import_user_roster(source, tools)
                self.assertEqual(tools.current('local-default')['box_revision'], 1)

    def test_old_export_replay_remains_idempotent_after_rarity_is_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            with closing(TaskEngine(Path(temporary))) as engine:
                tools = RosterTools(engine, temporary)
                source = {'kind': 'maa-local-export', 'observed_at': 1000,
                          'evidence_ref': 'fixture:original', 'scan_id': 'scan-old'}
                row = {'name': '甲', 'owned': True, 'elite': 2, 'rarity': 6}
                first = tools.facts.publish('p', [row], False, source)
                # Represent an immutable batch written before rarity support.
                parsed = {key: value for key, value in first['operators'][0].items()
                          if key not in ('field_sources', 'rarity')}
                old_hash = hashlib.sha256(_json(['p', False, first['source'], [parsed]]).encode()).hexdigest()
                legacy = {key: value for key, value in first.items() if key != 'content_hash'}
                legacy['operators'][0].pop('rarity')
                legacy['operators'][0]['field_sources'].pop('rarity')
                payload = _json(legacy)
                engine._db.execute('UPDATE workflow_roster_batches SET input_hash=?,payload=?,content_hash=? WHERE batch_id=?',
                    (old_hash, payload, hashlib.sha256(payload.encode()).hexdigest(), first['batch_id']))
                repeated = tools.facts.publish('p', [row], False, source)
                self.assertEqual(repeated['batch_id'], first['batch_id'])
                self.assertEqual(repeated['box_revision'], 1)
                self.assertNotIn('rarity', repeated['operators'][0])


if __name__ == '__main__':
    unittest.main()
