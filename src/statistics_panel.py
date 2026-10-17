"""Minute samples and committed events only; graph gaps are intentionally visible."""
import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from PySide6.QtCore import QTimer, QPointF
from PySide6.QtGui import QPainter,QColor,QPen
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QTableWidget,QTableWidgetItem,QPushButton,QFileDialog
from world_actions import WorldActions

class NeedsGraph(QWidget):
    def __init__(self):
        super().__init__();self.samples=[];self.setMinimumHeight(200)
    def paintEvent(self,event):
        p=QPainter(self);p.fillRect(self.rect(),QColor('#1c2030'));p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor('#c3cadc'));p.drawText(12,22,'평균 허기 (주황) · 평균 피로 (보라) / 0~100')
        if not self.samples:p.drawText(12,55,'기록 없음 · 통계 수집 기능을 켜세요.');return
        groups={}
        for row in self.samples:groups.setdefault(row['minute'],[]).append(row)
        lo=min(groups);hi=max(groups);previous=None
        for minute,rows in sorted(groups.items()):
            values={field:sum(r[field] or 0 for r in rows)/len(rows) for field in ('hunger','fatigue')}
            x=20+(self.width()-40)*(minute-lo)/max(1,hi-lo)
            for field,color in [('hunger','#ffb25b'),('fatigue','#bca0ff')]:
                y=self.height()-20-(self.height()-60)*values[field]/100;p.setPen(QPen(QColor(color),2))
                point=QPointF(x,y);p.drawEllipse(point,2,2)
                if previous and minute-previous[0]==1 and len(rows)==previous[3]:p.drawLine(previous[1][field],point)
                values[field]=point
            previous=(minute,values,x,len(rows))

class StatisticsPanel(QWidget):
    def __init__(self,window):
        super().__init__();self.window=window
        root=Path(window.store.env.get('BOT_DATA_DIR') or window.root)
        if not root.is_absolute():root=window.root/root
        self.engine=WorldActions(root,window.store.env.get('DISCORD_GUILD_ID') or 'unconfigured',recover=False)
        layout=QVBoxLayout(self);row=QHBoxLayout();layout.addLayout(row)
        self.period=QComboBox()
        for h in (1,6,24):self.period.addItem(f'최근 {h}시간',h)
        self.period.setCurrentIndex(2);row.addWidget(self.period)
        self.actor=QComboBox();self.actor.addItem('전체 캐릭터','')
        for k,v in window.store.data['characters'].items():
            if not k.startswith('_'):self.actor.addItem(v.get('name',k),k)
        row.addWidget(self.actor);self.place=QComboBox();self.place.addItem('전체 장소','')
        for k in window.store.data['places']:
            if not k.startswith('_'):self.place.addItem(k,k)
        row.addWidget(self.place);button=QPushButton('선택 기간 CSV 내보내기');button.clicked.connect(lambda:window.guard(self.export));row.addWidget(button)
        self.summary=QLabel();layout.addWidget(self.summary);self.graph=NeedsGraph();layout.addWidget(self.graph,2)
        self.table=QTableWidget(0,2);self.table.setHorizontalHeaderLabels(['실제 기록 종류','건수']);layout.addWidget(self.table,1)
        note=QLabel('상태는 1분 간격, 최대 90일 보관합니다. 오프라인·누락 구간은 선으로 잇지 않습니다. 대화 수는 생성 초안이 아니라 공개 전송 기록입니다. 이 화면은 추가 AI를 호출하지 않습니다.')
        note.setWordWrap(True);layout.addWidget(note)
        for w in (self.period,self.actor,self.place):w.currentIndexChanged.connect(self.refresh)
        self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(30000);self.refresh()
    def refresh(self):
        self.data=self.engine.statistics(self.period.currentData(),self.actor.currentData(),self.place.currentData())
        samples=self.data['samples'];self.graph.samples=samples;self.graph.update()
        latest=max((s['minute'] for s in samples),default=0);rows=[s for s in samples if s['minute']==latest]
        self.summary.setText(f"현재 기록: {len(rows)}명 · 밤잠 {sum(s['sleeping'] and not s['nap'] for s in rows)} · 낮잠 {sum(s['nap'] for s in rows)} · 외출 {sum(s['away'] for s in rows)} · 연결 {sum(s['connected'] for s in rows)} · 샘플 {len(samples)}건")
        self.table.setRowCount(len(self.data['counts']))
        for i,event in enumerate(self.data['counts']):
            self.table.setItem(i,0,QTableWidgetItem(event['kind']));self.table.setItem(i,1,QTableWidgetItem(str(event['count'])))
    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'선택 상태 CSV',str(self.window.root/'life_statistics.csv'),'CSV (*.csv)')
        if not path:return
        zone=ZoneInfo(self.window.store.data['settings'].get('timezone','Asia/Seoul'))
        with Path(path).open('w',encoding='utf-8-sig',newline='') as out:
            writer=csv.writer(out);writer.writerow(['기록 시각 (생활 시간대)','캐릭터','장소','허기','피로','수면','낮잠','외출','연결'])
            for r in self.data['samples']:writer.writerow([datetime.fromtimestamp(r['minute']*60,zone).isoformat(),r['actor'],r['place'],r['hunger'],r['fatigue'],r['sleeping'],r['nap'],r['away'],r['connected']])
