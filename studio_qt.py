"""Wave Studio 3: Qt/PyQtGraph UI; raw acquisition runs independently."""
import argparse, json, logging, sys, time
from logging.handlers import RotatingFileHandler
from pathlib import Path
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W
import pyqtgraph as pg
from wave_core import Engine, NAMES
from guided_capture import Guide
ROOT=Path(__file__).resolve().parent
COLORS=['#48d9ba','#6aafff','#eac267','#df8bbb','#ac9cef','#8bd39b','#ff9982','#6ed5e9','#d9ce8e']
LOG=logging.getLogger('wave')
pg.setConfigOptions(antialias=False,background='#101c2d',foreground='#9caec5')

class Window(W.QMainWindow):
    def __init__(self,args):
        super().__init__(); self.setWindowTitle('AirMouse Wave Studio 3'); self.resize(1260,880)
        self.config=dict(port=9002,emg=500,imu=200,subject='P01_wear01',output=str(ROOT/'recordings'))
        try: self.config.update(json.loads((ROOT/'studio_config.json').read_text(encoding='utf-8')))
        except (OSError,ValueError): pass
        if args.port is not None: self.config['port']=args.port
        self.transition=None; self.closing=False; self.frame_times=[]; self.last_stats=0.; self.last_error=0.; self.render_errors=0
        self.setStyleSheet('QMainWindow,QDialog{background:#0b1422;color:#deebf7;} QWidget{background:#0b1422;font-family:Microsoft YaHei UI;font-size:10pt;} QLabel{color:#deebf7;} QPushButton,QComboBox,QLineEdit,QSpinBox{background:#1c2c42;color:#e1edf9;border:1px solid #344b64;border-radius:5px;padding:7px;} QPushButton:hover{background:#2a405b;} QPushButton:disabled{color:#6f8094;background:#152235;} QPushButton#record{background:#136c60;} QCheckBox{color:#cad9e9;} QScrollArea{border:0;} QMenu{background:#1c2c42;color:#e1edf9;} QMenu::item:selected{background:#365675;}')
        base=W.QWidget(); self.setCentralWidget(base); layout=W.QVBoxLayout(base); layout.setContentsMargins(20,14,20,10); layout.setSpacing(12)
        header=W.QHBoxLayout(); title=W.QLabel('AIR MOUSE  /  WAVE STUDIO'); title.setStyleSheet('font-size:19pt;font-weight:700;color:#53d9c0'); header.addWidget(title); header.addStretch()
        self.badge=W.QLabel('准备连接'); header.addWidget(self.badge); layout.addLayout(header)
        hint=W.QLabel('打开即等待开发板  ·  接收、绘图与文件保存独立运行'); hint.setStyleSheet('color:#8fa5bf'); layout.addWidget(hint)
        tools=W.QHBoxLayout(); self.record=W.QPushButton('● 开始保存'); self.record.setObjectName('record'); self.record.clicked.connect(self.toggle_record); tools.addWidget(self.record)
        self.label=W.QComboBox(); self.label.setEditable(True); self.label.addItems(['rest','index_click','middle_click','pinch','clutch_thumb_ring','clutch_three_open','free']); tools.addWidget(self.label)
        self.mark=W.QPushButton('标记动作  F5'); self.mark.clicked.connect(self.toggle_mark); tools.addWidget(self.mark)
        self.freeze=W.QPushButton('暂停显示'); self.freeze.setCheckable(True); self.freeze.toggled.connect(lambda v:self.freeze.setText('继续显示' if v else '暂停显示')); tools.addWidget(self.freeze); tools.addStretch()
        self.more=W.QPushButton('设置与文件 ▾'); menu=W.QMenu(self)
        menu.addAction('连接与保存设置…',self.settings); menu.addAction('打开数据目录',self.open_folder)
        menu.addSeparator(); menu.addAction('打开历史记录回放…',self.open_replay); menu.addAction('回到真实设备',lambda:self.change_source('RK_TCP')); menu.addAction('模拟演示',lambda:self.change_source('SIMULATED'))
        menu.addSeparator(); menu.addAction('查看错误日志',lambda:QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(ROOT/'studio_v3.log')))); menu.addAction('安全退出',self.close)
        self.more.setMenu(menu); tools.addWidget(self.more); layout.addLayout(tools)
        view=W.QHBoxLayout(); self.group=W.QComboBox(); self.group.addItems(['肌电 · 9 通道','IMU · 6 通道']); view.addWidget(self.group)
        view.addWidget(W.QLabel('时间窗')); self.span=W.QComboBox(); self.span.addItems(['2','5','10','20']); self.span.setCurrentText('5'); view.addWidget(self.span)
        self.center=W.QCheckBox('显示去直流'); self.center.setChecked(True); view.addWidget(self.center); view.addWidget(W.QLabel('仅影响显示，CSV 保存原始值')); view.addStretch(); self.metrics=W.QLabel(); view.addWidget(self.metrics); layout.addLayout(view)
        self.info=W.QLabel('等待第一包数据…'); self.info.setWordWrap(True); layout.addWidget(self.info)
        self.scroll=W.QScrollArea(); self.scroll.setWidgetResizable(True); layout.addWidget(self.scroll,1)
        self.build_plots(); self.group.currentIndexChanged.connect(self.build_plots)
        self.ruler=W.QLabel(); self.ruler.setStyleSheet('color:#89a3bf;padding-left:135px'); layout.addWidget(self.ruler)
        self.footer=W.QLabel('时间轴为单调显示时间；不代表已验证的硬件同步。'); self.footer.setWordWrap(True); self.footer.setStyleSheet('color:#91a8c3'); layout.addWidget(self.footer)
        self.shortcut=QtGui.QShortcut(QtGui.QKeySequence('F5'),self); self.shortcut.activated.connect(self.toggle_mark)
        self.timer=QtCore.QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(33)
        self.start_engine('REPLAY' if args.replay else 'SIMULATED' if args.demo else 'RK_TCP',args.replay)
        self.guide=Guide(self)
        guide_button=W.QPushButton('引导采集'); tools.insertWidget(0,guide_button)
        guide_button.clicked.connect(self.guide.show)

    def build_plots(self,*unused):
        old=self.scroll.takeWidget()
        if old: old.deleteLater()
        content=W.QWidget(); lay=W.QVBoxLayout(content); lay.setContentsMargins(0,0,6,0); lay.setSpacing(5)
        self.curves=[]; self.plots=[]; self.channel_labels=[]; self.guides=[]; self.kind=16 if self.group.currentIndex()==0 else 17
        for i,name in enumerate(NAMES[self.kind]):
            frame=W.QWidget(); row=W.QHBoxLayout(frame); row.setContentsMargins(3,0,3,0)
            label=W.QLabel(name); label.setFixedWidth(126); label.setStyleSheet('color:'+COLORS[i]); row.addWidget(label)
            plot=pg.PlotWidget(); plot.setMinimumHeight(145); plot.setMaximumHeight(145); plot.setMenuEnabled(False); plot.setMouseEnabled(x=False,y=False); plot.hideButtons(); plot.hideAxis('bottom'); plot.hideAxis('left')
            guides=[]
            for j in range(6):
                line=pg.InfiniteLine(angle=90,pen=pg.mkPen('#24384d')); plot.addItem(line); guides.append(line)
            plot.addItem(pg.InfiniteLine(pos=0,angle=0,pen=pg.mkPen('#344b63'))); self.guides.append(guides)
            curve=plot.plot(pen=pg.mkPen(COLORS[i],width=1),connect='finite'); curve.setClipToView(True); curve.setDownsampling(auto=True,method='peak')
            row.addWidget(plot,1); lay.addWidget(frame); self.curves.append(curve); self.plots.append(plot); self.channel_labels.append(label)
        lay.addStretch(); self.scroll.setWidget(content)
    def start_engine(self,source,replay=None):
        self.engine=Engine(port=self.config['port'],rates={16:self.config['emg'],17:self.config['imu']},source=source,replay=replay); self.engine.start(); self.freeze.setChecked(False)
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
    def toggle_record(self):
        if self.guide.active:
            self.guide.finish('user_stop'); return
        try:
            if self.engine.recorder: self.engine.end_record()
            else: self.engine.start_record(self.config['output'],self.config['subject'])
        except Exception as exc: W.QMessageBox.warning(self,'无法保存',str(exc)); LOG.exception('record toggle failed')
    def toggle_mark(self):
        if not self.guide.active: self.engine.mark(self.label.currentText())
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
                self.info.setText(f'{e.status}  |  EMG {counts[16]:,} · {rates[16]:.0f} 点/s    IMU {counts[17]:,} · {rates[17]:.0f} 点/s  |  协议异常 {errors}  时间调整 {adjust}（非丢包计数）')
                span=float(self.span.currentText())
                if not self.freeze.isChecked(): self.ruler.setText('距最新数据：  '+'     │     '.join(f'−{span*(1-i/5):g}s' if i<5 else '最新' for i in range(6))+f'      显示时刻 {e.latest:.2f}s')
                saving=any(w.thread.is_alive() for w in e.writers) and not rec
                self.record.setText('■ 结束并保存' if rec else '正在落盘…' if saving else '● 开始保存')
                self.record.setEnabled(bool(rec) or (not saving and e.source!='REPLAY' and e.connected and age<2))
                self.mark.setEnabled(bool(rec)); self.mark.setText('结束动作  F5' if e.label else '标记动作  F5'); self.label.setEnabled(not bool(e.label))
                problem=e.record_fault or next((w.error for w in e.writers if w.error),None)
                if problem: self.footer.setText('保存错误（记录不完整）：'+problem)
                elif e.writers and not e.writers[-1].thread.is_alive(): self.footer.setText('已保存：'+str(e.writers[-1].path))
                elif rec: self.footer.setText('正在保存原始样本：'+str(rec.path))
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
            label.setText(f'{NAMES[self.kind][c]}\n范围 {int(finite.min())}…{int(finite.max())}\n'+('显示已去直流\n' if self.center.isChecked() else '')+flag)
    def closeEvent(self,event):
        self.guide.finish('application_closed')
        if not self.closing:
            self.closing=True; self.engine.shutdown(); self.record.setEnabled(False); self.more.setEnabled(False)
        if self.engine.busy(): event.ignore()
        else: self.timer.stop(); event.accept()

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--demo',action='store_true'); parser.add_argument('--replay'); parser.add_argument('--port',type=int); args=parser.parse_args()
    handler=RotatingFileHandler(ROOT/'studio_v3.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8'); logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')
    app=W.QApplication(sys.argv); app.setStyle('Fusion')
    sys.excepthook=lambda kind,value,tb:LOG.error('Unhandled callback',exc_info=(kind,value,tb))
    win=Window(args); win.show(); return app.exec()

if __name__=='__main__': sys.exit(main())
