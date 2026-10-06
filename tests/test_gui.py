from contextlib import closing
"""Headless Qt smoke test: render every page and edit a real setting."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
try:
    from PySide6.QtWidgets import QApplication,QLineEdit,QPushButton,QCheckBox
    from setup_gui import Window,STYLE
    QT_AVAILABLE=True
except ImportError:
    QT_AVAILABLE=False


@unittest.skipUnless(QT_AVAILABLE,'GUI dependencies are optional for headless bot deployments')
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        project=Path(__file__).resolve().parents[1]
        shutil.copytree(project/'config',self.root/'config'); shutil.copytree(project/'prompts',self.root/'prompts')
        self.window=Window(self.root)
        self.window.auto_check=lambda:None
    def tearDown(self):
        self.window.dirty=False; self.window.close(); self.window.deleteLater(); self.temp.cleanup()
    def test_all_pages_construct_and_world_save(self):
        for index in range(10):
            self.window.nav.setCurrentRow(index)
            self.assertEqual(self.window.stack.currentIndex(),index)
        self.window.nav.setCurrentRow(3)
        page=self.window.stack.currentWidget()
        names=page.findChildren(QLineEdit)
        names[0].setText('Edited World')
        next(b for b in page.findChildren(QPushButton) if b.text()=='세계관 · 기능 저장').click()
        saved=json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['world_name'],'Edited World')
        self.assertEqual(saved['runtime']['MAX_BOT_CHAIN'],8)
        from setup_wizard import FEATURE_QUESTIONS
        self.assertEqual(len([k for k in saved['features'] if not k.startswith('_')]),len(FEATURE_QUESTIONS))
        self.assertTrue((self.root/'backup_setup').exists())
    def test_api_key_roundtrip_masked(self):
        self.window.nav.setCurrentRow(5)
        page=self.window.stack.currentWidget(); fields=page.findChildren(QLineEdit)
        fields[0].setText('secret-key')
        self.assertEqual(fields[0].echoMode(),QLineEdit.EchoMode.Password)
        next(b for b in page.findChildren(QPushButton) if b.text()=='연결 설정 저장').click()
        self.window.store.reload(); self.assertEqual(self.window.store.env['OPENAI_API_KEY'],'secret-key')

    def test_monitor_displays_fixture_and_masks_secrets(self):
        from desktop_monitor import Monitor
        self.window.store.write_env({'OPENAI_API_KEY':'sk-test-private'})
        key = next(k for k in self.window.store.data['characters'] if not k.startswith('_'))
        monitor = Monitor(self.root, ['sk-test-private'])
        monitor.characters[key] = dict(connected=True,place='호텔',outfit='검은 코트',activity='독서 중')
        monitor.request(key, '42', '답변 생성 중', '123', '안녕하세요 sk-test-private')
        self.window.nav.setCurrentRow(9)
        panel = self.window.stack.currentWidget()
        self.assertIn('호텔', panel.detail.text())
        self.assertIn('검은 코트', panel.detail.text())
        self.assertIn('답변 생성 중', panel.characters.item(0).text())
        self.assertNotIn('sk-test-private', panel.conversation.toPlainText())
        self.assertIn('[숨김]', panel.conversation.toPlainText())

    def test_wheel_does_not_change_focused_weight(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from setup_gui import NoWheelDoubleSpinBox
        widget = NoWheelDoubleSpinBox(); widget.setValue(5); widget.show(); widget.setFocus()
        event = QWheelEvent(QPointF(5,5), QPointF(5,5), QPoint(0,0), QPoint(0,120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(widget, event)
        self.assertEqual(widget.value(), 5)
        self.assertFalse(event.isAccepted())
        widget.close()

    def test_category_add_rename_and_used_delete(self):
        from unittest.mock import patch
        self.window.nav.setCurrentRow(8)
        with patch('setup_gui.QInputDialog.getText', side_effect=[('hotel',True),('호텔',True)]):
            next(b for b in self.window.stack.currentWidget().findChildren(QPushButton) if b.text()=='추가').click()
        self.assertEqual(self.window.store.data['settings']['channel_setup']['category_names']['hotel'],'호텔')
        from PySide6.QtWidgets import QListWidget
        page = self.window.stack.currentWidget()
        listing = page.findChild(QListWidget)
        listing.setCurrentRow(listing.count()-1)
        with patch('setup_gui.QInputDialog.getText',return_value=('호텔 건물',True)):
            next(b for b in page.findChildren(QPushButton) if b.text()=='이름 수정').click()
        self.assertEqual(self.window.store.data['settings']['channel_setup']['category_names']['hotel'],'호텔 건물')
        self.window.store.data['places']['test'] = {'group':'hotel'}
        page = self.window.stack.currentWidget(); listing = page.findChild(QListWidget); listing.setCurrentRow(listing.count()-1)
        with patch.object(self.window, 'error') as error:
            next(b for b in page.findChildren(QPushButton) if b.text()=='삭제').click()
            self.assertTrue(error.called)
        self.assertIn('hotel', self.window.store.data['settings']['channel_setup']['category_names'])

    def test_monitor_filter_sort_and_scoped_memory_delete(self):
        import sqlite3
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox
        from desktop_monitor import Monitor
        keys=[k for k in self.window.store.data['characters'] if not k.startswith('_')]
        monitor=Monitor(self.root)
        for i,key in enumerate(keys):
            monitor.characters[key]=dict(connected=True,place='테스트 장소',activity='독서',hunger=10+60*i,fatigue=10,sleeping=bool(i))
        monitor.flush()
        db=self.root/'discord_memory.db'
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE memories(id INTEGER PRIMARY KEY,character TEXT,content TEXT,subject TEXT)')
            for i,key in enumerate(keys):conn.execute('INSERT INTO memories VALUES(?,?,?,?)',(i+1,key,'기억'+str(i),'user:123'))
        self.window.nav.setCurrentRow(9);panel=self.window.stack.currentWidget()
        panel.filter.setCurrentText('수면');self.assertEqual(panel.characters.count(),len(keys)-1)
        panel.filter.setCurrentText('전체');panel.sort.setCurrentText('허기 높은 순')
        self.assertEqual(panel.characters.item(0).data(256),keys[-1])
        panel.characters.setCurrentRow(0);panel.refresh()
        target=panel.selected_key
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):panel.delete_memory()
        with closing(sqlite3.connect(db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memories WHERE character=?',(target,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0],len(keys)-1)
        panel.search.setText('없는 장소');self.assertEqual(panel.characters.count(),0)

    def test_character_guide_and_outfit_fields_roundtrip(self):
        from PySide6.QtWidgets import QPlainTextEdit
        self.window.nav.setCurrentRow(1);page=self.window.stack.currentWidget()
        self.window.error=lambda error: (_ for _ in ()).throw(AssertionError(str(error)))
        key=next(k for k in self.window.store.data['characters'] if not k.startswith('_'))
        edits=page.findChildren(QPlainTextEdit)
        next(edit for edit in edits if edit.placeholderText().startswith('존댓말/반말')).setPlainText('반말, 짧은 문장')
        next(b for b in page.findChildren(QPushButton) if b.text()=='변경 사항 저장').click()
        key=next(k for k in self.window.store.data['characters'] if not k.startswith('_'))
        saved=json.loads((self.root/'config/characters.json').read_text(encoding='utf-8'))
        self.assertEqual(saved[key]['prompt_guide']['speech'],'반말, 짧은 문장')
        self.assertIn('outfit_preferences_enabled',saved[key])
