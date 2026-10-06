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
        for index in range(11):
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
        panel.characters.setCurrentRow(next(i for i in range(panel.characters.count()) if panel.characters.item(i).data(256)==key)); panel.refresh()
        self.assertIn('호텔', panel.detail.text())
        self.assertIn('검은 코트', panel.detail.text())
        self.assertIn('답변 생성 중', next(panel.characters.item(i).text() for i in range(panel.characters.count()) if panel.characters.item(i).data(256)==key))
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

    def test_drafts_survive_tabs_and_item_switches(self):
        from PySide6.QtWidgets import QComboBox,QPlainTextEdit
        self.window.nav.setCurrentRow(1);page=self.window.stack.currentWidget()
        selector=page.findChild(QComboBox)
        edit=next(e for e in page.findChildren(QPlainTextEdit) if e.placeholderText().startswith('존댓말/반말'))
        edit.setPlainText('유지할 말투 초안')
        self.window.nav.setCurrentRow(3)
        self.window.stack.currentWidget().findChild(QLineEdit).setText('세계 초안')
        self.window.nav.setCurrentRow(1)
        self.assertIs(self.window.stack.currentWidget(),page)
        selector.setCurrentIndex(1);selector.setCurrentIndex(0)
        self.assertEqual(edit.toPlainText(),'유지할 말투 초안')
        self.assertTrue(self.window.dirty)
        saved=json.loads((self.root/'config/characters.json').read_text(encoding='utf-8'))
        self.assertNotEqual(saved['Alice']['prompt_guide']['speech'],'유지할 말투 초안')
        self.window.save_all()
        self.assertEqual(json.loads((self.root/'config/characters.json').read_text(encoding='utf-8'))['Alice']['prompt_guide']['speech'],'유지할 말투 초안')
        self.assertEqual(json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))['world_name'],'세계 초안')

    def test_bad_json_prevents_all_writes_and_keeps_drafts(self):
        from PySide6.QtWidgets import QPlainTextEdit
        self.window.nav.setCurrentRow(3)
        name=self.window.stack.currentWidget().findChild(QLineEdit);name.setText('저장되면 안 됨')
        self.window.nav.setCurrentRow(2)
        seats=next(e for e in self.window.stack.currentWidget().findChildren(QPlainTextEdit) if e.placeholderText().startswith('{"테이블'))
        seats.setPlainText('{"테이블": 4,}')
        before=(self.root/'config/settings.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'좌석별 정원.*JSON'):self.window.save_all()
        self.assertEqual((self.root/'config/settings.json').read_bytes(),before)
        self.assertEqual(seats.toPlainText(),'{"테이블": 4,}')
        self.assertTrue(self.window.dirty)
        seats.setPlainText('{"테이블": 4}')
        self.window.save_all()
        self.assertEqual(json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))['world_name'],'저장되면 안 됨')

    def test_relation_pair_drafts_are_independent(self):
        from PySide6.QtWidgets import QComboBox,QPlainTextEdit
        self.window.nav.setCurrentRow(4);page=self.window.stack.currentWidget()
        first,second=page.findChildren(QComboBox)[:2];editor=page.findChild(QPlainTextEdit)
        editor.setPlainText('첫 번째 관계 초안');second.setCurrentIndex(2)
        editor.setPlainText('두 번째 관계 초안');second.setCurrentIndex(1)
        self.assertEqual(editor.toPlainText(),'첫 번째 관계 초안')
        self.window.save_all()
        saved=json.loads((self.root/'config/relations.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['Alice|Saya']['context'],'첫 번째 관계 초안')
        self.assertEqual(saved['Alice|Yuri']['context'],'두 번째 관계 초안')

    def test_close_cancel_save_discard_and_save_failure(self):
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox
        from PySide6.QtGui import QCloseEvent
        self.window.nav.setCurrentRow(3)
        edit=self.window.stack.currentWidget().findChild(QLineEdit);edit.setText('종료 시 저장')
        def choose(text):
            def run(dialog):
                self.assertEqual(dialog.text(),'변경한 내용을 저장할까요?')
                next(b for b in dialog.buttons() if b.text()==text).click()
                return 0
            return run
        event=QCloseEvent()
        with patch.object(QMessageBox,'exec',choose('취소')):self.window.closeEvent(event)
        self.assertFalse(event.isAccepted());self.assertTrue(self.window.dirty)
        with patch.object(QMessageBox,'exec',choose('저장 후 종료')),patch.object(self.window.store,'flush',side_effect=OSError('쓰기 실패')),patch.object(self.window,'error'):
            event=QCloseEvent();self.window.closeEvent(event)
        self.assertFalse(event.isAccepted());self.assertEqual(edit.text(),'종료 시 저장');self.assertTrue(self.window.dirty)
        with patch.object(QMessageBox,'exec',choose('저장 후 종료')):
            event=QCloseEvent();self.window.closeEvent(event)
        self.assertTrue(event.isAccepted());self.assertFalse(self.window.dirty)
        self.assertEqual(json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))['world_name'],'종료 시 저장')
        edit.setText('버릴 수정')
        with patch.object(QMessageBox,'exec',choose('저장하지 않고 종료')):
            event=QCloseEvent();self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))['world_name'],'종료 시 저장')

    def test_checkbox_help_and_long_fields(self):
        from setup_gui import ResizableTextEdit
        from setup_wizard import FEATURE_QUESTIONS
        from form_help import CHECKBOX_HELP
        self.assertTrue(all(k in CHECKBOX_HELP for k,_,_ in FEATURE_QUESTIONS))
        self.window.nav.setCurrentRow(3);page=self.window.stack.currentWidget()
        from PySide6.QtWidgets import QLabel
        self.assertTrue(any('개인별 사용 설정도' in label.text() for label in page.findChildren(QLabel)))
        self.assertTrue(all(e.minimumHeight()>=150 for e in page.findChildren(ResizableTextEdit)))

    def test_monitor_controls_do_not_mark_settings_dirty(self):
        self.window.nav.setCurrentRow(9)
        panel=self.window.stack.currentWidget();panel.search.setText('검색')
        panel.filter.setCurrentText('수면');panel.sort.setCurrentText('허기 높은 순')
        self.assertFalse(self.window.dirty)

    def test_example_notice_is_only_on_examples(self):
        from unittest.mock import patch
        from PySide6.QtWidgets import QComboBox,QLabel
        self.window.nav.setCurrentRow(1);page=self.window.stack.currentWidget()
        self.assertTrue(any('참고용 예시' in label.text() for label in page.findChildren(QLabel)))
        with patch('setup_gui.QInputDialog.getText',return_value=('MyCharacter',True)):self.window.add_item('characters')
        selector=page.findChild(QComboBox);selector.setCurrentText('MyCharacter')
        self.assertFalse(self.window.store.data['characters']['MyCharacter'].get('is_example',False))
        self.assertFalse(any('참고용 예시' in label.text() and not label.parentWidget().isHidden() for label in page.findChildren(QLabel)))

    def test_navigation_builder_error_restores_selection(self):
        from unittest.mock import patch
        self.window.builders[3]=lambda: (_ for _ in ()).throw(ValueError('page error'))
        with patch.object(self.window,'error'):self.window.nav.setCurrentRow(3)
        self.assertEqual(self.window.nav.currentRow(),self.window.stack.currentIndex())

    def test_update_preference_is_a_draft_and_survives_refresh(self):
        self.window.nav.setCurrentRow(7)
        check=self.window.stack.currentWidget().findChild(QCheckBox)
        check.setChecked(False)
        self.assertTrue(self.window.dirty)
        self.assertFalse((self.root/'desktop_settings.json').exists())
        self.window.refresh()
        self.assertFalse(self.window.stack.currentWidget().findChild(QCheckBox).isChecked())
        self.window.save_all()
        self.assertFalse(json.loads((self.root/'desktop_settings.json').read_text(encoding='utf-8'))['check_updates_on_start'])

    def test_category_and_world_drafts_save_without_overwriting_each_other(self):
        from unittest.mock import patch
        self.window.nav.setCurrentRow(3)
        self.window.stack.currentWidget().findChild(QLineEdit).setText('새 세계')
        self.window.nav.setCurrentRow(8)
        with patch('setup_gui.QInputDialog.getText',side_effect=[('hotel',True),('호텔',True)]):
            next(b for b in self.window.stack.currentWidget().findChildren(QPushButton) if b.text()=='추가').click()
        self.assertNotIn('hotel',json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))['channel_setup']['category_names'])
        self.window.save_all()
        saved=json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['world_name'],'새 세계')
        self.assertEqual(saved['channel_setup']['category_names']['hotel'],'호텔')

    def test_calendar_event_drafts_survive_tabs_and_save_with_world_changes(self):
        from unittest.mock import patch
        from PySide6.QtWidgets import QComboBox
        self.window.nav.setCurrentRow(10)
        page=self.window.stack.currentWidget()
        next(w for w in page.findChildren(QCheckBox) if w.property('field_key')=='show_date').setChecked(False)
        with patch('setup_gui.QInputDialog.getText',return_value=('test_event',True)):
            next(b for b in page.findChildren(QPushButton) if b.text()=='기념일 추가').click()
        name=next(w for w in page.findChildren(QLineEdit) if w.property('field_key')=='name' and w.parentWidget().property('draft_key')=='event:test_event')
        name.setText('저장할 생일')
        self.window.nav.setCurrentRow(3);self.window.stack.currentWidget().findChild(QLineEdit).setText('달력 세계')
        self.window.nav.setCurrentRow(0);self.window.nav.setCurrentRow(10)
        self.assertEqual(name.text(),'저장할 생일');self.window.save_all()
        saved=json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['calendar_events']['test_event']['name'],'저장할 생일');self.assertEqual(saved['world_name'],'달력 세계')
        self.assertIn('season_profiles',saved['calendar'])

    def test_dependency_toggle_accept_and_cancel_restore_checks(self):
        from unittest.mock import patch
        self.window.nav.setCurrentRow(3);page=self.window.stack.currentWidget()
        checks={w.property('field_key'):w for w in page.findChildren(QCheckBox)}
        with patch.object(self.window,'confirm',return_value=False):checks['world_simulation'].setChecked(False)
        self.assertTrue(checks['world_simulation'].isChecked());self.assertTrue(checks['place_movement'].isChecked());self.assertFalse(self.window.dirty)
        with patch.object(self.window,'confirm',return_value=True):checks['world_simulation'].setChecked(False)
        self.assertFalse(checks['place_movement'].isChecked());self.assertFalse(checks['sleep_system'].isChecked());self.assertTrue(checks['autonomous_messages'].isChecked())
        self.window.save_all()
        with patch.object(self.window,'confirm',return_value=True):checks['sleep_quality'].setChecked(True)
        self.assertTrue(checks['world_simulation'].isChecked());self.assertTrue(checks['sleep_system'].isChecked())

    def test_invalid_configuration_blocks_run_without_spawning(self):
        from unittest.mock import patch
        self.window.store.data['settings']['features']['world_simulation']=False
        with patch('setup_gui.bot_running',return_value=False),patch('setup_gui.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):self.window.run_bot()
            launch.assert_not_called()

    def test_dirty_run_saves_before_launch_and_cancel_keeps_drafts(self):
        from unittest.mock import patch,Mock
        (self.root/'src').mkdir();(self.root/'src/bot.py').write_text('# fixture')
        self.window.nav.setCurrentRow(3);self.window.stack.currentWidget().findChild(QLineEdit).setText('실행할 세계')
        dialog=Mock();save=Mock();cancel=Mock();dialog.addButton.side_effect=[save,cancel];dialog.clickedButton.return_value=cancel
        with patch('setup_gui.bot_running',return_value=False),patch('setup_gui.QMessageBox',return_value=dialog) as box,patch('setup_gui.subprocess.Popen') as launch:
            box.ButtonRole.AcceptRole=0;box.ButtonRole.RejectRole=1
            self.window.run_bot();launch.assert_not_called();self.assertTrue(self.window.dirty)
            dialog.addButton.side_effect=[save,cancel];dialog.clickedButton.return_value=save
            def assert_saved(*args,**kwargs):
                self.assertEqual(json.loads((self.root/'config/settings.json').read_text(encoding='utf-8'))['world_name'],'실행할 세계')
                self.assertFalse(self.window.dirty)
                return Mock()
            launch.side_effect=assert_saved;self.window.run_bot();launch.assert_called_once()
        self.assertEqual(self.window.nav.currentRow(),9)

    def test_failed_save_in_run_retains_draft_and_does_not_launch(self):
        from unittest.mock import patch,Mock
        self.window.nav.setCurrentRow(3);editor=self.window.stack.currentWidget().findChild(QLineEdit);editor.setText('실패 후 유지')
        dialog=Mock();save=Mock();dialog.addButton.side_effect=[save,Mock()];dialog.clickedButton.return_value=save
        with patch('setup_gui.bot_running',return_value=False),patch('setup_gui.QMessageBox',return_value=dialog),patch.object(self.window.store,'flush',side_effect=OSError('disk failure')),patch('setup_gui.subprocess.Popen') as launch:
            with self.assertRaises(OSError):self.window.run_bot()
            launch.assert_not_called()
        self.assertTrue(self.window.dirty);self.assertEqual(editor.text(),'실패 후 유지')

    def test_legacy_text_flags_render_as_checkboxes(self):
        self.window.store.data['settings']['features']['world_simulation']='true'
        self.window.store.data['settings']['features']['world_calendar']='off'
        self.window.nav.setCurrentRow(3)
        checks={w.property('field_key'):w for w in self.window.stack.currentWidget().findChildren(QCheckBox)}
        self.assertTrue(checks['world_simulation'].isChecked());self.assertFalse(checks['world_calendar'].isChecked())
