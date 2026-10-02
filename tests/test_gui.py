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
        for index in range(8):
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
        self.assertEqual(len([k for k in saved['features'] if not k.startswith('_')]),16)
        self.assertTrue((self.root/'backup_setup').exists())
    def test_api_key_roundtrip_masked(self):
        self.window.nav.setCurrentRow(5)
        page=self.window.stack.currentWidget(); fields=page.findChildren(QLineEdit)
        fields[0].setText('secret-key')
        self.assertEqual(fields[0].echoMode(),QLineEdit.EchoMode.Password)
        next(b for b in page.findChildren(QPushButton) if b.text()=='연결 설정 저장').click()
        self.window.store.reload(); self.assertEqual(self.window.store.env['OPENAI_API_KEY'],'secret-key')
