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
from settings_store import Store, app_root, atomic_write
from app_version import VERSION
from runtime_options import OPTIONS, validate_options
from updater import latest_release, stage_release, launch_update, bot_running, BotLock
from setup_wizard import FEATURE_QUESTIONS
from monitor_panel import MonitorPanel

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
    default_outfit='', current_outfit='', sleep_start_range=[[1,0],[3,0]], wake_range=[[8,0],[10,0]], inventory=[], habits=[], goals=[], place_weights={}, restricted_places=[])
PLACE_DEFAULT = dict(channel_name='', description='', objects=[], group='public', parent=None,
    allowed_characters=[], time_multipliers=dict(morning=1.,day=1.,evening=1.,night=1.,late_night=1.))
LABELS = {'default_outfit':'기본 복장','current_outfit':'현재 복장 (비우면 기본 복장)','name':'이름','token_env':'토큰 환경변수 이름','prompt_file':'프롬프트 경로',
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


class Fields:
    """Typed form adapters; unknown keys are preserved by the caller."""
    def __init__(self, form):
        self.form = form
        self.readers = {}
    def add(self, key, value, label=None, secret=False, multiline=False):
        label = label or LABELS.get(key, key)
        if isinstance(value, bool):
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
            widget = QPlainTextEdit('\n'.join(map(str,value)) if isinstance(value,list) else str(value or ''))
            widget.setMinimumHeight(95); widget.setMaximumHeight(200)
            reader = (lambda: [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]) if isinstance(value,list) else widget.toPlainText
        else:
            widget = QLineEdit(str(value or ''))
            if secret:
                widget.setEchoMode(QLineEdit.EchoMode.Password)
                widget.setPlaceholderText('키 또는 토큰을 입력하세요')
            reader = widget.text
            if value is None: reader = lambda: widget.text().strip() or None
        self.form.addRow(label,widget)
        self.readers[key] = reader
        return widget
    def values(self):
        return {key:reader() for key,reader in self.readers.items()}


class Window(QMainWindow):
    def __init__(self, root=None):
        super().__init__()
        self.root = Path(root or app_root())
        self.store = Store(self.root)
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
        self.statusBar().showMessage('설정 저장 후 실행 중인 봇을 재시작하면 적용됩니다.')
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
        self.dirty=False
        self.statusBar().showMessage('저장 완료 · 이전 설정은 자동 백업했습니다. 봇을 재시작하면 적용됩니다.',10000)
    def confirm(self,text):
        return QMessageBox.question(self,'RPBot',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes
    def mark_dirty(self,*args): self.dirty=True
    def track(self,widget):
        for child in widget.findChildren(QWidget):
            if child.property('skip_dirty') or (isinstance(child,QPlainTextEdit) and child.isReadOnly()): continue
            for name in ('textEdited','textChanged','toggled','valueChanged','timeChanged'):
                signal=getattr(child,name,None)
                if signal is not None:
                    signal.connect(self.mark_dirty)
                    break
    def discard(self):
        return not self.dirty or self.confirm('저장하지 않은 변경 사항을 버릴까요?')
    def navigate(self,index):
        if index<0: return
        if not self.discard():
            self.nav.blockSignals(True); self.nav.setCurrentRow(self.stack.currentIndex()); self.nav.blockSignals(False); return
        self.dirty=False
        try:
            page=self.builders[index]()
        except Exception as error:
            self.error(error); return
        old=self.stack.widget(index); self.stack.removeWidget(old); old.deleteLater()
        self.stack.insertWidget(index,page); self.stack.setCurrentIndex(index)
        self.track(page)
    def refresh(self): self.dirty=False; self.navigate(self.nav.currentRow())
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
            self.store.write_json('settings', settings); self.refresh()
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
        def render(key):
            if not key: return
            while body_layout.count():
                item=body_layout.takeAt(0)
                if item.widget(): item.widget().deleteLater()
            existing=copy.deepcopy(self.store.data[name][key]); defaults=CHAR_DEFAULT if character else PLACE_DEFAULT
            fields,content=self.scrolled_form(body_layout)
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
                    fields.add(field,value,multiline=field in ('description','default_outfit','current_outfit'))
            token=None; prompt=None; avatar=None
            if character:
                token=fields.add('_token',self.store.env.get(existing.get('token_env',''),'') or '','Discord 봇 토큰',secret=True)
                show=QCheckBox('토큰 보기'); show.setProperty('skip_dirty',True); fields.form.addRow('',show)
                show.toggled.connect(lambda checked:token.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
                prompt_path=existing.get('prompt_file') or f'prompts/{key}.txt'
                path=self.store.safe_path(prompt_path)
                prompt=QPlainTextEdit(path.read_text(encoding='utf-8-sig') if path.exists() else '[정체성]\n이름: '+existing.get('name',key)+'\n\n[성격]\n\n[말투]\n')
                prompt.setMinimumHeight(240); fields.form.addRow('캐릭터 프롬프트',prompt)
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
                        destination=self.root/'profiles'/f'{key}.png'; destination.parent.mkdir(exist_ok=True)
                        self.store.backup()
                        if not pix.save(str(destination),'PNG'): raise ValueError('이미지 저장에 실패했습니다.')
                        existing['profile_image']=str(destination.relative_to(self.root))
                        avatar.setPixmap(pix.scaled(110,110,Qt.AspectRatioMode.KeepAspectRatio)); self.dirty=True
                pick.clicked.connect(lambda:self.guard(choose_image))
            def save():
                values=fields.values(); secret=values.pop('_token',None)
                if not str(values.get('name' if character else 'channel_name','')).strip(): raise ValueError('이름을 입력해주세요.')
                if character:
                    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',values['token_env']): raise ValueError('토큰 환경변수 이름을 확인해주세요.')
                    if values.get('private_room') and values['private_room'] not in self.store.data['places']:
                        raise ValueError('개인실을 장소 메뉴에 먼저 추가해주세요.')
                    for other_key,other in self.store.data['characters'].items():
                        if other_key!=key and not other_key.startswith('_') and other.get('token_env')==values['token_env']:
                            raise ValueError('다른 캐릭터가 사용하는 토큰 환경변수 이름입니다.')
                    self.store.safe_path(values['prompt_file'])
                    for restricted in values['restricted_places']:
                        if restricted not in self.store.data['places']: raise ValueError(f'없는 장소: {restricted}')
                    self.store.save_prompt(values['prompt_file'],prompt.toPlainText())
                    self.store.write_env({values['token_env']:secret})
                else:
                    if values.get('parent')==key: raise ValueError('상위 장소는 자기 자신일 수 없습니다.')
                    if values.get('parent') and values['parent'] not in self.store.data['places']: raise ValueError('상위 장소가 없습니다.')
                    seen={key}; ancestor=values.get('parent')
                    while ancestor:
                        if ancestor in seen: raise ValueError('상위 장소가 서로 순환하지 않도록 설정해주세요.')
                        seen.add(ancestor); ancestor=self.store.data['places'].get(ancestor,{}).get('parent')
                    for allowed in values['allowed_characters']:
                        if allowed not in self.store.data['characters']: raise ValueError(f'없는 캐릭터: {allowed}')
                data=copy.deepcopy(self.store.data[name]); data[key]=dict(existing,**values)
                self.store.write_json(name,data); self.ok()
            self.button(body_layout,'변경 사항 저장',save)
            self.track(content)
        def change(key):
            if not self.discard():
                selector.blockSignals(True); selector.setCurrentText(selected[0]); selector.blockSignals(False); return
            self.dirty=False; selected[0]=key; self.guard(lambda:render(key))
        selector.currentTextChanged.connect(change); render(selector.currentText()); return page

    def add_item(self,name):
        if not self.discard(): return
        key,accepted=QInputDialog.getText(self,'추가','캐릭터 내부 키 (영문/숫자/밑줄)' if name=='characters' else '장소 이름')
        key=key.strip()
        if not accepted: return
        if not key or key.startswith('_') or '|' in key or '/' in key or '\\' in key: raise ValueError('사용할 수 없는 이름입니다.')
        if name=='characters' and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',key): raise ValueError('캐릭터 내부 키는 영문으로 시작하고 영문/숫자/밑줄/하이픈을 사용해주세요.')
        if key in self.store.data[name]: raise ValueError('이미 존재합니다.')
        value=copy.deepcopy(CHAR_DEFAULT if name=='characters' else PLACE_DEFAULT)
        if name=='characters': value.update(name=key,token_env='DISCORD_'+key.upper().replace('-','_')+'_TOKEN',prompt_file=f'prompts/{key}.txt')
        else: value['channel_name']=key
        data=copy.deepcopy(self.store.data[name]); data[key]=value; self.store.write_json(name,data); self.refresh()
    def delete_item(self,name,key):
        if not key: return
        if not self.confirm(f'{key}을 삭제할까요?\n연결된 초기 관계와 출입 설정도 정리합니다. 대화 기록은 유지됩니다.'): return
        (self.store.remove_character if name=='characters' else self.store.remove_place)(key); self.refresh()

    def world(self):
        page,layout=self.page('세계관 · 기능','모든 캐릭터에 적용할 규칙과 기능을 설정하세요.')
        fields,content=self.scrolled_form(layout); data=copy.deepcopy(self.store.data['settings'])
        fields.add('world_name',data.get('world_name',''),'세계 이름')
        fields.add('world_description',data.get('world_description',''),'세계 소개',multiline=True)
        fields.add('common_rules',data.get('common_rules',[]),'공통 규칙 (한 줄에 하나)')
        fields.add('timezone',data.get('timezone','Asia/Seoul'),'생활 시간대')
        features=Fields(fields.form)
        for key,label,default in FEATURE_QUESTIONS: features.add(key,data.get('features',{}).get(key,default),label)
        channels=Fields(fields.form); channel_data=data.get('channel_setup',{})
        channels.add('create_categories',channel_data.get('create_categories',True),'장소 그룹별 카테고리 생성')
        channels.add('create_status_channel',channel_data.get('create_status_channel',True),'상태 채널 생성')
        channels.add('category_names',channel_data.get('category_names',{'public':'공용 장소','outdoor':'실외','private':'개인 공간','system':'시스템'}),'카테고리 이름')
        runtime=Fields(fields.form)
        for key,(label,default,minimum,maximum) in OPTIONS.items():
            widget=runtime.add(key,data.get('runtime',{}).get(key,default),label); widget.setRange(minimum,maximum)
        def save():
            values=fields.values(); ZoneInfo(values['timezone'])
            data['runtime']=dict(data.get('runtime',{}),**validate_options(runtime.values()))
            data.update(values); data['features']=dict(data.get('features',{}),**features.values())
            data['channel_setup']=dict(channel_data,**channels.values()); self.store.write_json('settings',data); self.ok()
        self.button(layout,'세계관 · 기능 저장',save); return page

    def relations(self):
        page,layout=self.page('캐릭터 관계','시작 시점의 관계를 설정합니다. 이미 학습된 관계와 기억은 초기화하지 않습니다.')
        keys=[k for k in self.store.data['characters'] if not k.startswith('_')]
        first=QComboBox(); first.addItems(keys); second=QComboBox(); second.addItems(keys)
        if len(keys)>1: second.setCurrentIndex(1)
        row=QHBoxLayout(); row.addWidget(first); row.addWidget(QLabel('↔')); row.addWidget(second); layout.addLayout(row)
        editor=QPlainTextEdit(); editor.setPlaceholderText('두 캐릭터의 관계와 배경을 적어주세요.'); layout.addWidget(editor,1)
        current=[None]
        def load():
            pair=first.currentText()+'|'+second.currentText(); reverse=second.currentText()+'|'+first.currentText()
            current[0]=pair if pair in self.store.data['relations'] else reverse if reverse in self.store.data['relations'] else pair
            editor.setPlainText(self.store.data['relations'].get(current[0],{}).get('context','')); self.dirty=False
        def change():
            if self.dirty:
                # Keep the draft visible; changing pair requires explicit discard.
                if not self.discard():
                    if current[0]:
                        a,b=current[0].split('|'); first.blockSignals(True); second.blockSignals(True); first.setCurrentText(a); second.setCurrentText(b); first.blockSignals(False); second.blockSignals(False)
                    return
            load()
        first.currentTextChanged.connect(change); second.currentTextChanged.connect(change); load()
        def save():
            if not first.currentText() or first.currentText()==second.currentText(): raise ValueError('서로 다른 두 캐릭터를 선택해주세요.')
            data=copy.deepcopy(self.store.data['relations']); data[current[0]]=dict(data.get(current[0],{}),context=editor.toPlainText())
            self.store.write_json('relations',data); self.ok()
        def remove():
            if current[0] in self.store.data['relations'] and self.confirm('초기 관계 설정을 삭제할까요?'):
                data=copy.deepcopy(self.store.data['relations']); data.pop(current[0]); self.store.write_json('relations',data); load()
        self.button(layout,'관계 저장',save); self.button(layout,'초기 관계 삭제',remove); return page

    def connections(self):
        page,layout=self.page('API · Discord 연결','캐릭터별 토큰은 캐릭터 메뉴에서 입력하세요. 빈 값으로 저장하면 해당 연결 값이 제거됩니다.')
        fields,content=self.scrolled_form(layout); secrets=[]
        for key,label in [('OPENAI_API_KEY','OpenAI API 키'),('DISCORD_GUILD_ID','본서버 ID'),('DISCORD_STATUS_TOKEN','상태봇 토큰'),('DISCORD_STATUS_CHANNEL','상태 채널 이름'),('DISCORD_COMMAND_CHANNEL_ID','명령 채널 ID'),('DISCORD_PERSONAL_RP_GUILD_IDS','개인 RP 서버 ID (쉼표 구분)')]:
            secret='TOKEN' in key or 'API_KEY' in key
            widget=fields.add(key,self.store.env.get(key,'') or '',label,secret=secret)
            if secret: secrets.append(widget)
        show=QCheckBox('키와 토큰 보기'); show.setProperty('skip_dirty',True); layout.addWidget(show)
        show.toggled.connect(lambda checked:[widget.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password) for widget in secrets])
        def save():
            values=fields.values()
            for key in ('DISCORD_GUILD_ID','DISCORD_COMMAND_CHANNEL_ID'):
                if values[key] and not values[key].isdigit(): raise ValueError('Discord ID는 숫자만 입력해주세요.')
            if any(not v.strip().isdigit() for v in values['DISCORD_PERSONAL_RP_GUILD_IDS'].split(',') if v.strip()): raise ValueError('개인 서버 ID는 숫자를 쉼표로 구분해주세요.')
            self.store.write_env(values); self.ok()
        self.button(layout,'연결 설정 저장',save); return page

    def backups(self):
        page,layout=self.page('백업 · 복원','설정·프롬프트·API 키를 백업합니다. 백업에는 비밀 키가 포함되므로 공유하지 마세요.\n복원은 백업 파일을 덮어씁니다. 백업 이후 추가된 파일과 대화 기록은 유지됩니다.')
        listing=QListWidget(); directory=self.root/'backup_setup'
        paths=sorted((p for p in directory.iterdir() if p.is_dir()),reverse=True) if directory.exists() else []
        listing.addItems([p.name for p in paths]); layout.addWidget(listing,1)
        def backup(): self.store.backup(); self.refresh()
        def restore():
            item=listing.currentItem()
            if item and self.confirm('선택한 시점의 설정과 프롬프트를 복원할까요? 현재 설정도 먼저 백업합니다.'):
                if bot_running(self.root): raise ValueError('봇을 종료한 뒤 복원해주세요.')
                self.store.restore(directory/item.text()); self.refresh()
        self.button(layout,'지금 백업',backup); self.button(layout,'선택 백업 복원',restore); return page

    def preferences(self):
        path=self.root/'desktop_settings.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'check_updates_on_start':True}
    def updates(self):
        page,layout=self.page('업데이트',f'현재 버전 v{VERSION} · 새 버전은 GitHub 정식 릴리스에서 확인합니다.\n처음 GUI 버전으로 바꿀 때는 수동 다운로드가 필요합니다. 이후부터 여기서 업데이트할 수 있습니다.')
        check=QCheckBox('프로그램 시작 시 새 버전 확인'); check.setProperty('skip_dirty',True); check.setChecked(self.preferences().get('check_updates_on_start',True)); layout.addWidget(check)
        def preference():
            data=self.preferences(); data['check_updates_on_start']=check.isChecked()
            atomic_write(self.root/'desktop_settings.json',json.dumps(data,ensure_ascii=False,indent=2)); self.dirty=False
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
        if not self.discard(): event.ignore(); return
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
