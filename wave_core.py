"""Wave Studio 3: raw acquisition never depends on the GUI paint loop."""
import csv
import json
import logging
import queue
import socket
import struct
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
import numpy as np

NAMES = {16: [f'CH{i}' for i in range(1, 10)],
         17: ['ACCX', 'ACCY', 'ACCZ', 'GYROX', 'GYROY', 'GYROZ']}
LOG = logging.getLogger('wave')

class Parser:
    def __init__(self):
        self.buf = bytearray(); self.bad = 0
    def feed(self, data):
        self.buf.extend(data); frames = []
        while len(self.buf) >= 8:
            magic, ver, kind, flags, reserved, size = struct.unpack_from('<2sBBBBH', self.buf)
            if magic != b'RK' or ver != 1 or kind not in (16, 17, 240) or size > 225:
                del self.buf[0]; self.bad += 1; continue
            if len(self.buf) < size + 8: break
            body = bytes(self.buf[8:8+size]); del self.buf[:8+size]
            if kind == 240: continue
            channels, limit = (9, 12) if kind == 16 else (6, 18)
            if len(body) < 9:
                self.bad += 1; continue
            stamp, count = struct.unpack_from('<QB', body)
            if not 1 <= count <= limit or len(body) < 9+2*count*channels:
                self.bad += 1; continue
            values = np.frombuffer(body, dtype='<i2', count=count*channels, offset=9).reshape(count, channels).copy()
            frames.append((kind, stamp, values))
        return frames

class Recorder:
    def __init__(self, path, meta):
        self.path = Path(path); self.path.mkdir(parents=True, exist_ok=False)
        self.meta = dict(meta, schema=3, complete=False, samples={'16':0, '17':0}, error=None,
            timing='estimated_device_s retains block stamp + sample/configured Hz; timeline_s is monotonic display-only, not verified physical synchronization')
        self.error = None; self.done = threading.Event(); self.queue = queue.Queue(2048)
        self.counts = {16:0, 17:0}; self.save_meta()
        self.thread = threading.Thread(target=self.run, name='csv-writer', daemon=True); self.thread.start()
    def save_meta(self):
        temp = self.path/'metadata.tmp'
        temp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path/'metadata.json')
    def put(self, item):
        if self.error: return
        try: self.queue.put_nowait(item)
        except queue.Full:
            self.error = '写入队列已满，本次记录不完整'; self.done.set()
    def finish(self): self.done.set()
    def run(self):
        handles = []
        try:
            writers = {}
            for k, name in ((16,'emg'), (17,'imu'), ('event','events')):
                f = (self.path/(name+'.csv')).open('w', encoding='utf-8', newline=''); handles.append(f)
                writers[k] = csv.writer(f)
                header = ['host_unix_ns','host_monotonic_ns','timeline_s','segment','label','event'] if k == 'event' else ['segment','block_device_ms','sample_index','estimated_device_s','timeline_s','host_unix_ns','host_monotonic_ns']+[f'ch{i+1}' for i in range(len(NAMES[k]))]
                writers[k].writerow(header)
            last = time.monotonic()
            while not self.done.is_set() or not self.queue.empty():
                try: item = self.queue.get(timeout=.2)
                except queue.Empty: item = None
                if item:
                    k = item[0]
                    if k == 'event': writers[k].writerow(item[1])
                    else:
                        _, seg, stamp, hz, times, vals, unix, mono = item
                        writers[k].writerows((seg, stamp, i, stamp/1000+i/hz, float(times[i]), unix, mono, *map(int,v)) for i,v in enumerate(vals))
                        self.counts[k] += len(vals)
                if time.monotonic()-last >= 1:
                    for f in handles: f.flush()
                    last = time.monotonic()
        except Exception as exc:
            self.error = str(exc); LOG.exception('recording failed')
        finally:
            for f in handles:
                try: f.close()
                except Exception as exc: self.error = str(exc)
            self.meta.update(complete=self.error is None, error=self.error, samples={str(k):v for k,v in self.counts.items()}, closed_unix_ns=time.time_ns())
            try: self.save_meta()
            except Exception as exc: self.error = str(exc); LOG.exception('metadata finalize failed')

