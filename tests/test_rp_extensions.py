"""Exercise bot helpers without starting Discord or making paid API calls."""
import ast
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desktop_monitor import Monitor, read_snapshot

SOURCE = ast.parse((Path(__file__).resolve().parents[1] / 'src/bot.py').read_text(encoding='utf-8'))


def functions(names, namespace):
    nodes = []
    for node in ast.walk(SOURCE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node = __import__('copy').deepcopy(node)
            node.decorator_list = []
            node.returns = None
            for arg in node.args.args: arg.annotation = None
            nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<bot helpers>', 'exec'), namespace)
    return namespace


class ExtensionTests(unittest.TestCase):
    def test_outfit_priority_and_missing_legacy_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'a.txt').write_text('性格', encoding='utf-8')
            config = dict(prompt_file='a.txt', default_outfit='평상복', current_outfit='예복')
            ns = functions({'load_character_prompt'}, dict(Path=Path, APP_ROOT=root, CHARACTERS={'a':config}))
            self.assertIn('현재 참고할 복장: 예복', ns['load_character_prompt']('a'))
            config['current_outfit'] = ''
            self.assertIn('현재 참고할 복장: 평상복', ns['load_character_prompt']('a'))
            config.pop('default_outfit')
            self.assertIn('현재 참고할 복장: 미지정', ns['load_character_prompt']('a'))

    def test_relation_isolation_and_main_server_command(self):
        with tempfile.TemporaryDirectory() as folder:
            ns = functions({'ensure_guild_relation_table', 'set_guild_user_character_relation',
                            'get_guild_user_character_relation','clear_guild_user_character_relation',
                            'get_guild_user_relation_context', 'personal_relation_set'},
                           dict(sqlite3=sqlite3, DB_PATH=str(Path(folder)/'memory.db'),
                                datetime=datetime, timezone=timezone, CHARACTERS={'a':{},'b':{}},
                                SIMPLE_RELATIONS={}, GENERIC_CONFIG_ACTIVE=True,
                                feature_enabled=lambda _:True, character='a', config={'name':'A'}, log_message=Mock()))
            interaction = SimpleNamespace(guild=SimpleNamespace(id=1), user=SimpleNamespace(id=2),
                                          response=SimpleNamespace(send_message=AsyncMock()))
            asyncio.run(ns['personal_relation_set'](interaction,'고용인','3년째 근무'))
            get = ns['get_guild_user_character_relation']
            self.assertEqual(get(1,2,'a'), ('고용인','3년째 근무'))
            for guild,user,char in [(2,2,'a'),(1,3,'a'),(1,2,'b')]:
                self.assertEqual(get(guild,user,char), ('',''))
            self.assertEqual(ns['get_guild_user_relation_context'](2,2,'a'), '')
            self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
            ns['clear_guild_user_character_relation'](1,2,'a')
            self.assertEqual(get(1,2,'a'), ('',''))

    def test_empty_category_created_without_moving_existing_channel(self):
        guild = SimpleNamespace(categories=[], text_channels=[SimpleNamespace(name='hall')],
                                create_category=AsyncMock(return_value=SimpleNamespace(name='호텔')),
                                create_text_channel=AsyncMock())
        ns = functions({'initialize_rp_server_channels'}, dict(
            _get_channel_setup_settings=lambda:dict(category_names={'hotel':'호텔'}),
            PLACE_CHANNELS=['홀'], PLACE_CHANNEL_NAMES={'홀':'hall'}, PLACE_GROUPS={'홀':'hotel'},
            STATUS_BOT_TOKEN='', feature_enabled=lambda _:False))
        result = asyncio.run(ns['initialize_rp_server_channels'](guild))
        self.assertEqual(result['created_categories'], ['호텔'])
        guild.create_text_channel.assert_not_called()
        guild.create_category.reset_mock()
        asyncio.run(ns['initialize_rp_server_channels'](guild,create_categories=False))
        guild.create_category.assert_not_called()

    def test_monitor_request_completion_is_not_duplicated_or_leaked(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = Monitor(folder,['private-secret'])
            monitor.request('a','1','답변 생성 중','42','사용자: 안녕')
            monitor.request('a','2','답변 생성 중','43','다른 대화')
            monitor.request('a','1','전송 완료','42','답장 private-secret')
            data = read_snapshot(folder)
            self.assertEqual(len(data['requests']),1)
            self.assertNotIn('private-secret',str(data))
            replies = [e for e in data['events'] if e['text'].startswith('답장')]
            self.assertEqual(len(replies),1)
