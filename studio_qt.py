"""Wave Studio 3: Qt/PyQtGraph UI; raw acquisition runs independently."""
import argparse, json, logging, os, subprocess, sys, time
from logging.handlers import RotatingFileHandler
from pathlib import Path
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W
import pyqtgraph as pg
from wave_core import Engine, NAMES
from guided_capture import Guide
import replay_labels
ROOT=Path(__file__).resolve().parent
COLORS=['#48d9ba','#6aafff','#eac267','#df8bbb','#ac9cef','#8bd39b','#ff9982','#6ed5e9','#d9ce8e']
MAX_LABEL_SPANS=16
LOG=logging.getLogger('wave')
pg.setConfigOptions(antialias=False,background='#101c2d',foreground='#9caec5')

def settings_path():
    """Workspace memory location. WAVE_STUDIO_SETTINGS overrides it (tests/portable use)."""
    override=os.environ.get('WAVE_STUDIO_SETTINGS')
    if override: return Path(override)
    base=os.environ.get('APPDATA')
    home=Path(base) if base else Path.home()/'AppData'/'Roaming'
    return home/'AirMouseWaveStudio'/'last_session.json'

class Window(W.QMainWindow):
    def __init__(self,args):
        super().__init__(); self.setWindowTitle('AirMouse Wave Studio 3'); self.resize(1260,880)
        self.config=dict(port=9002,emg=500,imu=200,subject='P01_wear01',output=str(ROOT/'recordings'))
        try: self.config.update(json.loads((ROOT/'studio_config.json').read_text(encoding='utf-8')))
        except (OSError,ValueError): pass
        if args.port is not None: self.config['port']=args.port
        self.transition=None; self.closing=False; self.frame_times=[]; self.last_stats=0.; self.last_error=0.; self.render_errors=0
        self.labels=[]; self.label_info={}; self._label_end=0.; self._label_static=False; self._label_dirty=False
        self._selected_label_index=None; self._syncing_table=False; self._edit_guard=False; self._paused_before_edit=False
        self._scrub_guard=False
        self._flash_text=''; self._flash_until=0.
        explicit=getattr(args,'replay',None); demo=getattr(args,'demo',False)
        # Auto-restore the previous replay workspace, but never override explicit launch flags.
        self._allow_persist_write=(os.environ.get('QT_QPA_PLATFORM')!='offscreen'
                                   or bool(os.environ.get('WAVE_STUDIO_SETTINGS'))) and not getattr(args,'compare',False)
        self.persist=not getattr(args,'compare',False)
        saved=self._load_session() if self.persist else {}
        self.restore=explicit; self._restore_note=''
        if not explicit and not demo and self.persist and saved.get('replay'):
            candidate=Path(saved['replay'])
            if candidate.is_dir() and (candidate/'metadata.json').is_file():
                self.restore=str(candidate)
                seen=saved.get('last_timeline')
                self._restore_note=f'已恢复上次回放：{candidate.name}'+(f'（上次看到 {seen:.1f}s，本次从头回放）' if isinstance(seen,(int,float)) else '')
            else:
                self._restore_note='上次回放目录已不存在，已回到等待设备；可在“设置与文件”里重新打开'
                self._forget_session()
        self.setStyleSheet('QMainWindow,QDialog{background:#0b1422;color:#deebf7;} QWidget{background:#0b1422;font-family:Microsoft YaHei UI;font-size:10pt;} QLabel{color:#deebf7;} QPushButton,QComboBox,QLineEdit,QSpinBox{background:#1c2c42;color:#e1edf9;border:1px solid #344b64;border-radius:5px;padding:7px;} QPushButton:hover{background:#2a405b;} QPushButton:disabled{color:#6f8094;background:#152235;} QPushButton#record{background:#136c60;} QCheckBox{color:#cad9e9;} QScrollArea{border:0;} QMenu{background:#1c2c42;color:#e1edf9;} QMenu::item:selected{background:#365675;} QTableWidget{background:#0f1b2b;color:#deebf7;gridline-color:#22364c;border:1px solid #22364c;} QHeaderView::section{background:#16253a;color:#9fc0dc;border:0;padding:3px;}')
        base=W.QWidget(); self.setCentralWidget(base); layout=W.QVBoxLayout(base); layout.setContentsMargins(20,14,20,10); layout.setSpacing(10)
        header=W.QHBoxLayout(); title=W.QLabel('AIR MOUSE  /  WAVE STUDIO'); title.setStyleSheet('font-size:19pt;font-weight:700;color:#53d9c0'); header.addWidget(title); header.addStretch()
        self.badge=W.QLabel('准备连接'); header.addWidget(self.badge); layout.addLayout(header)
        hint=W.QLabel('打开即等待开发板  ·  接收、绘图与文件保存独立运行'); hint.setStyleSheet('color:#8fa5bf'); layout.addWidget(hint)
        tools=W.QHBoxLayout(); self.record=W.QPushButton('● 开始保存'); self.record.setObjectName('record'); self.record.clicked.connect(self.toggle_record); tools.addWidget(self.record)
        self.label=W.QComboBox(); self.label.setEditable(True); self.label.addItems(['rest','index_click','middle_click','pinch','clutch_thumb_ring','clutch_three_open','free']); tools.addWidget(self.label)
        self.mark=W.QPushButton('标记动作  F5'); self.mark.clicked.connect(self.toggle_mark); tools.addWidget(self.mark)
        self.freeze=W.QPushButton('暂停显示'); self.freeze.setCheckable(True); self.freeze.toggled.connect(lambda v:self.freeze.setText('继续显示' if v else '暂停显示')); tools.addWidget(self.freeze); tools.addStretch()
        self.more=W.QPushButton('设置与文件 ▾'); menu=W.QMenu(self)
        menu.addAction('连接与保存设置…',self.settings); menu.addAction('打开数据目录',self.open_folder)
        menu.addSeparator(); menu.addAction('打开历史记录回放…',self.open_replay); menu.addAction('新建对比窗口…',self.open_compare); menu.addAction('回到真实设备',lambda:self.change_source('RK_TCP')); menu.addAction('模拟演示',lambda:self.change_source('SIMULATED'))
        menu.addSeparator(); menu.addAction('清除上次回放记忆',self.clear_session); menu.addAction('查看错误日志',lambda:QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(ROOT/'studio_v3.log')))); menu.addAction('安全退出',self.close)
        self.more.setMenu(menu); tools.addWidget(self.more); layout.addLayout(tools)
        view=W.QHBoxLayout(); self.group=W.QComboBox(); self.group.addItems(['肌电 · 9 通道','IMU · 6 通道']); view.addWidget(self.group)
        view.addWidget(W.QLabel('时间窗')); self.span=W.QComboBox(); self.span.addItems(['2','5','10','20']); self.span.setCurrentText('5'); view.addWidget(self.span)
        self.center=W.QCheckBox('显示去直流'); self.center.setChecked(True); view.addWidget(self.center); view.addWidget(W.QLabel('仅影响显示，CSV 保存原始值')); view.addStretch(); self.metrics=W.QLabel(); view.addWidget(self.metrics); layout.addLayout(view)
        if saved:
            if str(saved.get('span')) in [self.span.itemText(i) for i in range(self.span.count())]: self.span.setCurrentText(str(saved['span']))
            try: self.group.setCurrentIndex(int(saved.get('group',0))%self.group.count())
            except (TypeError,ValueError): pass
        if getattr(args,'span',None) is not None and self.span.findText(str(args.span))>=0: self.span.setCurrentText(str(args.span))
        if getattr(args,'group',None) is not None:
            try: self.group.setCurrentIndex(int(args.group)%self.group.count())
            except (TypeError,ValueError): pass
        self.info=W.QLabel('等待第一包数据…'); self.info.setWordWrap(True); layout.addWidget(self.info)
        self._build_label_bar(); layout.addWidget(self.labelbar)
        self.scroll=W.QScrollArea(); self.scroll.setWidgetResizable(True); layout.addWidget(self.scroll,1)
        self.build_plots(); self.group.currentIndexChanged.connect(self.build_plots)
        self._build_label_panel(); layout.addWidget(self.label_panel)
        self.ruler=W.QLabel(); self.ruler.setStyleSheet('color:#89a3bf;padding-left:135px'); layout.addWidget(self.ruler)
        self.footer=W.QLabel('时间轴为单调显示时间；不代表已验证的硬件同步。'); self.footer.setWordWrap(True); self.footer.setStyleSheet('color:#91a8c3'); layout.addWidget(self.footer)
        self.shortcut=QtGui.QShortcut(QtGui.QKeySequence('F5'),self); self.shortcut.activated.connect(self.toggle_mark)
        QtGui.QShortcut(QtGui.QKeySequence('Ctrl+S'),self,activated=self.save_label_edits)
        QtGui.QShortcut(QtGui.QKeySequence.Delete,self.table,activated=self.delete_selected_label)
        self.timer=QtCore.QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(33)
        source='REPLAY' if self.restore else 'SIMULATED' if demo else 'RK_TCP'
        self.start_engine(source,self.restore)
        if self._restore_note: self.flash(self._restore_note,12)
        self.guide=Guide(self)
        guide_button=W.QPushButton('引导采集'); tools.insertWidget(0,guide_button)
        guide_button.clicked.connect(self.guide.show)

    # ---------- label widgets ----------
    def _build_label_bar(self):
        self.labelbar=W.QWidget(); row=W.QHBoxLayout(self.labelbar); row.setContentsMargins(3,0,3,0); row.setSpacing(6)
        head=W.QLabel('动作标签'); head.setFixedWidth(126); head.setStyleSheet('font-size:9pt;color:#c9d7e6'); row.addWidget(head)
        self.label_plot=pg.PlotWidget(); self.label_plot.setMinimumHeight(52); self.label_plot.setMaximumHeight(52)
        self.label_plot.setMenuEnabled(False); self.label_plot.setMouseEnabled(x=False,y=False); self.label_plot.hideButtons()
        self.label_plot.hideAxis('left'); self.label_plot.hideAxis('bottom'); self.label_plot.setYRange(0,1,padding=0)
        self._label_regions=[]; self._label_texts=[]
        for _ in range(MAX_LABEL_SPANS):
            region=pg.LinearRegionItem(values=(0,0),orientation='vertical',brush=pg.mkBrush(255,255,255,20),pen=pg.mkPen('#ffffff'),movable=False)
            region.setZValue(5); region.hide(); region._label_index=None
            region.sigRegionChangeFinished.connect(lambda r=region:self._on_region_finished(r))
            self.label_plot.addItem(region); self._label_regions.append(region)
            text=pg.TextItem('',anchor=(0.5,0.5)); text.setZValue(6); text.hide(); self.label_plot.addItem(text); self._label_texts.append(text)
        self.label_plot.scene().sigMouseClicked.connect(self._on_label_click)
        row.addWidget(self.label_plot,1)
    def _build_label_panel(self):
        self.label_panel=W.QWidget(); box=W.QVBoxLayout(self.label_panel); box.setContentsMargins(0,0,0,2); box.setSpacing(4)
        head=W.QHBoxLayout(); self.label_title=W.QLabel('动作标签'); self.label_title.setStyleSheet('color:#53d9c0;font-weight:600')
        self.label_hint=W.QLabel(''); self.label_hint.setStyleSheet('color:#8fa5bf'); self.label_hint.setSizePolicy(W.QSizePolicy.Ignored,W.QSizePolicy.Preferred); self.label_hint.setMinimumWidth(0)
        head.addWidget(self.label_title); head.addWidget(self.label_hint,1)
        self.pause_replay=W.QPushButton('暂停回放'); self.pause_replay.setCheckable(True); self.pause_replay.toggled.connect(self._toggle_pause_replay)
        self.edit_labels=W.QCheckBox('编辑标签'); self.edit_labels.toggled.connect(self._toggle_label_edit)
        head.addWidget(self.pause_replay); head.addWidget(self.edit_labels); box.addLayout(head)
        self.table=W.QTableWidget(0,6); self.table.setHorizontalHeaderLabels(['#','开始 s','结束 s','时长 s','类别','来源'])
        self.table.verticalHeader().setVisible(False); self.table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(W.QAbstractItemView.SingleSelection); self.table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        header=self.table.horizontalHeader(); header.setStretchLastSection(False)
        header.setSectionResizeMode(4,W.QHeaderView.Stretch); header.setSectionResizeMode(5,W.QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(72); self.table.setMaximumHeight(150)
        for column,width in ((0,34),(1,70),(2,70),(3,64),(4,190)): self.table.setColumnWidth(column,width)
        self.table.itemSelectionChanged.connect(self._on_label_selection); self.table.itemChanged.connect(self._on_label_item_changed)
        box.addWidget(self.table)
        row=W.QHBoxLayout(); self.add_label_button=W.QPushButton('新建区间'); self.add_label_button.clicked.connect(self.add_label_at_view)
        self.del_label_button=W.QPushButton('删除选中'); self.del_label_button.clicked.connect(self.delete_selected_label)
        self.save_labels_button=W.QPushButton('保存标签'); self.save_labels_button.clicked.connect(self.save_label_edits)
        self.reload_labels_button=W.QPushButton('重新载入'); self.reload_labels_button.clicked.connect(self.reload_labels)
        for button in (self.add_label_button,self.del_label_button,self.save_labels_button,self.reload_labels_button): row.addWidget(button)
        row.addStretch(); self.label_tip=W.QLabel('拖动色块边界改时间 · 双击标签条空白新建 · Ctrl+S 保存')
        self.label_tip.setStyleSheet('color:#8fa5bf'); self.label_tip.setSizePolicy(W.QSizePolicy.Ignored,W.QSizePolicy.Preferred); self.label_tip.setMinimumWidth(0)
        row.addWidget(self.label_tip)
        box.addLayout(row)
        # 回放定位：拖到任意时刻；点标签表格的行也会跳转（见 _on_label_selection）
        scrub_row=W.QHBoxLayout(); self.scrub_caption=W.QLabel('定位'); self.scrub_caption.setStyleSheet('color:#8fa5bf')
        self.scrub=W.QSlider(QtCore.Qt.Horizontal); self.scrub.setRange(0,1000); self.scrub.setEnabled(False)
        self.scrub.setToolTip('拖动定位到任意时刻；点表格某一行跳到该动作')
        self.scrub.valueChanged.connect(self._on_scrub)
        self.scrub_pos=W.QLabel('0.0 / 0.0 s'); self.scrub_pos.setStyleSheet('color:#8fa5bf')
        scrub_row.addWidget(self.scrub_caption); scrub_row.addWidget(self.scrub,1); scrub_row.addWidget(self.scrub_pos)
        box.addLayout(scrub_row)
        self.labelbar.setVisible(False); self.label_panel.setVisible(False)
    def _load_labels(self,folder):
        self._label_dirty=False; self._selected_label_index=None
        if folder:
            try: self.labels,self.label_info=replay_labels.load(folder)
            except Exception as exc:
                LOG.exception('label load failed'); self.labels=[]; self.label_info={'warnings':['读取动作标签失败：'+str(exc)],'n':0,'truth':None}
        else: self.labels=[]; self.label_info={}
        self._refresh_label_table(); self._update_label_header()
        warnings=self.label_info.get('warnings') or []
        if warnings: self.flash(warnings[0],10)
        self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
    def _refresh_label_table(self):
        self._syncing_table=True
        try:
            self.table.setRowCount(len(self.labels))
            for row,item in enumerate(self.labels):
                color=QtGui.QColor(replay_labels.color_for(item['label']))
                source={'manual':'手打','guide':'引导'}.get(item.get('source'),'编辑')
                values=[str(row+1),f"{item['start']:.2f}",f"{item['end']:.2f}",f"{item['end']-item['start']:.2f}",item['label'],source]
                for column,text in enumerate(values):
                    cell=self.table.item(row,column)
                    if cell is None: cell=W.QTableWidgetItem(); self.table.setItem(row,column,cell)
                    cell.setText(text)
                    flags=QtCore.Qt.ItemIsEnabled|QtCore.Qt.ItemIsSelectable
                    if column==4: flags|=QtCore.Qt.ItemIsEditable
                    cell.setFlags(flags)
                    cell.setForeground(color if column in (4,5) else QtGui.QBrush(QtGui.QColor('#deebf7')))
                self.table.setRowHeight(row,20)
        finally: self._syncing_table=False
    def _update_label_header(self):
        if not self.label_info:
            self.label_title.setText('动作标签'); self.label_hint.setText(''); return
        truth='events.edited.csv（编辑后真值）' if self.label_info.get('edited') else 'events.csv（原始）'
        self.label_title.setText(f"动作标签 {len(self.labels)} 段 · {truth}"+(' · 未保存*' if self._label_dirty else ''))
        counts={}
        for item in self.labels: counts[item['label']]=counts.get(item['label'],0)+1
        parts='  '.join(f'{name}×{count}' for name,count in sorted(counts.items()))
        self.label_hint.setText(('类别：'+parts+'   ' if parts else '')+'来源：手打=events start/end；引导=guide JSON phase 1')
    def _sync_labels(self,end,span,force=False):
        if not self.labelbar.isVisible(): return
        if self._label_static and not force: return
        visible=[]
        for index,item in enumerate(self.labels):
            if item['end']>end-span-.001 and item['start']<end+.001: visible.append((index,item))
        self._syncing_labels=True
        try:
            for slot,region in enumerate(self._label_regions):
                text=self._label_texts[slot]
                if slot>=len(visible):
                    region.hide(); text.hide(); region._label_index=None; continue
                index,item=visible[slot]; color=replay_labels.color_for(item['label'])
                left=max(item['start']-end,-span); right=min(item['end']-end,0.0)
                region.setRegion((left,right)); brush=QtGui.QColor(color); brush.setAlpha(70)
                region.setBrush(pg.mkBrush(brush))
                pen=pg.mkPen(color,width=2 if index==self._selected_label_index else 1)
                for edge in region.lines: edge.setPen(pen)
                region.setMovable(self._label_static); region.show(); region._label_index=index; region._sync_end=end
                text.setText(item['label']); text.setColor(color); text.setPos((left+right)/2,.5)
                text.setVisible((right-left)>span*.06)
        finally: self._syncing_labels=False
        self._sync_channel_labels(visible,end,span)
    def _sync_channel_labels(self,visible,end,span):
        for lines in getattr(self,'label_lines',[]):
            used=min(len(visible),len(lines)//2)
            for slot in range(len(lines)//2):
                if slot>=used:
                    lines[2*slot].hide(); lines[2*slot+1].hide(); continue
                index,item=visible[slot]; color=replay_labels.color_for(item['label'])
                for line,value in ((lines[2*slot],item['start']-end),(lines[2*slot+1],item['end']-end)):
                    line.setPen(pg.mkPen(color,width=1,style=QtCore.Qt.DashLine)); line.setPos(max(-span,min(0.,value))); line.show()
    def _on_label_click(self,event):
        if not self.labelbar.isVisible() or not event.double(): return
        if not self._label_static:
            self.edit_labels.setChecked(True)
            if not self._label_static: return
        self._create_label_at(self.label_plot.getViewBox().mapSceneToView(event.scenePos()).x()+self._label_end)
    def _on_region_finished(self,region):
        if not self._label_static or getattr(self,'_syncing_labels',False): return
        index=getattr(region,'_label_index',None)
        if index is None or index>=len(self.labels): return
        left,right=region.getRegion(); end=getattr(region,'_sync_end',self._label_end)
        start=max(0.,min(left,right)+end); stop=max(left,right)+end
        total=self.label_info.get('total_s')
        if total: stop=min(stop,float(total))
        if stop-start<.05: self.flash('区间太短（<50 ms），已恢复原值'); self._sync_labels(end,float(self.span.currentText()),force=True); return
        self.labels[index]['start']=start; self.labels[index]['end']=stop; self._label_dirty=True
        self._refresh_label_table()
        self._syncing_table=True
        try:
            self.table.selectRow(index); self._selected_label_index=index
        finally: self._syncing_table=False
        self._update_label_header(); self.flash(f"已移动区间：{self.labels[index]['label']} {start:.2f}–{stop:.2f}s（未保存）")
    def _on_label_selection(self):
        if self._syncing_table: return
        self._selected_label_index=self.table.currentRow() if self.table.currentRow()>=0 else None
        if not self._label_static: self._sync_labels(self._label_end,float(self.span.currentText()))
        index=self._selected_label_index
        if index is not None and index<len(self.labels) and not self.edit_labels.isChecked():
            self._jump_to(self.labels[index])
    def _on_label_item_changed(self,cell):
        if self._syncing_table or cell.column()!=4: return
        row=cell.row()
        if row<0 or row>=len(self.labels): return
        label=cell.text().strip() or self.labels[row]['label']
        if label==self.labels[row]['label']: return
        self.labels[row]['label']=label; self._label_dirty=True
        self._syncing_table=True
        try: cell.setForeground(QtGui.QBrush(QtGui.QColor(replay_labels.color_for(label))))
        finally: self._syncing_table=False
        self._update_label_header(); self.flash(f'已改类别：{label}（未保存）')
    def _create_label_at(self,when):
        label=self.label.currentText().strip() or 'unlabeled'; total=self.label_info.get('total_s')
        start=max(0.,when-.5); stop=when+.5
        if total: stop=min(stop,float(total)); start=min(start,max(0.,float(total)-.1))
        if stop-start<.1: self.flash('此处无法新建区间（超出记录时间轴）'); return
        self.labels.append(dict(label=label,start=start,end=stop,source='edited'))
        self.labels.sort(key=lambda item:(item['start'],item['end'],item['label']))
        self._label_dirty=True; self._refresh_label_table(); self._update_label_header()
        self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
        index=next(i for i,item in enumerate(self.labels) if item['start']==start and item['label']==label)
        self.table.selectRow(index); self._selected_label_index=index
        self.flash(f'已新建区间 {label} {start:.2f}–{stop:.2f}s（未保存，Ctrl+S 保存）')
    def add_label_at_view(self):
        if not self.labelbar.isVisible(): self.flash('只有回放视图可以编辑标签'); return
        if not self._label_static: self.edit_labels.setChecked(True)
        if not self._label_static: return
        self._create_label_at(self._label_end-float(self.span.currentText())/2)
    def delete_selected_label(self):
        row=self.table.currentRow()
        if row<0 or row>=len(self.labels): self.flash('先在下方清单里选中一行，再删除'); return
        removed=self.labels.pop(row); self._label_dirty=True; self._selected_label_index=None
        self._refresh_label_table(); self._update_label_header()
        self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
        self.flash(f"已删除区间 {removed['label']} {removed['start']:.2f}–{removed['end']:.2f}s（未保存）")
    def _toggle_label_edit(self,checked):
        if self._edit_guard: return
        self._edit_guard=True
        timer=getattr(self,'_freeze_edit_timer',None)
        try:
            if checked:
                if not self.labelbar.isVisible():
                    self.flash('只有回放视图可以编辑标签'); self.edit_labels.setChecked(False); return
                self._paused_before_edit=self.pause_replay.isChecked()
                self.pause_replay.setChecked(True)
                if self.engine: self.engine.pause.set()
                self.table.setEditTriggers(W.QAbstractItemView.DoubleClicked|W.QAbstractItemView.EditKeyPressed)
                # The replay thread may deliver one more block after the pause request; wait
                # for the display clock to settle before freezing the label geometry.
                self._label_static=False
                for region in self._label_regions: region.setMovable(False)
                timer=QtCore.QTimer(self); timer.setSingleShot(True); timer.timeout.connect(self._freeze_labels_for_edit)
                self._freeze_edit_timer=timer; timer.start(300)
                self.flash('编辑模式：正在暂停回放…')
            else:
                if timer: timer.stop()
                self._label_static=False
                for region in self._label_regions: region.setMovable(False)
                self.table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
                if not self._paused_before_edit:
                    self.pause_replay.setChecked(False)
                    if self.engine:
                        self.engine.follow_latest()
                        self.engine.pause.clear()
                self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
        finally: self._edit_guard=False
    def _freeze_labels_for_edit(self):
        if not self.edit_labels.isChecked(): return
        self._label_static=True
        for region in self._label_regions: region.setMovable(True)
        self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
        self.flash('编辑模式：回放已暂停。拖动色块边界改时间，双击空白处新建，改完点“保存标签”。')
    def _toggle_pause_replay(self,checked):
        if self._edit_guard: return
        engine=getattr(self,'engine',None)
        if engine:
            if checked: engine.pause.set()
            else:
                if self.edit_labels.isChecked():
                    self._edit_guard=True
                    try: self.pause_replay.setChecked(True)
                    finally: self._edit_guard=False
                    self.flash('编辑标签时回放保持暂停；先取消“编辑标签”再继续回放'); return
                engine.follow_latest()      # 继续回放时回到流位置
                engine.pause.clear()
        self.flash('回放已暂停：时间轴静止，可编辑标签' if checked else '回放继续')
    def _replay_bounds(self):
        engine=getattr(self,'engine',None)
        if not engine or getattr(engine,'source',None)!='REPLAY': return None
        return engine.bounds()

    def _update_scrub_text(self):
        b=self._replay_bounds()
        if not b:
            self.scrub.setEnabled(False); self.scrub_pos.setText('—'); return
        lo,hi=b; dur=max(hi-lo,1e-6)
        pos=self.engine.view_end if self.engine.view_end is not None else hi
        self.scrub.setEnabled(True)
        self._scrub_guard=True
        try: self.scrub.setValue(int(round((pos-lo)/dur*1000)))
        finally: self._scrub_guard=False
        self.scrub_pos.setText(f'{pos-lo:.1f} / {dur:.1f} s')

    def _on_scrub(self,value):
        if self._scrub_guard: return
        b=self._replay_bounds()
        if not b: return
        lo,hi=b; self._scrub_guard=True
        try:
            if not self.pause_replay.isChecked(): self.pause_replay.setChecked(True)
            self.engine.seek(lo+(hi-lo)*value/1000.0)
            self.render_frame()
            self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
        except Exception: LOG.exception('scrub failed')
        finally: self._scrub_guard=False
        self._update_scrub_text()

    def _jump_to(self,item):
        b=self._replay_bounds()
        if not b: return
        lo,hi=b; span=float(self.span.currentText())
        mid=(float(item['start'])+float(item['end']))/2.0
        target=min(max(mid+span/2.0,lo),hi)     # 让区间落在视窗中间
        self._scrub_guard=True
        try:
            if not self.pause_replay.isChecked(): self.pause_replay.setChecked(True)
            self.engine.seek(target); self.render_frame()
            self._sync_labels(self._label_end,span,force=True)
        except Exception: LOG.exception('jump failed'); return
        finally: self._scrub_guard=False
        self._update_scrub_text(); self.flash(f"已定位：{item['label']} @ {mid:.2f}s")

    def save_label_edits(self):
        if not self.labelbar.isVisible(): self.flash('只有回放视图有标签可保存'); return
        folder=getattr(self.engine,'replay',None)
        if not folder: self.flash('当前不是回放，没有可保存的标签'); return
        if not self._label_dirty:
            self.flash('没有未保存的修改'); return
        try: edited,backup=replay_labels.save(folder,self.labels)
        except Exception as exc:
            LOG.exception('label save failed'); self.flash('保存标签失败：'+str(exc),12); return
        self.labels,self.label_info=replay_labels.load(folder)
        self._label_dirty=False; self._selected_label_index=None
        self._refresh_label_table(); self._update_label_header()
        self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
        self.flash(f'已保存标签 → {edited.name}（真值）'+ (f'；原 events.csv 备份 → {backup.name}' if backup else ''),14)
    def reload_labels(self):
        folder=getattr(self.engine,'replay',None)
        if not folder: return
        self._load_labels(folder); self.flash('已重新载入标签')
    def clear_session(self):
        self._forget_session(); self.flash('已清除上次回放记忆')
    def _forget_session(self):
        if not self._allow_persist_write: return
        data=self._load_session(); data['replay']=None; data.pop('source',None)
        try:
            path=settings_path(); path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError: LOG.exception('session forget failed')
    # ---------- main view ----------
    def build_plots(self,*unused):
        old=self.scroll.takeWidget()
        if old: old.deleteLater()
        content=W.QWidget(); lay=W.QVBoxLayout(content); lay.setContentsMargins(0,0,0,0); lay.setSpacing(3)
        self.curves=[]; self.plots=[]; self.channel_labels=[]; self.guides=[]; self.label_lines=[]; self.kind=16 if self.group.currentIndex()==0 else 17
        for i,name in enumerate(NAMES[self.kind]):
            frame=W.QWidget(); row=W.QHBoxLayout(frame); row.setContentsMargins(3,0,3,0)
            label=W.QLabel(name); label.setFixedWidth(126); label.setStyleSheet('font-size:9pt;color:'+COLORS[i]); label.setSizePolicy(W.QSizePolicy.Fixed,W.QSizePolicy.Ignored); row.addWidget(label)
            plot=pg.PlotWidget(); plot.setMinimumHeight(32); plot.setSizePolicy(W.QSizePolicy.Expanding,W.QSizePolicy.Ignored); plot.setMenuEnabled(False); plot.setMouseEnabled(x=False,y=False); plot.hideButtons(); plot.hideAxis('bottom'); plot.hideAxis('left')
            guides=[]
            for j in range(6):
                line=pg.InfiniteLine(angle=90,pen=pg.mkPen('#24384d')); plot.addItem(line); guides.append(line)
            plot.addItem(pg.InfiniteLine(pos=0,angle=0,pen=pg.mkPen('#344b63'))); self.guides.append(guides)
            lines=[]
            for _ in range(MAX_LABEL_SPANS*2):
                line=pg.InfiniteLine(angle=90,movable=False,pen=pg.mkPen('#ffffff')); line.setZValue(4); line.hide(); plot.addItem(line); lines.append(line)
            self.label_lines.append(lines)
            curve=plot.plot(pen=pg.mkPen(COLORS[i],width=1),connect='finite'); curve.setClipToView(True); curve.setDownsampling(auto=True,method='peak')
            row.addWidget(plot,1); lay.addWidget(frame,1); self.curves.append(curve); self.plots.append(plot); self.channel_labels.append(label)
        self.scroll.setWidget(content)
        self._sync_labels(self._label_end,float(self.span.currentText()),force=True)
    def start_engine(self,source,replay=None):
        self.engine=Engine(port=self.config['port'],rates={16:self.config['emg'],17:self.config['imu']},source=source,replay=replay)
        is_replay=source=='REPLAY'
        self.labelbar.setVisible(is_replay); self.label_panel.setVisible(is_replay)
        self.pause_replay.setEnabled(is_replay); self.edit_labels.setEnabled(is_replay)
        self.save_labels_button.setEnabled(is_replay); self.add_label_button.setEnabled(is_replay)
        self.del_label_button.setEnabled(is_replay); self.reload_labels_button.setEnabled(is_replay)
        if not is_replay and self.edit_labels.isChecked(): self.edit_labels.setChecked(False)
        if self.freeze.isChecked(): self.freeze.setChecked(False)
        self._load_labels(replay if is_replay else None)
        self.engine.start()
        if self.pause_replay.isChecked(): self.engine.pause.set()
        if is_replay and self._allow_persist_write and replay: self._save_session(source,replay)
    def change_source(self,source,replay=None):
        if self.engine.recorder or any(w.thread.is_alive() for w in self.engine.writers):
            W.QMessageBox.information(self,'先结束保存','先停止保存，等待文件写完，再切换数据源。'); return
        self.transition=(source,replay); self.engine.shutdown(); self.more.setEnabled(False)
    def settings(self):
        dialog=W.QDialog(self); dialog.setWindowTitle('连接与保存'); form=W.QFormLayout(dialog); fields={}
        for key,label in [('port','本机监听端口'),('emg','EMG Hz（须匹配板子）'),('imu','IMU Hz（须匹配板子）')]:
            field=W.QSpinBox(); field.setRange(1,65535 if key=='port' else 10000); field.setValue(self.config[key]); fields[key]=field; form.addRow(label,field)
        for key,label in [('subject','受试者 / 佩戴编号'),('output','保存目录')]:
            field=W.QLineEdit(str(self.config[key])); fields[key]=field; form.addRow(label,field)
        form.addRow(W.QLabel('使用已有 SSH 隧道；这里不会修改板子采样率。'))
        buttons=W.QDialogButtonBox(W.QDialogButtonBox.Ok|W.QDialogButtonBox.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec()!=W.QDialog.Accepted: return
        if self.engine.recorder or any(w.thread.is_alive() for w in self.engine.writers):
            W.QMessageBox.information(self,'保存中','请先结束保存，再更改设置。'); return
        try:
            updated={k:v.value() if isinstance(v,W.QSpinBox) else v.text().strip() for k,v in fields.items()}
            if not updated['output']: raise ValueError('保存目录不能为空')
            self.config.update(updated); (ROOT/'studio_config.json').write_text(json.dumps(self.config,ensure_ascii=False,indent=2),encoding='utf-8'); self.change_source('RK_TCP')
        except Exception as exc: W.QMessageBox.warning(self,'设置失败',str(exc))
    def open_folder(self): QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.config['output']))
    def open_replay(self):
        folder=W.QFileDialog.getExistingDirectory(self,'选择含 metadata.json 的历史目录',self.config['output'])
        if folder: self.change_source('REPLAY',folder)
    def open_compare(self):
        folder=getattr(self.engine,'replay',None)
        if self.engine.source!='REPLAY' or not folder:
            W.QMessageBox.information(self,'需要回放','先打开一个历史记录回放，再开对比窗口。'); return
        groups=['肌电 · 9 通道','IMU · 6 通道']
        choice,ok=W.QInputDialog.getItem(self,'新建对比窗口','新窗口显示的通道组：',groups,0,False)
        if not ok: return
        spans=[self.span.itemText(i) for i in range(self.span.count())]
        span,ok=W.QInputDialog.getItem(self,'新建对比窗口','新窗口时间窗（秒）：',spans,2,False)
        if not ok: return
        command=[sys.executable,str(ROOT/'studio_qt.py'),'--replay',str(folder),'--compare','--group',str(groups.index(choice)),'--span',span]
        try:
            with open(ROOT/'compare_windows.log','a',encoding='utf-8') as log:
                subprocess.Popen(command,cwd=str(ROOT),stdout=log,stderr=log,close_fds=True)
            self.flash(f'已启动对比窗口：{choice} · {span}s 时间窗（独立进程，可再开一个）',10)
        except Exception as exc:
            LOG.exception('compare window failed'); W.QMessageBox.warning(self,'无法启动对比窗口',str(exc))
    def toggle_record(self):
        if self.guide.active:
            self.guide.finish('user_stop'); return
        try:
            if self.engine.recorder: self.engine.end_record()
            else: self.engine.start_record(self.config['output'],self.config['subject'])
        except Exception as exc: W.QMessageBox.warning(self,'无法保存',str(exc)); LOG.exception('record toggle failed')
    def toggle_mark(self):
        if not self.guide.active: self.engine.mark(self.label.currentText())
    def flash(self,text,seconds=8):
        self._flash_text=text; self._flash_until=time.monotonic()+seconds
        if hasattr(self,'footer'): self.footer.setText(text)
    def tick(self):
        started=time.perf_counter()
        try:
            if self.closing:
                if not self.engine.busy(): self.timer.stop(); self.close(); return
                self.info.setText('正在安全退出：等待接收停止、数据写完…'); return
            if self.transition:
                if not self.engine.busy():
                    source,replay=self.transition; self.transition=None; self.start_engine(source,replay); self.more.setEnabled(True)
                return
            e=self.engine; now=time.monotonic()
            if now-self.last_stats>.25:
                self.last_stats=now
                with e.lock:
                    counts=dict(e.counts); recent={k:list(v) for k,v in e.recent.items()}; errors=e.bad; adjust=e.adjustments; rec=e.recorder
                age=now-e.last_receive if e.last_receive else float('inf')
                self.badge.setText('历史回放' if e.source=='REPLAY' else '模拟演示' if e.source=='SIMULATED' else '● 正在接收' if e.connected and age<2 else '● 等待 / 无新数据')
                self.badge.setStyleSheet('color:'+('#4ed7b5' if e.connected and age<2 else '#f2c26a'))
                rates={}
                for k,q in recent.items():
                    q=[r for r in q if r[0]>now-3]
                    rates[k]=sum(n for _,n in q[1:])/(q[-1][0]-q[0][0]) if len(q)>1 and q[-1][0]>q[0][0] else 0
                paused_replay=e.source=='REPLAY' and self.pause_replay.isChecked()
                self.info.setText(f'{e.status}{" · 已暂停" if paused_replay else ""}  |  EMG {counts[16]:,} · {rates[16]:.0f} 点/s    IMU {counts[17]:,} · {rates[17]:.0f} 点/s  |  协议异常 {errors}  时间调整 {adjust}（非丢包计数）')
                span=float(self.span.currentText())
                pos=e.view_end if e.view_end is not None else e.latest
                lead='距最新数据' if e.view_end is None else '窗口（定位中）'
                if not self.freeze.isChecked(): self.ruler.setText(lead+'：  '+'     │     '.join(f'−{span*(1-i/5):g}s' if i<5 else '最新' for i in range(6))+f'      显示时刻 {pos:.2f}s')
                self._update_scrub_text()
                saving=any(w.thread.is_alive() for w in e.writers) and not rec
                self.record.setText('■ 结束并保存' if rec else '正在落盘…' if saving else '● 开始保存')
                self.record.setEnabled(bool(rec) or (not saving and e.source!='REPLAY' and e.connected and age<2))
                self.mark.setEnabled(bool(rec)); self.mark.setText('结束动作  F5' if e.label else '标记动作  F5'); self.label.setEnabled(not bool(e.label))
                problem=e.record_fault or next((w.error for w in e.writers if w.error),None)
                if now<self._flash_until: self.footer.setText(self._flash_text)
                elif problem: self.footer.setText('保存错误（记录不完整）：'+problem)
                elif e.writers and not e.writers[-1].thread.is_alive(): self.footer.setText('已保存：'+str(e.writers[-1].path))
                elif rec: self.footer.setText('正在保存原始样本：'+str(rec.path))
                elif paused_replay: self.footer.setText('回放已暂停：时间轴静止，可编辑标签；取消“编辑标签”后点“继续回放”。')
                elif e.source=='REPLAY': self.footer.setText('历史回放：时间轴按记录文件重建，不是已验证的硬件同步。标签真值见 events.edited.csv（若存在）。')
                else: self.footer.setText('暂停显示不停止接收；显示去直流不改变原始文件。时间轴为显示估算。')
            if not self.freeze.isChecked(): self.render_frame()
            self.frame_times.append((time.perf_counter()-started)*1000); self.frame_times=self.frame_times[-120:]
            self.metrics.setText(f'数据更新 {np.median(self.frame_times):.1f} ms · 目标 30 fps')
        except Exception:
            self.render_errors+=1
            if time.monotonic()-self.last_error>5:
                self.last_error=time.monotonic(); LOG.exception('UI refresh failed; timer remains active'); self.footer.setText('显示异常，正在重试；接收线程独立。详情见 studio_v3.log')
    def render_frame(self):
        span=float(self.span.currentText()); x,y,end=self.engine.snapshot(span,self.kind)
        self._label_end=end
        self.label_plot.setXRange(-span,0,padding=0)
        self._sync_labels(end,span)
        if not len(x): return
        viewport=self.scroll.viewport().rect()
        for c,(plot,curve,label) in enumerate(zip(self.plots,self.curves,self.channel_labels)):
            top=plot.mapTo(self.scroll.viewport(),QtCore.QPoint(0,0)).y()
            if top>viewport.height() or top+plot.height()<0: continue
            raw=y[:,c]; finite=raw[np.isfinite(raw)]
            if not len(finite): continue
            center=float(np.median(finite)) if self.center.isChecked() else 0.
            plotted=raw-center; amp=max(30,float(np.max(np.abs(finite-center)))*1.15)
            plot.setXRange(-span,0,padding=0); plot.setYRange(-amp,amp,padding=0); curve.setData(x-end,plotted,connect='finite')
            for j,line in enumerate(self.guides[c]): line.setPos(-span+span*j/5)
            stale=time.monotonic()-self.engine.last_by_kind[self.kind]>2
            flag='无新数据' if stale else '平线 · 检查接触' if np.ptp(finite)==0 else '接近 int16 边界' if np.any(np.abs(finite)>=32760) else ''
            label.setText(f'{NAMES[self.kind][c]}'+(' · '+flag if flag else '')+f'\n{int(finite.min())}…{int(finite.max())}'); label.setToolTip('原始值范围；各通道独立自动缩放。'+('显示已去直流。' if self.center.isChecked() else '')+flag)
    # ---------- session memory ----------
    def _load_session(self):
        try: return json.loads(settings_path().read_text(encoding='utf-8'))
        except (OSError,ValueError): return {}
    def _save_session(self,source=None,replay=None):
        if not self._allow_persist_write: return
        engine=getattr(self,'engine',None)
        source=source or (engine.source if engine else 'RK_TCP')
        replay=replay if replay is not None else (engine.replay if engine else None)
        data=dict(source=source,replay=(str(replay) if source=='REPLAY' and replay else None),
                  span=self.span.currentText(),group=self.group.currentIndex(),
                  last_timeline=round(float(engine.latest),3) if engine else 0.,saved_unix_ns=time.time_ns())
        try:
            path=settings_path(); path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError: LOG.exception('session save failed')
    def closeEvent(self,event):
        self._save_session()
        self.guide.finish('application_closed')
        if not self.closing:
            self.closing=True; self.engine.shutdown(); self.record.setEnabled(False); self.more.setEnabled(False)
        if self.engine.busy(): event.ignore()
        else: self.timer.stop(); event.accept()

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--demo',action='store_true'); parser.add_argument('--replay')
    parser.add_argument('--port',type=int); parser.add_argument('--group',type=int,choices=(0,1)); parser.add_argument('--span',choices=('2','5','10','20'))
    parser.add_argument('--compare',action='store_true',help='第二/第三对比窗口：不读取也不写入工作区记忆')
    args=parser.parse_args()
    handler=RotatingFileHandler(ROOT/'studio_v3.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8'); logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')
    app=W.QApplication(sys.argv); app.setStyle('Fusion')
    sys.excepthook=lambda kind,value,tb:LOG.error('Unhandled callback',exc_info=(kind,value,tb))
    win=Window(args); win.show(); return app.exec()

if __name__=='__main__': sys.exit(main())
