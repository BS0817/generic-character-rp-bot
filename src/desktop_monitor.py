"""Local monitoring snapshots. Never serializes bot credentials."""
import json
import re
import time
from threading import RLock
from collections import deque
from pathlib import Path
from settings_store import atomic_write


def redact(text, secrets=()):
    text = str(text)
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, '[숨김]')
    return re.sub(r'(?i)(Bearer\s+|sk-(?:proj-)?)[A-Za-z0-9_.-]+', r'\1[숨김]', text)


class Monitor:
    def __init__(self, root, secrets=()):
        self.lock = RLock()
        self.path = Path(root) / 'desktop_monitor.json'
        self.secrets = secrets
        self.events = deque(maxlen=200)
        self.characters = {}
        self.requests = {}

    def event(self, kind, character='', text='', channel=''):
        with self.lock:
            self.events.append(dict(time=time.time(), kind=kind, character=character,
                                    text=redact(text, self.secrets), channel=str(channel)))

    def request(self, character, request_id, phase, channel='', text=''):
        key = f'{character}:{request_id}'
        self.requests[key] = dict(character=character, phase=phase, channel=str(channel), time=time.time())
        if text and phase not in ('전송 완료', '오류'):
            self.event('대화', character, text, channel)
        if phase in ('전송 완료', '오류'):
            if phase == '전송 완료':
                self.characters.setdefault(character, {})['last_reply'] = time.time()
            self.event(phase, character, text or phase, channel)
            self.requests.pop(key, None)
        self.flush()

    def flush(self):
        try:
            with self.lock:
                atomic_write(self.path, json.dumps(dict(updated=time.time(), characters=self.characters,
                         requests=self.requests, events=list(self.events)), ensure_ascii=False, default=str))
        except OSError:
            # Monitoring failure must not interrupt a Discord reply.
            pass


def read_snapshot(root):
    try:
        return json.loads((Path(root) / 'desktop_monitor.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
