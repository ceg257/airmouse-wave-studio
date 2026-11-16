"""Non-blocking guided cues. Events are cue observations, NOT action truth."""
import json, random, time
from PySide6 import QtCore, QtWidgets as W

class Cue(W.QLabel):
    painted = QtCore.Signal()
    pending = False
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.pending:
            self.pending = False
            self.painted.emit()

class Guide(W.QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner; self.active=False; self.rec=None; self.deadline=None; self.waiting=False
        self.setWindowTitle('引导采集 · 提示时间 ≠ 动作起点'); self.resize(620,390)
        lay=W.QVBoxLayout(self)
        self.actions=W.QLineEdit('index_click,middle_click,pinch,clutch_thumb_ring')
        lay.addWidget(W.QLabel('动作（逗号分隔，可修改；离合只选实际使用的一种）')); lay.addWidget(self.actions)
        form=W.QFormLayout(); self.reps=W.QSpinBox(); self.reps.setRange(1,100); self.reps.setValue(5); form.addRow('每类次数',self.reps)
        self.durations=[]
        for name,default in [('准备秒',2),('执行秒',2),('放松秒',3)]:
            w=W.QSpinBox(); w.setRange(1,30); w.setValue(default); self.durations.append(w); form.addRow(name,w)
        lay.addLayout(form)
        self.cue=Cue('开始前确认已连接，九通道波形正常'); self.cue.setWordWrap(True); self.cue.setStyleSheet('font-size:24px;padding:18px;color:#53d9c0'); lay.addWidget(self.cue)
        self.status=W.QLabel('自动保存原始信号；提示仅作为待审核标签。'); self.status.setWordWrap(True); lay.addWidget(self.status)
        row=W.QHBoxLayout(); lay.addLayout(row)
        self.start=W.QPushButton('开始引导'); self.pause=W.QPushButton('暂停 / 重做本轮'); self.resume=W.QPushButton('继续（从准备重来）'); self.stop=W.QPushButton('结束引导')
        for b in (self.start,self.pause,self.resume,self.stop): row.addWidget(b)
        self.start.clicked.connect(self.begin); self.pause.clicked.connect(lambda:self.suspend('manual_pause')); self.resume.clicked.connect(self.continue_trial); self.stop.clicked.connect(lambda:self.finish('user_stop'))
        self.cue.painted.connect(self.on_painted)
        self.timer=QtCore.QTimer(self); self.timer.setTimerType(QtCore.Qt.PreciseTimer); self.timer.timeout.connect(self.tick); self.timer.start(25)
    def emit_event(self,kind,**extra):
        e=self.owner.engine
        with e.lock:
            if not self.rec or e.recorder is not self.rec: return False
            payload=dict(trial=self.index+1,attempt=self.attempt,action=self.order[self.index],phase=self.phase,review='unreviewed',**extra)
            ok=self.rec.put(('event',[time.time_ns(),time.monotonic_ns(),e.latest,e.segment,json.dumps(payload,ensure_ascii=False),'guide_'+kind]))
            return not self.rec.error
    def healthy(self):
        e=self.owner.engine
        return e.recorder is self.rec and not self.rec.error and e.connected and time.monotonic()-e.last_by_kind[16]<1
    def begin(self):
        if self.active: return
        e=self.owner.engine
        try:
            if e.source=='REPLAY': raise ValueError('回放不能用于引导采集')
            if e.recorder: raise ValueError('请先结束当前保存，再开始独立引导记录')
            if any(w.thread.is_alive() for w in e.writers): raise ValueError('等待上一份文件写完')
            actions=list(dict.fromkeys(x.strip() for x in self.actions.text().split(',') if x.strip()))
            if not actions: raise ValueError('至少填写一种动作')
            self.order=actions*self.reps.value(); random.shuffle(self.order)
            self.times=[w.value() for w in self.durations]; self.index=0; self.attempt=1; self.phase=0
            e.start_record(self.owner.config['output'],self.owner.config['subject']); self.rec=e.recorder
            self.active=True; self.waiting=False
            self.emit_event('session_start',order=self.order,durations=self.times,clock='host_monotonic_ns',timing='paint callback; no display/BLE latency correction')
            self.start.setEnabled(False)
            self.set_phase(0)
        except Exception as exc: W.QMessageBox.warning(self,'无法开始',str(exc))
    def set_phase(self,phase):
        self.phase=phase; self.deadline=None; self.requested=time.monotonic_ns()
        action=self.order[self.index]
        self.cue.setText(['准备：'+action,'执行一次：'+action,'放松'][phase])
        self.emit_event('cue_requested')
        self.cue.pending=True; self.cue.update()
    def on_painted(self):
        if not self.active or self.waiting: return
        self.emit_event('cue_painted',request_to_paint_ms=(time.monotonic_ns()-self.requested)/1e6)
        self.deadline=time.monotonic()+self.times[self.phase]
    def suspend(self,reason):
        if not self.active or self.waiting: return
        self.emit_event('trial_invalid',reason=reason)
        self.waiting=True; self.deadline=None; self.cue.pending=False; self.cue.setText('已暂停 · 本次尝试作废')
        self.status.setText('原始数据继续保存；恢复后重新准备当前动作。')
    def continue_trial(self):
        if self.active and self.waiting and self.healthy():
            self.attempt+=1; self.waiting=False; self.set_phase(0)
    def tick(self):
        if not self.active: return
        try:
            if not self.healthy():
                self.suspend('emg_stale_or_recording_lost')
                if self.owner.engine.recorder is not self.rec or self.rec.error: self.finish('recording_lost')
                return
            if self.waiting or self.deadline is None: return
            now=time.monotonic()
            self.status.setText(f'第 {self.index+1}/{len(self.order)} 轮 · 尝试 {self.attempt} · 剩余 {max(0,self.deadline-now):.1f}s')
            if now<self.deadline: return
            if now-self.deadline>.25: self.suspend('ui_deadline_missed'); return
            if self.phase<2: self.set_phase(self.phase+1)
            else:
                self.emit_event('trial_complete')
                if self.index+1==len(self.order): self.finish('completed')
                else: self.index+=1; self.attempt=1; self.set_phase(0)
        except Exception as exc:
            self.suspend('callback_error'); self.status.setText(str(exc))
    def finish(self,reason):
        if not self.active: return
        if reason!='completed': self.emit_event('trial_invalid',reason=reason)
        self.emit_event('session_end',reason=reason)
        self.active=False; self.deadline=None; self.cue.pending=False
        if self.owner.engine.recorder is self.rec: self.owner.engine.end_record()
        self.start.setEnabled(True); self.cue.setText('引导结束：'+reason); self.status.setText('数据目录：'+str(self.rec.path))
    def closeEvent(self,event):
        self.finish('window_closed'); event.accept()
