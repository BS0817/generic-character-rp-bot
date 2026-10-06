import sys,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from form_help import parse_json
from settings_store import DraftStore

class FormHelpTests(unittest.TestCase):
    def test_json_error_has_field_position_reason_and_example(self):
        for text in ['{"벤치": 3,}',"{'벤치': 3}",'("벤치": 3)','{"전체": ("차")}','{"전체": ["차"] "아침": []}']:
            with self.assertRaises(ValueError) as raised:parse_json(text,'좌석별 정원','seats')
            message=str(raised.exception)
            self.assertIn('좌석별 정원',message);self.assertIn('번째 줄',message)
            self.assertIn('큰따옴표',message);self.assertIn('소괄호',message)
            self.assertIn('"테이블 1": 4',message)
        self.assertEqual(parse_json('{"벤치":3}'),{'벤치':3})

    def test_failed_commit_rolls_back_files_and_keeps_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'config').mkdir()
            first=root/'config/characters.json';second=root/'config/settings.json'
            first.write_text('{}');second.write_text('{}')
            store=DraftStore(root);store.write_json('characters',{'a':{'name':'A'}})
            store.write_json('settings',{'world_name':'changed'})
            import os
            replace=os.replace;calls=[]
            def fail_once(source,target):
                calls.append(target)
                if len(calls)==2:raise OSError('disk failure')
                return replace(source,target)
            with patch('settings_store.os.replace',side_effect=fail_once),self.assertRaises(OSError):store.flush()
            self.assertEqual(first.read_text(),'{}');self.assertEqual(second.read_text(),'{}')
            self.assertEqual(len(store.pending),2)
            self.assertFalse(any(root.rglob('*.tmp')))
            store.flush()
            self.assertEqual(json.loads(first.read_text())['a']['name'],'A')
            self.assertFalse(store.pending)

    def test_calendar_json_examples_are_complete_and_valid(self):
        from form_help import JSON_EXAMPLES
        from world_calendar import validate_calendar
        profiles=parse_json(JSON_EXAMPLES['season_profiles'])
        schedules=parse_json(JSON_EXAMPLES['weekly_schedule'])
        validate_calendar({'calendar':{'season_profiles':profiles}}, {'a':{'weekly_schedule':schedules}}, {'도서관':{}})
