"""Read-only desktop dashboard, independent from the Discord event loop."""
import json
import sqlite3
import time
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QPlainTextEdit, QComboBox, QPushButton
from desktop_monitor import read_snapshot, redact


class MonitorPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.data_root = Path(window.store.env.get('BOT_DATA_DIR') or window.root)
        if not self.data_root.is_absolute():
            self.data_root = window.root / self.data_root
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        for label, action in [('봇 실행', window.run_bot), ('봇 정지', window.stop_bot)]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, fn=action: window.guard(fn))
            controls.addWidget(button)
        self.status = QLabel(); controls.addWidget(self.status, 1); layout.addLayout(controls)
        body = QHBoxLayout(); layout.addLayout(body, 3)
        self.characters = QListWidget(); body.addWidget(self.characters)
        right = QVBoxLayout(); body.addLayout(right, 3)
        self.detail = QLabel(); self.detail.setWordWrap(True); right.addWidget(self.detail)
        self.channel = QComboBox(); self.channel.addItem('전체 채널', '')
        self.channel.setProperty('skip_dirty', True); right.addWidget(self.channel)
        right.addWidget(QLabel('최근 대화'))
        self.conversation = QPlainTextEdit(); self.conversation.setReadOnly(True); right.addWidget(self.conversation, 2)
        right.addWidget(QLabel('프로필 · 프롬프트 · 저장된 기억'))
        self.memory = QPlainTextEdit(); self.memory.setReadOnly(True); right.addWidget(self.memory, 1)
        layout.addWidget(QLabel('오류 · 실행 기록'))
        self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); layout.addWidget(self.logs, 1)
        self.characters.currentRowChanged.connect(self.refresh)
        self.channel.currentIndexChanged.connect(self.refresh)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(2000)
        self.refresh()

    def refresh(self, *args):
        snapshot = read_snapshot(self.data_root)
        configs = self.window.store.data['characters']
        keys = [k for k in configs if not k.startswith('_')]
        states = snapshot.get('characters', {})
        live = time.time() - snapshot.get('updated', 0) < 15
        online = sum(bool(states.get(k, {}).get('connected')) for k in keys) if live else 0
        self.status.setText(f'연결 {online}/{len(keys)} · ' + ('상태 수신 중' if live else '정지 또는 상태 수신 대기'))
        selected = self.characters.currentRow()
        self.characters.blockSignals(True)
        self.characters.clear()
        for key in keys:
            state = states.get(key, {})
            phases = [r['phase'] for r in snapshot.get('requests', {}).values() if r['character'] == key]
            label = ' / '.join(phases) or ('온라인' if live and state.get('connected') else '오프라인')
            self.characters.addItem(f"{configs[key].get('name', key)} · {label}")
        self.characters.setCurrentRow(min(max(selected, 0), len(keys)-1))
        self.characters.blockSignals(False)
        key = keys[self.characters.currentRow()] if keys else ''
        state = states.get(key, {})
        stamp = state.get('last_reply')
        self.detail.setText(f"장소: {state.get('place') or '미정'} · 활동: {state.get('activity') or '미정'}\n"
                            f"복장: {state.get('outfit') or configs.get(key, {}).get('current_outfit') or configs.get(key, {}).get('default_outfit') or '미지정'}\n"
                            f"마지막 응답: {time.strftime('%H:%M:%S', time.localtime(stamp)) if stamp else '없음'}")
        events = snapshot.get('events', [])
        channels = sorted({e.get('channel', '') for e in events if e.get('channel')})
        known = {self.channel.itemData(i) for i in range(self.channel.count())}
        self.channel.blockSignals(True)
        for channel in channels:
            if channel not in known: self.channel.addItem(channel, channel)
        self.channel.blockSignals(False)
        channel = self.channel.currentData()
        def line(event):
            return f"[{time.strftime('%H:%M:%S', time.localtime(event['time']))}][{event['channel']}] {event['text']}"
        self.conversation.setPlainText('\n\n'.join(line(e) for e in events if e['character'] == key and e['kind'] in ('대화', '전송 완료') and (not channel or e['channel'] == channel)))
        errors = [e for e in events if e['kind'] == '오류']
        self.logs.setPlainText('\n'.join(line(e) for e in events if e['kind'] not in ('대화', '전송 완료'))
                               + ('\n\n오류 대처: API 키·사용량, Discord 연결 및 아래 오류 문구를 확인해주세요.' if errors else ''))
        self.logs.setStyleSheet('border:1px solid #d87272;' if errors else '')
        if not live:
            startup = self.data_root / 'desktop_startup.log'
            try:
                with startup.open('rb') as stream:
                    stream.seek(max(0, startup.stat().st_size - 16000))
                    text = stream.read().decode('utf-8', errors='replace')
                secrets = [v for k,v in self.window.store.env.items() if 'TOKEN' in k or 'KEY' in k or k in {c.get('token_env') for c in configs.values() if isinstance(c, dict)}]
                self.logs.setPlainText(redact(text, secrets))
            except OSError: pass
        profile = json.dumps({k: ('[숨김]' if any(word in k.lower() for word in ('token', 'key', 'secret')) else v) for k,v in configs.get(key, {}).items()}, ensure_ascii=False, indent=2)
        # Config may contain unknown secret fields; mask them before displaying.
        secrets = [v for k, v in self.window.store.env.items() if 'TOKEN' in k or 'KEY' in k or k in {c.get('token_env') for c in configs.values() if isinstance(c, dict)}]
        profile = redact(profile, secrets)
        memories = []
        db = self.data_root / 'discord_memory.db'
        if db.exists() and key:
            try:
                with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=.1) as conn:
                    memories = [row[0] for row in conn.execute('SELECT content FROM memories WHERE character=? ORDER BY id DESC LIMIT 20', (key,))]
            except sqlite3.Error:
                memories = ['기억을 아직 읽을 수 없습니다. 봇 실행 후 확인해주세요.']
        prompt_path = configs.get(key, {}).get('prompt_file')
        prompt = ''
        if prompt_path:
            try: prompt = self.window.store.safe_path(prompt_path).read_text(encoding='utf-8-sig')
            except (OSError, ValueError): prompt = '프롬프트 파일을 확인해주세요.'
        self.memory.setPlainText(redact('프로필\n' + profile + '\n\n프롬프트\n' + prompt + '\n\n최근 저장 기억\n' + '\n'.join(memories), secrets))
