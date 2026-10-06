"""Korean desktop configuration manager. Existing JSON fields remain intact."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QPlainTextEdit, QCheckBox, QComboBox,
    QListWidget, QStackedWidget, QScrollArea, QMessageBox, QInputDialog,
    QFileDialog, QSpinBox, QDoubleSpinBox, QTimeEdit, QDialog, QDialogButtonBox, QSystemTrayIcon, QMenu, QStyle,
)
from settings_store import DraftStore, app_root, atomic_write
from app_version import VERSION
from runtime_options import OPTIONS, validate_options
from updater import latest_release, stage_release, launch_update, bot_running, BotLock
from setup_wizard import FEATURE_QUESTIONS
from monitor_panel import MonitorPanel
from rp_policy import DEFAULT_COMMON_PROMPT, GUIDE_FIELDS, build_prompt
from form_help import CHECKBOX_HELP, JSON_HELP, JSON_EXAMPLES, parse_json

STYLE = '''
QWidget { background:#151822; color:#e6e8ef; font-family:"Malgun Gothic"; font-size:14px; }
QMainWindow { background:#151822; }
QListWidget { background:#1c2030; border:0; border-radius:12px; padding:10px; }
QListWidget::item { padding:13px; border-radius:7px; }
QListWidget::item:selected { background:#5d4bb0; color:white; }
QLineEdit,QPlainTextEdit,QComboBox,QSpinBox,QDoubleSpinBox,QTimeEdit { background:#222738; border:1px solid #343b51; border-radius:7px; padding:8px; }
QPushButton { background:#7360cf; border:0; border-radius:8px; padding:10px 18px; color:white; }
QPushButton:hover { background:#8978e2; }
QPushButton:disabled { background:#343b51; color:#9096a9; }
QCheckBox { padding:7px; }
QCheckBox::indicator { width:19px; height:19px; }
QLabel#title { font-size:27px; font-weight:700; padding:4px 0 14px 0; }
QLabel#muted { color:#a3abc0; }
QScrollArea { border:0; }
QToolTip { background:#343b51; color:white; }
'''
CHAR_DEFAULT = dict(name='새 캐릭터', token_env='', prompt_file='', private_room=None,
    default_outfit='', current_outfit='', outfit_preferences_enabled=False, preferred_style='', preferred_colors='', disliked_outfits='', hated_outfits='', outfit_notes='', sleep_start_range=[[1,0],[3,0]], wake_range=[[8,0],[10,0]], inventory=[], habits=[], goals=[], place_weights={}, restricted_places=[])
PLACE_DEFAULT = dict(channel_name='', description='', objects=[], group='public', parent=None,
    allowed_characters=[], seats={}, menus={'전체':[]}, nap_allowed=False, outdoor=False, activities=[], time_multipliers=dict(morning=1.,day=1.,evening=1.,night=1.,late_night=1.))
LABELS = {'outfit_preferences_enabled':'복장 취향 사용','preferred_style':'선호 스타일','preferred_colors':'선호 색·소재','disliked_outfits':'별로 좋아하지 않는 복장','hated_outfits':'싫어하는 복장','outfit_notes':'복장 취향 추가 설명','seats':'좌석별 정원 (JSON)','menus':'시간대별 메뉴 (JSON)','nap_allowed':'낮잠 가능한 장소','outdoor':'야외 장소','activities':'단계별 활동 (한 줄에 하나)','default_outfit':'기본 복장','current_outfit':'현재 복장 (비우면 기본 복장)','name':'이름','token_env':'토큰 환경변수 이름','prompt_file':'프롬프트 경로',
    'private_room':'개인실 이름 (없으면 비워두기)','sleep_start_range':'취침 시작 범위','wake_range':'기상 범위',
    'inventory':'소지품 (한 줄에 하나)','habits':'생활 습관 (한 줄에 하나)','goals':'장기 목표 (한 줄에 하나)',
    'restricted_places':'출입 금지 장소 (한 줄에 하나)','channel_name':'Discord 채널 이름',
    'description':'장소 설명','objects':'주변 사물 (한 줄에 하나)','group':'장소 그룹',
    'parent':'상위 장소 (없으면 비워두기)','allowed_characters':'출입 허용 캐릭터 키 (비우면 모두)',
    'place_weights':'장소별 방문 가중치','time_multipliers':'시간대별 방문 배율','context':'관계 설명'}


class Job(QThread):
    done = Signal(object)
    failed = Signal(str)
    def __init__(self, fn, parent):
        super().__init__(parent)
        self.fn = fn
    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as error:
            self.failed.emit(str(error))


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event): event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event): event.ignore()


class TextHeightGrip(QLabel):
    def __init__(self, editor):
        super().__init__('↕',editor)
        self.editor=editor; self.origin=None
        self.setFixedSize(24,24); self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip('끌어서 입력칸 높이를 조절하세요.')
    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.origin=(event.globalPosition().y(),self.editor.height()); event.accept()
    def mouseMoveEvent(self,event):
        if self.origin:
            self.editor.setFixedHeight(max(120,int(self.origin[1]+event.globalPosition().y()-self.origin[0])))
            event.accept()
    def mouseReleaseEvent(self,event):
        self.origin=None; event.accept()


class ResizableTextEdit(QPlainTextEdit):
    """Long text fields with an independent vertical size grip."""
    def __init__(self, text='', height=190):
        super().__init__(text)
        self.setMinimumHeight(height)
        self.grip=TextHeightGrip(self)
        self.setViewportMargins(0,0,0,24)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.grip.move(self.width()-self.grip.width()-4,self.height()-self.grip.height()-4)


def checkbox_row(checkbox, key):
    row = QWidget(); layout = QHBoxLayout(row); layout.setContentsMargins(0,0,0,0)
    layout.addWidget(checkbox)
    help_text = QLabel(CHECKBOX_HELP.get(key, '이 설정의 사용 여부를 선택합니다.'))
    help_text.setWordWrap(True); help_text.setObjectName('muted')
    layout.addWidget(help_text, 1)
    return row


class Fields:
    """Typed form adapters; unknown keys are preserved by the caller."""
    def __init__(self, form):
        self.form = form
        self.readers = {}
    def add(self, key, value, label=None, secret=False, multiline=False):
        label = label or LABELS.get(key, key)
        if key in ('seats','menus'):
            widget = ResizableTextEdit(json.dumps(value,ensure_ascii=False,indent=2), 240)
            widget.setPlaceholderText('{"테이블 1": 4, "소파": 2}' if key=='seats' else '{"전체": ["차", "샌드위치"], "아침": ["토스트"]}')
            widget.setToolTip('좌석은 이름: 정원, 메뉴는 아침·점심·저녁·야식·전체: 음식 이름 목록으로 입력하세요.')
            reader = lambda: parse_json(widget.toPlainText(), label, key)
        elif isinstance(value, bool):
            widget = QCheckBox('사용')
            widget.setChecked(value)
            reader = widget.isChecked
        elif key in ('sleep_start_range','wake_range'):
            from PySide6.QtCore import QTime
            widget = QWidget(); layout = QHBoxLayout(widget); layout.setContentsMargins(0,0,0,0)
            times = []
            for hour, minute in value:
                edit = QTimeEdit(QTime(hour, minute)); edit.setDisplayFormat('HH:mm'); layout.addWidget(edit); times.append(edit)
            reader = lambda: [[e.time().hour(),e.time().minute()] for e in times]
        elif isinstance(value, dict):
            widget = QWidget(); subform = QFormLayout(widget); adapters = Fields(subform)
            for subkey, subvalue in value.items():
                if not subkey.startswith('_'):
                    adapters.add(subkey, subvalue)
            reader = lambda: dict(value, **adapters.values())
        elif isinstance(value, (int,float)):
            widget = NoWheelSpinBox() if isinstance(value,int) else NoWheelDoubleSpinBox()
            widget.setRange(0,100000); widget.setValue(value)
            if isinstance(widget,QDoubleSpinBox): widget.setDecimals(3)
            reader = widget.value
        elif isinstance(value,list) or multiline:
            widget = ResizableTextEdit('\n'.join(map(str,value)) if isinstance(value,list) else str(value or ''), 150 if isinstance(value,list) else 220)
            reader = (lambda: [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]) if isinstance(value,list) else widget.toPlainText
        else:
            widget = QLineEdit(str(value or ''))
            if secret:
                widget.setEchoMode(QLineEdit.EchoMode.Password)
                widget.setPlaceholderText('키 또는 토큰을 입력하세요')
            reader = widget.text
            if value is None: reader = lambda: widget.text().strip() or None
        self.form.addRow(label, checkbox_row(widget,key) if isinstance(widget,QCheckBox) else widget)
        if key in JSON_EXAMPLES:
            help_label = QLabel(JSON_HELP + '\n예시: ' + JSON_EXAMPLES[key])
            help_label.setWordWrap(True); help_label.setObjectName('muted'); self.form.addRow('',help_label)
        self.readers[key] = reader
        return widget
    def values(self):
        return {key:reader() for key,reader in self.readers.items()}


class Window(QMainWindow):
    def __init__(self, root=None):
        super().__init__()
        self.root = Path(root or app_root())
        self.store = DraftStore(self.root)
        self.page_cache = {}
        self.savers = {}
        self.dirty_forms = set()
        self._saving_all = False
        self.bot_process = None
        self.job = None
        self.release = None
        self.dirty = False
        self.setWindowTitle('RPBot · 설정 관리')
        self.resize(1180,820); self.setMinimumSize(920,660)
        container=QWidget(); self.setCentralWidget(container); layout=QHBoxLayout(container)
        sidebar=QVBoxLayout(); brand=QLabel('RPBot'); brand.setObjectName('title'); sidebar.addWidget(brand)
        sidebar.addWidget(QLabel(f'캐릭터 세계 관리  ·  v{VERSION}'))
        self.nav=QListWidget(); self.nav.setFixedWidth(220)
        self.nav.addItems(['시작하기','캐릭터','장소','세계관 · 기능','관계','API · Discord','백업 · 복원','업데이트','카테고리','모니터링'])
        sidebar.addWidget(self.nav); layout.addLayout(sidebar)
        self.stack=QStackedWidget(); layout.addWidget(self.stack,1)
        self.builders=[self.home,lambda:self.collection('characters'),lambda:self.collection('places'),self.world,self.relations,self.connections,self.backups,self.updates,self.categories,lambda:MonitorPanel(self)]
        for _ in self.builders: self.stack.addWidget(QWidget())
        self.nav.currentRowChanged.connect(self.navigate)
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage('저장 버튼은 모든 탭의 수정 내용을 저장합니다. 적용하려면 봇을 재시작하세요.')
        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
        menu = QMenu(self)
        menu.addAction('관리 창 열기', self.showNormal)
        menu.addAction('종료', self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        if QSystemTrayIcon.isSystemTrayAvailable(): self.tray.show()
        QTimer.singleShot(800,self.auto_check)

    def error(self,error): QMessageBox.warning(self,'확인해주세요',str(error))
    def guard(self,fn):
        try: fn()
        except Exception as error: self.error(error)
    def ok(self):
        if not self._saving_all:
            self.store.flush()
            self.dirty=False
        if not self._saving_all:
            self.statusBar().showMessage('저장 완료 · 이전 설정은 자동 백업했습니다. 봇을 재시작하면 적용됩니다.',10000)
    def confirm(self,text):
        return QMessageBox.question(self,'RPBot',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes
    def mark_dirty(self,*args):
        self.dirty=True
        widget=self.sender()
        while isinstance(widget,QWidget):
            token=widget.property('draft_key')
            if token:
                self.dirty_forms.add(token); break
            widget=widget.parentWidget()
        self.statusBar().showMessage('저장하지 않은 수정 사항이 있습니다. 탭을 이동해도 입력 내용은 유지됩니다.')
    def track(self,widget):
        if widget.property('skip_dirty'): return
        for child in widget.findChildren(QWidget):
            if child.property('draft_tracked'): continue
            child.setProperty('draft_tracked',True)
            if child.property('skip_dirty') or (isinstance(child,QPlainTextEdit) and child.isReadOnly()): continue
            for name in ('textChanged','toggled','valueChanged','timeChanged','currentTextChanged'):
                signal=getattr(child,name,None)
                if signal is not None:
                    signal.connect(self.mark_dirty)
                    break
    def save_all(self):
        # Validate and stage every retained form before any disk writes.
        before = (copy.deepcopy(self.store.data), dict(self.store.env), dict(self.store.pending))
        self._saving_all = True
        try:
            for token,save in list(self.savers.items()):
                if token in self.dirty_forms: save()
            self.store.flush()
        except Exception:
            self.store.data, self.store.env, self.store.pending = before
            self.statusBar().showMessage('저장하지 못했습니다. 수정 내용은 유지됩니다. 오류 항목을 확인하세요.')
            raise
        finally:
            self._saving_all = False
        self.dirty = False
        self.dirty_forms.clear()
        self.statusBar().showMessage('모든 수정 사항을 저장했습니다.')

    def navigate(self,index):
        if index<0: return
        previous = self.stack.currentIndex()
        try:
            if index not in self.page_cache:
                page=self.builders[index]()
                if index in (0,6,7,8,9): page.setProperty('skip_dirty',True)
                old=self.stack.widget(index); self.stack.removeWidget(old); old.deleteLater()
                self.stack.insertWidget(index,page); self.page_cache[index]=page
                self.track(page)
            page=self.page_cache[index]
            if hasattr(page,'sync_items'): page.sync_items()
            self.stack.setCurrentIndex(index)
        except Exception as error:
            self.nav.blockSignals(True); self.nav.setCurrentRow(previous); self.nav.blockSignals(False)
            self.error(error)

    def refresh(self):
        # Only utility pages are rebuilt. Editing pages retain their widgets.
        index=self.nav.currentRow()
        if index in (0,6,7,8): self.page_cache.pop(index,None)
        self.navigate(index)

    def page(self,title,note=''):
        page=QWidget(); layout=QVBoxLayout(page)
        heading=QLabel(title); heading.setObjectName('title'); layout.addWidget(heading)
        if note:
            label=QLabel(note); label.setWordWrap(True); label.setObjectName('muted'); layout.addWidget(label)
        return page,layout
    def button(self,layout,text,fn):
        button=QPushButton(text); button.clicked.connect(lambda:self.guard(fn)); layout.addWidget(button); return button
    def scrolled_form(self,layout):
        scroll=QScrollArea(); scroll.setWidgetResizable(True); content=QWidget(); form=QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(content); layout.addWidget(scroll,1); return Fields(form),content

    def home(self):
        page,layout=self.page('내 캐릭터의 세계를 시작하세요','처음 사용하는 경우 API · Discord 연결 → 세계관 → 장소 → 캐릭터 순서로 설정하세요.')
        for title,name in [('캐릭터','characters'),('장소','places'),('초기 관계','relations')]:
            layout.addWidget(QLabel(f'{title}   {len([k for k in self.store.data[name] if not k.startswith("_")])}개'))
        layout.addWidget(QLabel('설정과 프롬프트는 저장 전에 자동으로 백업됩니다.\n대화 기록과 기억 데이터는 설정 변경으로 지워지지 않습니다.'))
        self.button(layout,'봇 실행',self.run_bot)
        self.button(layout,'설정 폴더 열기',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root))))
        self.button(layout,'초보자 설치 가이드',lambda:QDesktopServices.openUrl(QUrl('https://bs0817.github.io/generic-character-rp-bot/guide_ko.html')))
        layout.addStretch(); return page
    def run_bot(self):
        if bot_running(self.root): raise ValueError('RPBot이 이미 실행 중입니다.')
        if self.dirty: raise ValueError('설정을 먼저 저장해주세요.')
        command=[str(self.root/'RPBot.exe')] if getattr(sys,'frozen',False) else [sys.executable,str(self.root/'src'/'bot.py')]
        if not Path(command[-1]).exists(): raise ValueError('RPBot 실행 파일이 없습니다.')
        data_root = Path(self.store.env.get('BOT_DATA_DIR') or self.root)
        if not data_root.is_absolute(): data_root = self.root / data_root
        data_root.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, RPBOT_MANAGED_DESKTOP='1', PYTHONUNBUFFERED='1')
        with (data_root / 'desktop_startup.log').open('w', encoding='utf-8') as output:
            self.bot_process = subprocess.Popen(command,cwd=self.root,env=env,stdout=output,stderr=subprocess.STDOUT,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.statusBar().showMessage('봇 실행 요청 완료 · 모니터링에서 상태를 확인하세요.')
        self.nav.setCurrentRow(9)

    def stop_bot(self):
        if not bot_running(self.root):
            self.statusBar().showMessage('실행 중인 봇이 없습니다.'); return
        data_root = Path(self.store.env.get('BOT_DATA_DIR') or self.root)
        if not data_root.is_absolute(): data_root = self.root / data_root
        atomic_write(data_root / 'desktop_stop.request', 'stop')
        self.statusBar().showMessage('종료 요청 완료 · 현재 처리 작업 후 연결을 정리합니다.')

    def categories(self):
        page, layout = self.page('카테고리 관리', '카테고리는 /서버초기화로 생성합니다. 이름 변경·삭제는 설정에만 적용되며 기존 Discord 채널은 유지됩니다.')
        aliases = copy.deepcopy(self.store.data['settings'].get('channel_setup', {}).get('category_names', {}))
        listing = QListWidget(); listing.addItems([f'{key} → {value}' for key,value in aliases.items()]); layout.addWidget(listing)
        def selected():
            row = listing.currentRow()
            return list(aliases)[row] if 0 <= row < len(aliases) else None
        def persist():
            settings = copy.deepcopy(self.store.data['settings'])
            settings.setdefault('channel_setup', {})['category_names'] = aliases
            self.store.write_json('settings', settings); self.mark_dirty(); self.refresh()
        def add():
            key, ok = QInputDialog.getText(self, '카테고리 추가', '카테고리 키 (장소 그룹):')
            if not ok: return
            key = key.strip()
            if not key or len(key)>100 or key in aliases: raise ValueError('중복되지 않는 1~100자 키를 입력하세요.')
            name, ok = QInputDialog.getText(self, '카테고리 이름', 'Discord에 표시할 이름:', text=key)
            if not ok: return
            name = name.strip()
            if not name or len(name)>100 or name in aliases.values(): raise ValueError('중복되지 않는 1~100자 이름을 입력하세요.')
            aliases[key] = name; persist()
        def rename():
            key = selected()
            if key is None: return
            name, ok = QInputDialog.getText(self, '카테고리 이름 수정', '새 표시 이름:', text=aliases[key])
            if not ok: return
            name = name.strip()
            if not name or len(name)>100 or any(v == name for k,v in aliases.items() if k != key): raise ValueError('중복되지 않는 1~100자 이름을 입력하세요.')
            aliases[key] = name; persist()
        def remove():
            key = selected()
            if key is None: return
            if any(p.get('group') == key for p in self.store.data['places'].values() if isinstance(p,dict)):
                raise ValueError('이 카테고리를 사용하는 장소의 카테고리를 먼저 변경하세요.')
            if self.confirm('설정에서 카테고리를 삭제할까요? Discord의 기존 카테고리는 삭제하지 않습니다.'):
                del aliases[key]; persist()
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        for name, fn in [('추가',add),('이름 수정',rename),('삭제',remove)]: self.button(buttons,name,fn)
        return page

    def collection(self,name):
        character=name=='characters'; title='캐릭터' if character else '장소'
        page,layout=self.page(f'{title} 관리','목록에서 선택해 편집하세요. 내부 키는 기존 관계와 연결되므로 생성 후 유지됩니다.')
        toolbar=QHBoxLayout(); selector=QComboBox(); keys=[k for k in self.store.data[name] if not k.startswith('_')]
        selector.addItems(keys); toolbar.addWidget(selector,1)
        self.button(toolbar,f'{title} 추가',lambda:self.add_item(name))
        self.button(toolbar,'선택 삭제',lambda:self.delete_item(name,selector.currentText()))
        layout.addLayout(toolbar)
        body=QWidget(); body_layout=QVBoxLayout(body); layout.addWidget(body,1)
        selected=[selector.currentText()]
        panels = {}
        selector.setProperty('skip_dirty',True)
        def render(key):
            if not key: return
            for panel in panels.values(): panel.hide()
            if key in panels:
                panels[key].show(); return
            panel=QWidget(); panel_layout=QVBoxLayout(panel); panel_layout.setContentsMargins(0,0,0,0)
            body_layout.addWidget(panel); panels[key]=panel
            draft_token=f'{name}:{key}'; panel.setProperty('draft_key',draft_token)
            existing=copy.deepcopy(self.store.data[name][key]); defaults=CHAR_DEFAULT if character else PLACE_DEFAULT
            fields,content=self.scrolled_form(panel_layout)
            panel_layout.insertWidget(0,QLabel('저장 버튼은 모든 탭의 수정 내용을 함께 저장합니다.'))
            if existing.get('is_example'):
                notice=QLabel('이 캐릭터는 참고용 예시입니다. 예시를 수정해 사용하기보다 위의 ‘캐릭터 추가’로 새 캐릭터를 만드세요.')
                notice.setWordWrap(True); notice.setObjectName('muted'); panel_layout.insertWidget(0,notice)
            for field,default in defaults.items():
                value=existing.get(field,copy.deepcopy(default))
                if field=='place_weights':
                    value=dict(value)
                    for place in self.store.data['places']:
                        if not place.startswith('_'): value.setdefault(place,1)
                if not character and field == 'group':
                    groups = self.store.data['settings'].get('channel_setup', {}).get('category_names', {})
                    combo = QComboBox(); combo.setEditable(True)
                    combo.addItems(list(dict.fromkeys([str(value or 'public'), *groups])))
                    combo.setCurrentText(str(value or 'public'))
                    fields.form.addRow('카테고리 (장소 그룹)', combo)
                    fields.readers[field] = combo.currentText
                    combo.currentTextChanged.connect(self.mark_dirty)
                else:
                    fields.add(field,value,multiline=field in ('description','default_outfit','current_outfit','preferred_style','preferred_colors','disliked_outfits','hated_outfits','outfit_notes'))
            token=None; prompt=None; avatar=None
            if character:
                token=fields.add('_token',self.store.env.get(existing.get('token_env',''),'') or '','Discord 봇 토큰',secret=True)
                show=QCheckBox('토큰 보기'); show.setProperty('skip_dirty',True); fields.form.addRow('',checkbox_row(show,'show_token'))
                show.toggled.connect(lambda checked:token.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
                prompt_path=existing.get('prompt_file') or f'prompts/{key}.txt'
                path=self.store.safe_path(prompt_path)
                prompt=ResizableTextEdit(self.store.pending[path].decode('utf-8') if path in self.store.pending else path.read_text(encoding='utf-8-sig') if path.exists() else '[정체성]\n이름: '+existing.get('name',key)+'\n\n[성격]\n\n[말투]\n')
                prompt.setMinimumHeight(360); fields.form.addRow('캐릭터 프롬프트 (직접 편집)',prompt)
                guided = Fields(fields.form)
                for field,(label,hint) in GUIDE_FIELDS.items():
                    widget = guided.add(field,existing.get('prompt_guide',{}).get(field,''),'작성 가이드 · '+label,multiline=True)
                    widget.setPlaceholderText(hint)
                preview = QPushButton('가이드로 프롬프트 미리보기'); fields.form.addRow('',preview)
                def show_preview():
                    values=guided.values()
                    if not values['personality'].strip() or not values['speech'].strip():
                        raise ValueError('핵심 성격과 말투를 입력해주세요.')
                    draft=build_prompt(fields.values()['name'],values)
                    dialog=QDialog(self); dialog.setWindowTitle('프롬프트 미리보기')
                    box=QVBoxLayout(dialog); text=QPlainTextEdit(draft); text.setReadOnly(True); box.addWidget(text)
                    buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
                    buttons.button(QDialogButtonBox.StandardButton.Ok).setText('편집 칸에 적용')
                    box.addWidget(buttons); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
                    dialog.resize(650,600)
                    if dialog.exec()==QDialog.DialogCode.Accepted:
                        prompt.setPlainText(draft); self.mark_dirty()
                preview.clicked.connect(lambda:self.guard(show_preview))
                avatar=QLabel('프로필 이미지 없음'); avatar.setMinimumHeight(100)
                avatar_path=existing.get('profile_image')
                if avatar_path:
                    pix=QPixmap(str(self.store.safe_path(avatar_path)))
                    if not pix.isNull(): avatar.setPixmap(pix.scaled(110,110,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                fields.form.addRow('관리 화면 프로필',avatar)
                pick=QPushButton('이미지 선택'); fields.form.addRow('',pick)
                def choose_image():
                    filename,_=QFileDialog.getOpenFileName(self,'프로필 이미지',str(self.root),'Images (*.png *.jpg *.jpeg *.webp)')
                    if filename:
                        pix=QPixmap(filename)
                        if pix.isNull(): raise ValueError('이미지를 읽을 수 없습니다.')
                        destination=self.root/'profiles'/f'{key}.png'
                        from PySide6.QtCore import QBuffer, QIODevice
                        buffer=QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                        if not pix.save(buffer,'PNG'): raise ValueError('이미지 저장에 실패했습니다.')
                        self.store.stage(destination,bytes(buffer.data()))
                        existing['profile_image']=str(destination.relative_to(self.root))
                        avatar.setPixmap(pix.scaled(110,110,Qt.AspectRatioMode.KeepAspectRatio)); self.dirty_forms.add(draft_token); self.mark_dirty()
                pick.clicked.connect(lambda:self.guard(choose_image))
            def save():
                values=fields.values(); secret=values.pop('_token',None)
                if not str(values.get('name' if character else 'channel_name','')).strip(): raise ValueError('이름을 입력해주세요.')
                if character:
                    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',values['token_env']): raise ValueError('토큰 환경변수 이름을 확인해주세요.')
                    if values.get('private_room') and values['private_room'] not in self.store.data['places'] and values['private_room'] != existing.get('private_room'):
                        raise ValueError('개인실을 장소 메뉴에 먼저 추가해주세요.')
                    for other_key,other in self.store.data['characters'].items():
                        if other_key!=key and not other_key.startswith('_') and other.get('token_env')==values['token_env']:
                            raise ValueError('다른 캐릭터가 사용하는 토큰 환경변수 이름입니다.')
                    self.store.safe_path(values['prompt_file'])
                    for restricted in values['restricted_places']:
                        if restricted not in self.store.data['places']: raise ValueError(f'없는 장소: {restricted}')
                    values['prompt_guide']=guided.values()
                    self.store.save_prompt(values['prompt_file'],prompt.toPlainText())
                    self.store.write_env({values['token_env']:secret})
                else:
                    if not isinstance(values['seats'],dict) or any(not str(k).strip() or isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=100 for k,v in values['seats'].items()):
                        raise ValueError('좌석은 이름과 1~100 정원으로 입력해주세요.')
                    if not isinstance(values['menus'],dict) or any(not isinstance(v,list) or any(not isinstance(m,str) or not m.strip() for m in v) for v in values['menus'].values()):
                        raise ValueError('메뉴는 시간대별 음식 이름 목록으로 입력해주세요.')
                    if values.get('parent')==key: raise ValueError('상위 장소는 자기 자신일 수 없습니다.')
                    if values.get('parent') and values['parent'] not in self.store.data['places']: raise ValueError('상위 장소가 없습니다.')
                    seen={key}; ancestor=values.get('parent')
                    while ancestor:
                        if ancestor in seen: raise ValueError('상위 장소가 서로 순환하지 않도록 설정해주세요.')
                        seen.add(ancestor); ancestor=self.store.data['places'].get(ancestor,{}).get('parent')
                    for allowed in values['allowed_characters']:
                        if allowed not in self.store.data['characters']: raise ValueError(f'없는 캐릭터: {allowed}')
                data=copy.deepcopy(self.store.data[name])
                if key not in data: return
                data[key]={**data[key],**existing,**values}
                self.store.write_json(name,data); self.ok()
            self.savers[draft_token]=save
            self.button(panel_layout,'변경 사항 저장',self.save_all)
            self.track(content)
        def change(key):
            selected[0]=key; self.guard(lambda:render(key))
        def sync_items():
            current=selector.currentText()
            keys=[k for k in self.store.data[name] if not k.startswith('_')]
            if [selector.itemText(i) for i in range(selector.count())] != keys:
                selector.blockSignals(True); selector.clear(); selector.addItems(keys)
                if current in keys: selector.setCurrentText(current)
                selector.blockSignals(False)
                for key in list(panels):
                    if key not in keys:
                        panels.pop(key).deleteLater(); self.savers.pop(f'{name}:{key}',None); self.dirty_forms.discard(f'{name}:{key}')
                render(selector.currentText())
        page.sync_items=sync_items
        selector.currentTextChanged.connect(change); render(selector.currentText()); return page

    def add_item(self,name):
        key,accepted=QInputDialog.getText(self,'추가','캐릭터 내부 키 (영문/숫자/밑줄)' if name=='characters' else '장소 이름')
        key=key.strip()
        if not accepted: return
        if not key or key.startswith('_') or '|' in key or '/' in key or '\\' in key: raise ValueError('사용할 수 없는 이름입니다.')
        if name=='characters' and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',key): raise ValueError('캐릭터 내부 키는 영문으로 시작하고 영문/숫자/밑줄/하이픈을 사용해주세요.')
        if key in self.store.data[name]: raise ValueError('이미 존재합니다.')
        value=copy.deepcopy(CHAR_DEFAULT if name=='characters' else PLACE_DEFAULT)
        if name=='characters': value.update(name=key,token_env='DISCORD_'+key.upper().replace('-','_')+'_TOKEN',prompt_file=f'prompts/{key}.txt')
        else: value['channel_name']=key
        data=copy.deepcopy(self.store.data[name]); data[key]=value; self.store.write_json(name,data); self.mark_dirty(); self.refresh()
    def delete_item(self,name,key):
        if not key: return
        if not self.confirm(f'{key}을 삭제할까요?\n연결된 초기 관계와 출입 설정도 정리합니다. 대화 기록은 유지됩니다.'): return
        (self.store.remove_character if name=='characters' else self.store.remove_place)(key)
        self.savers.pop(f'{name}:{key}',None); self.dirty_forms.discard(f'{name}:{key}'); self.mark_dirty(); self.refresh()

    def world(self):
        page,layout=self.page('세계관 · 기능','모든 캐릭터에 적용할 규칙과 기능을 설정하세요.')
        page.setProperty('draft_key','world')
        fields,content=self.scrolled_form(layout); data=copy.deepcopy(self.store.data['settings'])
        fields.add('world_name',data.get('world_name',''),'세계 이름')
        fields.add('world_description',data.get('world_description',''),'세계 소개',multiline=True)
        fields.add('common_rules',data.get('common_rules',[]),'세계 규칙 (한 줄에 하나)')
        common=fields.add('common_prompt',data.get('common_prompt',DEFAULT_COMMON_PROMPT),'최우선 공통 행동·대화 규칙',multiline=True)
        restore=QPushButton('공통 프롬프트 기본 규칙 복원'); fields.form.addRow('',restore)
        restore.clicked.connect(lambda:common.setPlainText(DEFAULT_COMMON_PROMPT))
        modes=Fields(fields.form)
        for scope,label in [('world','월드'),('rp','개인·공동 RP'),('dm','DM')]:
            combo=QComboBox()
            for key,title in [('inherit','세계 기본값 사용'),('scene','현장 RP'),('messenger','메신저 RP'),('custom','직접 설정')]:
                if scope!='world' or key!='inherit': combo.addItem(title,key)
            combo.setCurrentIndex(max(0,combo.findData(data.get('conversation_modes',{}).get(scope,'custom' if scope=='world' else 'inherit'))))
            fields.form.addRow(label+' 대화 방식',combo); modes.readers[scope]=combo.currentData
        fields.add('custom_conversation_rule',data.get('custom_conversation_rule',''),'직접 설정 대화 방식',multiline=True)
        fields.add('timezone',data.get('timezone','Asia/Seoul'),'생활 시간대')
        features=Fields(fields.form)
        for key,label,default in FEATURE_QUESTIONS: features.add(key,data.get('features',{}).get(key,default),label)
        channels=Fields(fields.form); channel_data=data.get('channel_setup',{})
        channels.add('create_categories',channel_data.get('create_categories',True),'장소 그룹별 카테고리 생성')
        channels.add('create_status_channel',channel_data.get('create_status_channel',True),'상태 채널 생성')
        category_note=QLabel('카테고리 이름은 왼쪽 ‘카테고리’ 탭에서 추가·수정하세요.'); category_note.setWordWrap(True); fields.form.addRow('',category_note)
        runtime=Fields(fields.form)
        for key,(label,default,minimum,maximum) in OPTIONS.items():
            widget=runtime.add(key,data.get('runtime',{}).get(key,default),label); widget.setRange(minimum,maximum)
        def save():
            values=fields.values(); ZoneInfo(values['timezone'])
            data=copy.deepcopy(self.store.data['settings'])
            data['runtime']=dict(data.get('runtime',{}),**validate_options(runtime.values()))
            data['conversation_modes']=modes.values()
            data.update(values); data['features']=dict(data.get('features',{}),**features.values())
            channel_values=channels.values()
            data['channel_setup']=dict(data.get('channel_setup',{}),**channel_values); self.store.write_json('settings',data); self.ok()
        self.savers['world']=save
        self.button(layout,'세계관 · 기능 저장',self.save_all).setToolTip('다른 탭의 수정 내용도 함께 저장합니다.'); return page

    def relations(self):
        page,layout=self.page('캐릭터 관계','시작 시점의 관계를 설정합니다. 이미 학습된 관계와 기억은 초기화하지 않습니다.')
        page.setProperty('draft_key','relations')
        keys=[k for k in self.store.data['characters'] if not k.startswith('_')]
        first=QComboBox(); first.addItems(keys); second=QComboBox(); second.addItems(keys)
        if len(keys)>1: second.setCurrentIndex(1)
        row=QHBoxLayout(); row.addWidget(first); row.addWidget(QLabel('↔')); row.addWidget(second); layout.addLayout(row)
        editor=ResizableTextEdit(height=300); editor.setPlaceholderText('두 캐릭터의 관계와 배경을 적어주세요.'); layout.addWidget(editor,1)
        current=[None]; drafts={}; loading=[False]
        first.setProperty('skip_dirty',True); second.setProperty('skip_dirty',True)
        def remember():
            if not loading[0] and current[0]: drafts[current[0]]=editor.toPlainText()
        editor.textChanged.connect(remember)
        def load():
            pair=first.currentText()+'|'+second.currentText(); reverse=second.currentText()+'|'+first.currentText()
            current[0]=pair if pair in drafts or pair in self.store.data['relations'] else reverse if reverse in drafts or reverse in self.store.data['relations'] else pair
            loading[0]=True
            editor.blockSignals(True)
            value=self.store.data['relations'].get(current[0],{})
            editor.setPlainText(drafts.get(current[0],value.get('context','') if isinstance(value,dict) else str(value)))
            editor.blockSignals(False); loading[0]=False
        first.currentTextChanged.connect(load); second.currentTextChanged.connect(load); load()
        def save():
            remember()
            data=copy.deepcopy(self.store.data['relations'])
            for pair,text in drafts.items():
                a,b=pair.split('|')
                if a==b:
                    if text.strip(): raise ValueError('서로 다른 두 캐릭터의 관계를 입력해주세요.')
                    continue
                if a not in self.store.data['characters'] or b not in self.store.data['characters']: continue
                old=data.get(pair,{})
                data[pair]=dict(old if isinstance(old,dict) else {},context=text)
            self.store.write_json('relations',data); self.ok()
        def remove():
            if current[0] in self.store.data['relations'] and self.confirm('초기 관계 설정을 삭제할까요?'):
                data=copy.deepcopy(self.store.data['relations']); data.pop(current[0]); drafts.pop(current[0],None)
                self.store.write_json('relations',data); load(); self.mark_dirty()
        self.savers['relations']=save
        self.button(layout,'관계 저장',self.save_all); self.button(layout,'초기 관계 삭제',remove); return page

    def connections(self):
        page,layout=self.page('API · Discord 연결','캐릭터별 토큰은 캐릭터 메뉴에서 입력하세요. 빈 값으로 저장하면 해당 연결 값이 제거됩니다.')
        page.setProperty('draft_key','connections')
        fields,content=self.scrolled_form(layout); secrets=[]
        for key,label in [('OPENAI_API_KEY','OpenAI API 키'),('DISCORD_GUILD_ID','본서버 ID'),('DISCORD_STATUS_TOKEN','상태봇 토큰'),('DISCORD_STATUS_CHANNEL','상태 채널 이름'),('DISCORD_COMMAND_CHANNEL_ID','명령 채널 ID'),('DISCORD_PERSONAL_RP_GUILD_IDS','개인 RP 서버 ID (쉼표 구분)')]:
            secret='TOKEN' in key or 'API_KEY' in key
            widget=fields.add(key,self.store.env.get(key,'') or '',label,secret=secret)
            if secret: secrets.append(widget)
        show=QCheckBox('키와 토큰 보기'); show.setProperty('skip_dirty',True); layout.addWidget(checkbox_row(show,'show_token'))
        show.toggled.connect(lambda checked:[widget.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password) for widget in secrets])
        def save():
            values=fields.values()
            for key in ('DISCORD_GUILD_ID','DISCORD_COMMAND_CHANNEL_ID'):
                if values[key] and not values[key].isdigit(): raise ValueError('Discord ID는 숫자만 입력해주세요.')
            if any(not v.strip().isdigit() for v in values['DISCORD_PERSONAL_RP_GUILD_IDS'].split(',') if v.strip()): raise ValueError('개인 서버 ID는 숫자를 쉼표로 구분해주세요.')
            self.store.write_env(values); self.ok()
        self.savers['connections']=save
        self.button(layout,'연결 설정 저장',self.save_all).setToolTip('다른 탭의 수정 내용도 함께 저장합니다.'); return page

    def backups(self):
        page,layout=self.page('백업 · 복원','설정·프롬프트·API 키를 백업합니다. 백업에는 비밀 키가 포함되므로 공유하지 마세요.\n복원은 백업 파일을 덮어씁니다. 백업 이후 추가된 파일과 대화 기록은 유지됩니다.')
        listing=QListWidget(); directory=self.root/'backup_setup'
        paths=sorted((p for p in directory.iterdir() if p.is_dir()),reverse=True) if directory.exists() else []
        listing.addItems([p.name for p in paths]); layout.addWidget(listing,1)
        def backup(): self.store.backup(); self.refresh()
        def restore():
            item=listing.currentItem()
            if item and self.confirm('선택한 시점의 설정과 프롬프트를 복원할까요? 저장하지 않은 수정 내용도 교체됩니다. 현재 저장된 설정은 먼저 백업합니다.'):
                if bot_running(self.root): raise ValueError('봇을 종료한 뒤 복원해주세요.')
                self.store.restore(directory/item.text()); self.store.pending.clear()
                self.savers.clear(); self.dirty_forms.clear()
                for index,page in list(self.page_cache.items()):
                    self.stack.removeWidget(page); page.deleteLater(); self.stack.insertWidget(index,QWidget())
                self.page_cache.clear(); self.dirty=False; self.refresh()
        self.button(layout,'지금 백업',backup); self.button(layout,'선택 백업 복원',restore); return page

    def preferences(self):
        path=self.root/'desktop_settings.json'
        raw=self.store.pending.get(path)
        return parse_json(raw.decode('utf-8') if raw is not None else path.read_text(encoding='utf-8'),'업데이트 설정') if raw is not None or path.exists() else {'check_updates_on_start':True}
    def updates(self):
        page,layout=self.page('업데이트',f'현재 버전 v{VERSION} · 새 버전은 GitHub 정식 릴리스에서 확인합니다.\n처음 GUI 버전으로 바꿀 때는 수동 다운로드가 필요합니다. 이후부터 여기서 업데이트할 수 있습니다.')
        check=QCheckBox('프로그램 시작 시 새 버전 확인'); check.setProperty('skip_dirty',True); check.setChecked(self.preferences().get('check_updates_on_start',True)); layout.addWidget(checkbox_row(check,'check_updates_on_start'))
        def preference():
            data=self.preferences(); data['check_updates_on_start']=check.isChecked()
            self.store.stage(self.root/'desktop_settings.json',json.dumps(data,ensure_ascii=False,indent=2)); self.mark_dirty()
        check.toggled.connect(lambda:self.guard(preference))
        self.update_text=QPlainTextEdit(); self.update_text.setReadOnly(True)
        self.update_text.setPlainText(self.release['version']+'\n\n'+self.release['notes'] if self.release else '업데이트 확인 버튼을 눌러주세요.')
        layout.addWidget(self.update_text,1)
        self.check_button=self.button(layout,'새 버전 확인',self.check_update)
        self.install_button=self.button(layout,'다운로드 · 업데이트',self.install_update)
        self.install_button.setEnabled(bool(self.release) and getattr(sys,'frozen',False) and os.name=='nt')
        return page
    def background(self,fn,done,quiet=False):
        if self.job and self.job.isRunning(): raise ValueError('작업이 진행 중입니다. 잠시 기다려주세요.')
        self.job=Job(fn,self); self.job.done.connect(done); self.job.failed.connect((lambda error:self.statusBar().showMessage('업데이트 확인을 마치지 못했습니다. 나중에 다시 확인해주세요.')) if quiet else self.error); self.job.start()
    def auto_check(self):
        self.guard(lambda:self.background(latest_release,self.auto_result,quiet=True) if self.preferences().get('check_updates_on_start',True) else None)
    def auto_result(self,release):
        self.release=release
        if release:
            self.statusBar().showMessage(f"새 버전 {release['version']}이 있습니다. 업데이트 메뉴에서 확인하세요.")
        if self.nav.currentRow()==7: self.refresh()
    def check_update(self):
        self.statusBar().showMessage('GitHub에서 새 버전을 확인하는 중…')
        def result(release):
            self.release=release
            if self.nav.currentRow()==7:
                self.update_text.setPlainText(release['version']+'\n\n'+release['notes'] if release else '최신 버전을 사용하고 있습니다.')
                self.install_button.setEnabled(bool(release) and getattr(sys,'frozen',False) and os.name=='nt')
            self.statusBar().showMessage('업데이트 확인 완료')
        self.background(latest_release,result)
    def install_update(self):
        if not self.release: return
        if bot_running(self.root): raise ValueError('실행 중인 봇을 콘솔에서 종료한 뒤 업데이트해주세요.')
        if not self.confirm(f"{self.release['version']}으로 업데이트할까요?\n설정 프로그램이 종료된 후 다시 열립니다. 설정·프롬프트·기억·대화 기록은 유지됩니다."): return
        if self.dirty: self.save_all()
        self.store.backup(); self.statusBar().showMessage('업데이트 다운로드 및 검증 중…')
        self.background(lambda:stage_release(self.release,self.root),self.install_ready)
    def install_ready(self,stage):
        try:
            launch_update(stage,self.root)
            self.dirty=False
            # Signal arrives before QThread finishes; wait briefly before quitting.
            QTimer.singleShot(100,self.finish_install)
        except Exception as error: self.error(error)
    def finish_install(self):
        if self.job and self.job.isRunning(): QTimer.singleShot(100,self.finish_install)
        else: self.close()
    def closeEvent(self,event):
        if self.job and self.job.isRunning():
            self.error('다운로드 또는 확인이 끝난 뒤 창을 닫아주세요.'); event.ignore(); return
        if self.dirty:
            dialog=QMessageBox(self); dialog.setWindowTitle('설정 저장')
            dialog.setText('변경한 내용을 저장할까요?')
            save=dialog.addButton('저장 후 종료',QMessageBox.ButtonRole.AcceptRole)
            discard=dialog.addButton('저장하지 않고 종료',QMessageBox.ButtonRole.DestructiveRole)
            dialog.addButton('취소',QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(save); dialog.exec()
            if dialog.clickedButton()==save:
                try: self.save_all()
                except Exception as error:
                    self.error(error); event.ignore(); return
            elif dialog.clickedButton()!=discard: event.ignore(); return
        if bot_running(self.root):
            dialog = QMessageBox(self)
            dialog.setWindowTitle('RPBot 종료')
            dialog.setText('봇이 실행 중입니다. 창을 어떻게 닫을까요?')
            stop = dialog.addButton('봇 종료 후 닫기', QMessageBox.ButtonRole.AcceptRole)
            tray = dialog.addButton('트레이로 최소화', QMessageBox.ButtonRole.ActionRole) if QSystemTrayIcon.isSystemTrayAvailable() else None
            dialog.addButton('취소', QMessageBox.ButtonRole.RejectRole)
            dialog.exec()
            if tray is not None and dialog.clickedButton() == tray:
                self.hide(); event.ignore(); return
            if dialog.clickedButton() != stop: event.ignore(); return
            self.stop_bot()
        self.tray.hide()
        event.accept()


def main():
    app=QApplication(sys.argv); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    lock=BotLock(app_root(),'.rpbot-settings.lock')
    if not lock.acquire():
        QMessageBox.information(None,'RPBot','설정 프로그램이 이미 열려 있습니다. 기존 창을 사용해주세요.'); return 1
    try: window=Window()
    except Exception as error:
        QMessageBox.critical(None,'RPBot 설정을 열 수 없습니다',str(error)+'\n원본 파일은 변경하지 않았습니다. 설정 파일을 확인해주세요.'); return 1
    if '--monitor' in sys.argv: window.nav.setCurrentRow(9)
    window.show(); return app.exec()


if __name__=='__main__': sys.exit(main())