class Engine:
    def __init__(self, port=9002, rates=None, source='RK_TCP', replay=None):
        self.port = port; self.rates = rates or {16:500,17:200}; self.source = source; self.replay = replay
        self.lock = threading.RLock(); self.stop = threading.Event(); self.pause = threading.Event(); self.thread = None
        self.blocks = {16:deque(),17:deque()}; self.counts = {16:0,17:0}; self.recent = {16:deque(),17:deque()}
        self.status = '准备连接'; self.connected = False; self.last_receive = 0.; self.last_by_kind={16:0.,17:0.}
        self.latest = 0.; self.segment = 0; self.anchor = None; self.offset = 0.; self.prev = {}; self.ends = {}
        self.adjustments = 0; self.bad = 0; self.discontinuities = 0; self.recorder = None; self.writers = []
        self.events = deque(maxlen=80); self.label = None; self.record_fault = None
        # 回放定位：流式块会被裁剪，所以回放额外留一份全量历史；view_end 为 None 表示跟随最新
        self.retain = (source == 'REPLAY')
        self.history = {16: deque(), 17: deque()}; self.history_samples = {16: 0, 17: 0}
        self.history_cap = 1_500_000          # 每组通道最多保留的样本数（500 Hz 约 50 分钟）
        self.view_end = None
    def message(self, text):
        self.events.append((time.strftime('%H:%M:%S'), text)); LOG.info(text)
    def start(self):
        target = self.network if self.source == 'RK_TCP' else self.replay_file if self.source == 'REPLAY' else self.demo
        self.thread = threading.Thread(target=self.worker, args=(target,), name='acquisition', daemon=True); self.thread.start()
    def worker(self, target):
        try: target()
        except Exception as exc: self.status='接收错误：'+str(exc); self.message(self.status); LOG.exception('source failed')
        finally:
            with self.lock:
                self.connected=False; self.end_record()
    def new_segment(self):
        with self.lock:
            self.segment += 1; self.anchor=None; self.prev={}; self.ends={}
            self.offset=self.latest+.2 if self.segment>1 else 0.
            if self.label: self.mark()
    def receive(self, kind, stamp, values):
        values=np.asarray(values,dtype=np.int16); now=time.monotonic(); unix=time.time_ns(); mono=time.monotonic_ns()
        with self.lock:
            old=self.prev.get(kind)
            if old is not None and stamp < old: self.new_segment(); self.discontinuities+=1
            if self.anchor is None: self.anchor=stamp/1000
            nominal=self.offset+stamp/1000-self.anchor; hz=self.rates[kind]
            minimum=self.ends.get(kind,nominal-1/hz)+1/hz
            begin=max(nominal,minimum)
            if begin>nominal+1e-8: self.adjustments+=1
            gap=old is None or nominal-self.ends.get(kind,nominal)>max(.1,3*len(values)/hz)
            if old is not None and gap: self.discontinuities+=1
            times=begin+np.arange(len(values))/hz
            self.prev[kind]=stamp; self.ends[kind]=float(times[-1]); self.latest=max(self.latest,float(times[-1]))
            self.blocks[kind].append((times, values, self.segment, gap))
            while self.blocks[kind] and (self.blocks[kind][0][0][-1]<self.latest-32 or len(self.blocks[kind])>3000): self.blocks[kind].popleft()
            if self.retain:
                # 回放额外留一份全量（供拖动定位），有上限；超限退化为流式
                self.history[kind].append((times, values, self.segment, gap)); self.history_samples[kind]+=len(values)
                while self.history[kind] and self.history_samples[kind]>self.history_cap:
                    old=self.history[kind].popleft(); self.history_samples[kind]-=len(old[1])
            self.counts[kind]+=len(values); self.last_receive=now; self.last_by_kind[kind]=now
            self.recent[kind].append((now,len(values)))
            while self.recent[kind] and self.recent[kind][0][0]<now-3: self.recent[kind].popleft()
            if self.recorder:
                self.recorder.put((kind,self.segment,stamp,hz,times,values,unix,mono))
                if self.recorder.error:
                    self.record_fault=self.recorder.error; self.message('保存失败：'+self.record_fault); self.end_record()
    def snapshot(self, span=5, kind=16):
        with self.lock:
            end=self.latest if self.view_end is None else self.view_end
            source=self.history[kind] if (self.retain and self.history[kind]) else self.blocks[kind]
            blocks=[b for b in source if b[0][-1]>=end-span and b[0][0]<=end]
        if not blocks: return np.empty(0),np.empty((0,len(NAMES[kind]))),end
        tt=[]; vv=[]; previous=None
        for t,v,seg,gap in blocks:
            if previous is not None and (gap or seg!=previous):
                tt.append(np.array([t[0]-1e-9])); vv.append(np.full((1,v.shape[1]),np.nan))
            tt.append(t); vv.append(v); previous=seg
        x=np.concatenate(tt); y=np.concatenate(vv).astype(float,copy=False)
        take=(x>=end-span)&(x<=end)
        return x[take],y[take],end
    def bounds(self):
        """回放可定位区间 (最早, 最新)；非回放或还没数据时返回 None。"""
        with self.lock:
            if not self.retain: return None
            lo=min((h[0][0][0] for h in self.history.values() if h),default=None)
            hi=max((h[-1][0][-1] for h in self.history.values() if h),default=None)
        return (float(lo),float(hi)) if lo is not None and hi is not None else None
    def seek(self, seconds):
        """把显示位置钉在某一时刻（绝对时间轴）；返回实际位置，失败返回 None。"""
        b=self.bounds()
        if not b: return None
        lo,hi=b; t=min(max(float(seconds),lo),hi)
        with self.lock: self.view_end=t
        return t
    def follow_latest(self):
        with self.lock: self.view_end=None
    def start_record(self, root, subject):
        with self.lock:
            if self.recorder: return
            if not self.connected or time.monotonic()-self.last_receive>2: raise ValueError('尚未收到实时数据，不能开始保存')
            if any(w.thread.is_alive() for w in self.writers): raise ValueError('上一段仍在保存，请稍候')
            folder=Path(root)/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            rec=Recorder(folder,dict(source=self.source,subject_wear=subject,rates_hz={str(k):v for k,v in self.rates.items()},units='raw int16',created_unix_ns=time.time_ns()))
            self.recorder=rec; self.writers.append(rec); self.record_fault=None; self.message('开始保存：'+str(folder)); return folder
    def end_record(self):
        with self.lock:
            if self.recorder:
                if self.label: self.mark()
                self.recorder.finish(); self.message('正在完成文件写入'); self.recorder=None
    def mark(self,label=None):
        with self.lock:
            if not self.recorder: return
            event='end' if self.label else 'start'; value=self.label or label or 'unlabeled'
            self.recorder.put(('event',[time.time_ns(),time.monotonic_ns(),self.latest,self.segment,value,event]))
            self.label=None if self.label else value
    def shutdown(self):
        self.stop.set(); self.end_record()
    def busy(self):
        return bool(self.thread and self.thread.is_alive()) or any(w.thread.is_alive() for w in self.writers)
    def network(self):
        while not self.stop.is_set():
            try:
                with socket.socket() as server:
                    if hasattr(socket,'SO_EXCLUSIVEADDRUSE'): server.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                    server.bind(('127.0.0.1',self.port)); server.listen(1); server.settimeout(.25)
                    self.status=f'等待开发板 · 本机 {self.port}'; self.message(self.status)
                    while not self.stop.is_set():
                        try: conn,addr=server.accept()
                        except socket.timeout: continue
                        with conn:
                            conn.settimeout(.25); parser=Parser(); before=0; self.new_segment(); self.connected=True; self.status='设备已连接'; self.message(self.status)
                            try:
                                while not self.stop.is_set():
                                    try: data=conn.recv(65536)
                                    except socket.timeout: continue
                                    if not data: break
                                    for frame in parser.feed(data): self.receive(*frame)
                                    self.bad+=parser.bad-before; before=parser.bad
                            except OSError as exc: self.message('连接中断：'+str(exc))
                            finally:
                                with self.lock:
                                    self.connected=False; self.end_record(); self.status='设备断开 · 等待重连'; self.message(self.status)
            except OSError as exc:
                self.status=f'端口 {self.port} 不可用：{exc}'; self.message(self.status); self.stop.wait(2)
    def demo(self):
        # demo_signal.py 是拆分版（github 仓库里有）；本地老副本从 engine.py 取同一个函数
        try:
            from demo_signal import simulated_imu
        except ImportError:
            from engine import simulated_imu
        import random
        rng=random.Random(42); start=time.monotonic(); counters={16:0,17:0}
        self.new_segment(); self.connected=True; self.status='模拟数据 · 不可用于训练'
        while not self.stop.wait(.005):
            for k,n in ((16,12),(17,18)):
                hz=self.rates[k]
                while (counters[k]+n)/hz<=time.monotonic()-start:
                    t=(counters[k]+np.arange(n))/hz
                    v=(np.sin(t[:,None]*np.arange(40,49)*6.28)*1500*(.2+np.maximum(0,np.sin(t[:,None]*2)))).astype(np.int16) if k==16 else np.array([simulated_imu(ti,rng) for ti in t],dtype=np.int16)
                    self.receive(k,round(counters[k]*1000/hz),v); counters[k]+=n
    def replay_file(self):
        # Streaming CSV replay, bounded memory; raw rows are never rewritten.
        import heapq
        handles=[]; heap=[]
        try:
            meta=json.loads((Path(self.replay)/'metadata.json').read_text(encoding='utf-8'))
            self.rates={int(k):int(v) for k,v in meta['rates_hz'].items()}
            def groups(path,k):
                f=path.open(encoding='utf-8',newline=''); handles.append(f); reader=csv.DictReader(f); rows=[]; key=None
                for row in reader:
                    current=(row['segment'],row['block_device_ms'],row['host_monotonic_ns'])
                    if rows and (current!=key or row['sample_index']=='0'):
                        yield int(key[2])/1e9,int(key[1]),np.asarray(rows,dtype=np.int16),key[0]; rows=[]
                    key=current; rows.append([int(row[f'ch{i+1}']) for i in range(len(NAMES[k]))])
                if rows: yield int(key[2])/1e9,int(key[1]),np.asarray(rows,dtype=np.int16),key[0]
            for k,name in ((16,'emg'),(17,'imu')):
                it=iter(groups(Path(self.replay)/(name+'.csv'),k)); first=next(it,None)
                if first: heapq.heappush(heap,(first[0],k,first,it))
            if not heap: raise ValueError('记录没有数据')
            origin=heap[0][0]; start=time.monotonic(); paused=0.; self.new_segment(); self.connected=True; self.status='历史数据回放 · 非实时设备'; last_seg=None
            while heap and not self.stop.is_set():
                if self.pause.is_set():
                    # Hold the display clock still so the plot (and label editing) is stable.
                    begin=time.monotonic()
                    while self.pause.is_set() and not self.stop.is_set(): time.sleep(.02)
                    paused+=time.monotonic()-begin
                    if self.stop.is_set(): break
                    continue
                host,k,item,it=heapq.heappop(heap)
                if self.stop.wait(max(0,host-origin-(time.monotonic()-start-paused))): break
                if last_seg is not None and item[3]!=last_seg: self.new_segment()
                last_seg=item[3]; self.receive(k,item[1],item[2])
                nxt=next(it,None)
                if nxt: heapq.heappush(heap,(nxt[0],k,nxt,it))
            self.status='回放已结束'
        finally:
            for f in handles: f.close()
