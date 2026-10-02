import copy
import io
import json
import hashlib
import tempfile
import unittest
import zipfile
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from settings_store import Store
from updater import BotLock, bot_running, stage_release, FILES, version_tuple
from update_helper import replace_binaries
from runtime_options import validate_options


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        self.data = {
            'characters': {'a': {'name':'A','prompt_file':'prompts/a.txt','token_env':'TOKEN_A','private_room':'Room','place_weights':{'Room':2},'restricted_places':['Room'],'extra':{'preserve':True}},'b':{'name':'B'}},
            'places': {'Room':{'allowed_characters':['a','b'],'parent':None},'Hall':{'parent':'Room'}},
            'relations': {'a|b':{'context':'friends'},'_description':{'keep':True}},
            'settings': {'custom':42,'features':{'dm':False}},
        }
        for name,data in self.data.items():
            (self.root/'config'/f'{name}.json').write_text(json.dumps(data),encoding='utf-8')
        (self.root/'.env').write_text('# keep comment\nUNKNOWN="x # y"\nTOKEN_A=old\n',encoding='utf-8')
        (self.root/'prompts').mkdir(); (self.root/'prompts/a.txt').write_text('old prompt',encoding='utf-8')
        (self.root/'history.db').write_bytes(b'history')
        self.store = Store(self.root)
    def tearDown(self): self.temp.cleanup()
    def test_env_comments_unknowns_and_quotes(self):
        self.store.write_env({'TOKEN_A':"abc\\def'ghi # $value"})
        self.assertEqual(self.store.env['TOKEN_A'],"abc\\def'ghi # $value")
        self.assertIn('# keep comment',(self.root/'.env').read_text())
        self.assertEqual(self.store.env['UNKNOWN'],'x # y')
    def test_backup_restore_preserves_history(self):
        backup=self.store.backup()
        self.store.save_prompt('prompts/a.txt','new')
        self.store.write_env({'TOKEN_A':'new'})
        self.store.restore(backup)
        self.assertEqual((self.root/'prompts/a.txt').read_text(),'old prompt')
        self.assertEqual(self.store.env['TOKEN_A'],'old')
        self.assertEqual((self.root/'history.db').read_bytes(),b'history')
    def test_unknown_fields_preserved(self):
        data=copy.deepcopy(self.store.data['characters']); data['a']['name']='Updated'
        self.store.write_json('characters',data); self.store.reload()
        self.assertTrue(self.store.data['characters']['a']['extra']['preserve'])
    def test_delete_character_cleans_references(self):
        self.store.remove_character('a')
        self.assertNotIn('a|b',self.store.data['relations'])
        self.assertEqual(self.store.data['places']['Room']['allowed_characters'],['b'])
        self.assertTrue((self.root/'prompts/a.txt').exists())
        self.assertEqual(self.store.env['TOKEN_A'],'old')
    def test_delete_place_cleans_references(self):
        self.store.remove_place('Room')
        self.assertEqual(self.store.data['characters']['a']['place_weights'],{})
        self.assertIsNone(self.store.data['characters']['a']['private_room'])
        self.assertIsNone(self.store.data['places']['Hall']['parent'])
    def test_bad_json_refuses_without_overwrite(self):
        path=self.root/'config/settings.json'; path.write_text('{bad')
        with self.assertRaises(json.JSONDecodeError): Store(self.root)
        self.assertEqual(path.read_text(),'{bad')
    def test_paths_rejected(self):
        with self.assertRaises(ValueError): self.store.save_prompt('../oops.txt','text')
        with self.assertRaises(ValueError): self.store.save_prompt('config/settings.json','text')
    def test_runtime_validation(self):
        with self.assertRaises(ValueError): validate_options({'RP_AUTONOMOUS_USER_MENTION_CHANCE':2})
        with self.assertRaises(ValueError): validate_options({'AUTONOMOUS_CHARACTER_COOLDOWN_MIN':200,'AUTONOMOUS_CHARACTER_COOLDOWN_MAX':100})
        self.assertEqual(validate_options({})['MAX_BOT_CHAIN'],8)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        for name in FILES: (self.root/name).write_bytes(b'MZold')
        (self.root/'config').mkdir(); (self.root/'config/settings.json').write_text('personal')
    def tearDown(self): self.temp.cleanup()
    def stage(self):
        stage=self.root/'rpbot-update-test'; stage.mkdir()
        for name in FILES: (stage/name).write_bytes(b'MZnew')
        return stage
    def test_lifetime_lock(self):
        lock=BotLock(self.root); self.assertTrue(lock.acquire()); self.assertTrue(bot_running(self.root)); lock.release(); self.assertFalse(bot_running(self.root))
    def test_only_binaries_replaced(self):
        replace_binaries(self.root,self.stage())
        for name in FILES: self.assertEqual((self.root/name).read_bytes(),b'MZnew')
        self.assertEqual((self.root/'config/settings.json').read_text(),'personal')
    def test_running_bot_prevents_update(self):
        lock=BotLock(self.root); lock.acquire()
        try:
            with self.assertRaises(RuntimeError): replace_binaries(self.root,self.stage())
            self.assertEqual((self.root/FILES[0]).read_bytes(),b'MZold')
        finally: lock.release()
    def test_replace_failure_rolls_back(self):
        stage=self.stage()
        import os
        real=os.replace
        def replace(source,target):
            if Path(source)==stage/FILES[1]: raise PermissionError('busy')
            return real(source,target)
        with patch('update_helper.os.replace',side_effect=replace):
            with self.assertRaises(PermissionError): replace_binaries(self.root,stage)
        for name in FILES: self.assertEqual((self.root/name).read_bytes(),b'MZold')
    def test_download_validation_and_allowlist(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as package:
            for name in FILES: package.writestr(name,b'MZnew')
            package.writestr('config/settings.json','overwrite')
            package.writestr('../evil','traversal')
        payload=buffer.getvalue(); digest=hashlib.sha256(payload).hexdigest().encode()
        with patch('updater.request',side_effect=[io.BytesIO(digest),io.BytesIO(payload)]):
            stage=stage_release({'checksum_url':'https://checksum','url':'https://archive','version':'v0.1.4'},self.root)
        self.assertFalse((stage/'config').exists()); self.assertFalse((self.root/'evil').exists())
    def test_corrupt_download_is_removed(self):
        with patch('updater.request',side_effect=[io.BytesIO(b'0'*64),io.BytesIO(b'bad')]):
            with self.assertRaises(ValueError): stage_release({'checksum_url':'x','url':'y','version':'v0.1.4'},self.root)
        self.assertEqual(list(self.root.glob('rpbot-update-*')),[])
    def test_semantic_version(self):
        self.assertGreater(version_tuple('v0.1.10'),version_tuple('0.1.9'))
        with self.assertRaises(ValueError): version_tuple('v0.1.4-beta')


if __name__=='__main__': unittest.main()
