"""Administrator-only desktop inspection, audited correction and CSV export."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPlainTextEdit, QPushButton, QFileDialog, QMessageBox
from world_actions import WorldActions
from form_help import parse_json

TITLES={'food':'음식 재고','carried_food':'수령한 음식','ingredients':'식재료 재고','sink':'사용한 식기·예약','object':'오브젝트 속성','leisure':'여가 모임','request':'부탁·쪽지','item':'물건·분실물','thought':'속마음 (관리자 전용)','box':'식재료 상자'}

class WorldPanel(QWidget):
    def __init__(self, window):
        super().__init__(); self.window=window
        root=Path(window.store.env.get('BOT_DATA_DIR') or window.root)
        if not root.is_absolute(): root=window.root/root
        self.engine=WorldActions(root,window.store.env.get('DISCORD_GUILD_ID') or 'unconfigured',recover=False)
        self.selection=None; self.editing=False
        layout=QVBoxLayout(self)
        note=QLabel('관리자용 생활 자료 · 실제 상태와 변경 이력을 확인합니다. 선택 후 수정 버튼으로 편집하세요. 수정 내역은 별도 기록됩니다.\n음식·오브젝트·여가는 장소 탭에서 먼저 정의하고 세계관 · 기능에서 켜세요. 식재료 재고는 장소를 선택해 입력합니다.')
        note.setWordWrap(True);layout.addWidget(note)
        row=QHBoxLayout();layout.addLayout(row)
        self.kind=QComboBox()
        for key,title in TITLES.items(): self.kind.addItem(title,key)
        self.key=QComboBox();row.addWidget(self.kind);row.addWidget(self.key,1)
        self.view=QPlainTextEdit();self.view.setReadOnly(True);layout.addWidget(self.view,2)
        controls=QHBoxLayout();layout.addLayout(controls)
        for label,fn in [('수정',self.edit),('수정 저장',self.save),('편집 취소',self.cancel),('CSV 내보내기',self.export),('불확정 작업 확인',self.resolve_job)]:
            button=QPushButton(label);button.clicked.connect(lambda checked=False,fn=fn:window.guard(fn));controls.addWidget(button)
        self.logs=QPlainTextEdit();self.logs.setReadOnly(True);layout.addWidget(self.logs,2)
        self.kind.currentIndexChanged.connect(self.change);self.key.currentIndexChanged.connect(self.change)
        self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(2000);self.refresh()

    def refresh(self):
        if self.editing: return
        snapshot=self.engine.snapshot(self.window.store.data['settings'])
        kind=self.kind.currentData();states=self.engine.states(kind)
        keys=set(states)
        if kind in ('food','ingredients','sink'):
            keys.update(k for k in self.window.store.data['places'] if not k.startswith('_'))
        elif kind=='carried_food': keys.update(k for k in self.window.store.data['characters'] if not k.startswith('_'))
        elif kind=='object':
            keys.update(f'{p}:{n}' for p,d in self.window.store.data['places'].items() if isinstance(d,dict) for n in d.get('object_states',{}))
        selected=self.key.currentText();self.key.blockSignals(True);self.key.clear();self.key.addItems(sorted(keys))
        if selected in keys: self.key.setCurrentText(selected)
        self.key.blockSignals(False);self.selection=(kind,self.key.currentText())
        default=[] if kind in ('food','carried_food') else {'dirty':0,'reserved_by':'','until':0} if kind=='sink' else {}
        if kind=='object' and self.key.currentText() not in states:
            for place,definition in self.window.store.data['places'].items():
                for name,spec in definition.get('object_states',{}).items() if isinstance(definition,dict) else []:
                    if f'{place}:{name}'==self.key.currentText():default=spec.get('properties',{})
        self.view.setPlainText(json.dumps(states.get(self.key.currentText(),default),ensure_ascii=False,indent=2))
        zone=ZoneInfo(self.window.store.data['settings'].get('timezone','Asia/Seoul'))
        lines=[f"[{datetime.fromtimestamp(e['created'],zone).strftime('%Y-%m-%d %H:%M:%S')}] {e['place']} · {e['actor']} · {e['kind']}: {e['content']}" for e in snapshot['events']]
        jobs=[f"{j['kind']} · {j['actor']} · {j['phase']} · {j['error']}" for j in snapshot['jobs']]
        self.logs.setPlainText('작업 기록\n'+'\n'.join(jobs)+'\n\n생활 변경 기록\n'+'\n'.join(lines))

    def change(self):
        if not self.editing: self.refresh()
    def edit(self):
        if self.kind.currentData() in ('leisure','request','item','thought','box'): raise ValueError('여가 모임은 참여·종료 행동으로 변경하세요.')
        if not self.key.currentText(): return
        self.editing=True;self.view.setReadOnly(False);self.kind.setEnabled(False);self.key.setEnabled(False)
    def save(self, confirm=True):
        if not self.editing: return False
        if confirm and QMessageBox.question(self,'생활 자료 수정','선택한 실제 상태를 이 값으로 저장할까요? 기존 값과 수정 기록은 변경 이력에 남습니다.')!=QMessageBox.StandardButton.Yes:return False
        value=parse_json(self.view.toPlainText(),'생활 자료')
        kind,key=self.selection
        if kind=='object':
            allowed=None
            for place,definition in self.window.store.data['places'].items():
                for name,spec in definition.get('object_states',{}).items() if isinstance(definition,dict) else []:
                    if key==f'{place}:{name}':allowed=spec.get('properties',{})
            if allowed is None or not isinstance(value,dict) or set(value)!=set(allowed):raise ValueError('등록된 사물 속성만 수정하세요. 속성 추가·삭제는 장소 설정에서 하세요.')
            if any(not isinstance(v,(str,int,float,bool)) for v in value.values()):raise ValueError('속성 값은 문자·숫자·true/false로 입력하세요.')
        self.engine.correct(kind,key,value);self.cancel();return True
    def cancel(self):
        self.editing=False;self.view.setReadOnly(True);self.kind.setEnabled(True);self.key.setEnabled(True);self.refresh()
    def resolve_job(self):
        from PySide6.QtWidgets import QInputDialog
        jobs=[j for j in self.engine.snapshot(self.window.store.data['settings'])['jobs'] if j['phase'] in ('불확정','오류')]
        if not jobs:raise ValueError('확인할 오류·불확정 작업이 없습니다.')
        choice,ok=QInputDialog.getItem(self,'작업 확인','확인할 작업',[j['id']+' · '+j['phase'] for j in jobs],0,False)
        if not ok:return
        selected=jobs[next(i for i,j in enumerate(jobs) if choice.startswith(j['id']+' · '))]
        result,ok=QInputDialog.getItem(self,'전송 결과 확인','Discord 기록을 확인한 결과를 선택하세요. 자동 재전송하지 않습니다.',['전송 완료를 확인함','미전송·중단을 확인함'],0,False)
        if ok:self.engine.job_phase(selected['id'],'완료' if result.startswith('전송 완료') else '취소','관리자 확인');self.refresh()

    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'공개 월드 생활 기록 CSV 저장',str(self.window.root/'world_events.csv'),'CSV (*.csv)')
        if path:self.engine.export(path)
