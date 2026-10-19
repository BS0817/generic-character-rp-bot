"""Exercise the live deque history shape without Discord or paid model calls."""
import ast
import contextvars
import functools
import inspect
import sys
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from rp_policy import dialogue_reason, relationship_reason


class DialogueHistoryTests(unittest.TestCase):
    def test_recent_window_accepts_list_tuple_and_deque_without_mutation(self):
        entries = ['a: 오래된 말'] + [f'a: 최근 문장 {i}' for i in range(6)]
        for factory in (list, tuple, lambda rows: deque(rows, maxlen=10)):
            history = factory(entries)
            with self.subTest(history_type=type(history).__name__):
                self.assertEqual(dialogue_reason('오래된 말', history), '')
                self.assertEqual(dialogue_reason('최근 문장 5', history), '최근 발언 반복')
                self.assertEqual(list(history), entries)

    def test_deque_agreement_detection_and_empty_history(self):
        self.assertEqual(dialogue_reason('좋아.', deque(['a: 그래.', 'b: 맞아.'])), '단순 동의 연쇄')
        self.assertEqual(dialogue_reason('안녕', deque()), '')

    def test_generation_guard_accepts_live_mention_history_and_preserves_retry_limit(self):
        source = ast.parse((ROOT / 'src/bot.py').read_text(encoding='utf-8'))
        node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == '_checked_generation')
        scope = contextvars.ContextVar('test_generation_scope', default='')
        feedback = contextvars.ContextVar('test_generation_feedback', default='')
        namespace = dict(inspect=inspect, functools=functools,
            _generation_scope=scope, _generation_feedback=feedback,
            CURRENT_LOG_SCOPE=contextvars.ContextVar('test_log_scope', default='world'),
            CURRENT_LOG_GUILD_ID=contextvars.ContextVar('test_guild', default='1'),
            _monitor=Mock(), feature_enabled=lambda key: key == 'dialogue_guard',
            dialogue_reason=dialogue_reason, relationship_reason=relationship_reason,
            recent_autonomous_messages=defaultdict(lambda: deque(maxlen=6)),
            _autonomous_history_key=lambda *args: args)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<generation guard>', 'exec'), namespace)
        for reply, expected_calls in [('새로운 답변', 1), ('이미 한 말', 2)]:
            calls = []
            def generate(character, history):
                calls.append(character)
                return reply, None
            history = deque(['사용자: 안녕', '캐릭터: 이미 한 말'], maxlen=10)
            with self.subTest(reply=reply):
                result = namespace['_checked_generation'](generate)('a', history)
                self.assertEqual(result, (reply, None) if expected_calls == 1 else ('', None))
                self.assertEqual(len(calls), expected_calls)
                self.assertEqual(scope.get(), '')
                self.assertEqual(feedback.get(), '')
                self.assertEqual(len(history), 2)
