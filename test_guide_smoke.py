import os,time,tempfile,csv,json
os.environ['QT_QPA_PLATFORM']='offscreen'
from argparse import Namespace
from PySide6.QtWidgets import QApplication, QMessageBox
from studio_qt import Window
QMessageBox.warning=lambda *args: print(args[2],flush=True)
app=QApplication([]); w=Window(Namespace(port=0,replay=None,demo=True)); w.config['output']=str(__import__('pathlib').Path('test_artifacts/guide').resolve()); w.show()
def pump(s):
 end=time.monotonic()+s
 while time.monotonic()<end: app.processEvents(); time.sleep(.005)
pump(2); g=w.guide; g.show(); g.actions.setText('pinch'); g.reps.setValue(1)
for f in g.durations: f.setValue(1)
g.begin(); pump(.15); assert g.deadline is not None
g.suspend('test_pause'); assert g.waiting; g.continue_trial(); assert g.attempt==2
pump(3.4); assert not g.active
pump(.2); rows=list(csv.DictReader(open(g.rec.path/'events.csv',encoding='utf-8')))
assert any(r['event']=='guide_cue_painted' for r in rows)
assert any(r['event']=='guide_trial_invalid' for r in rows)
assert any(r['event']=='guide_session_end' and json.loads(r['label'])['reason']=='completed' for r in rows)
assert json.load(open(g.rec.path/'metadata.json'))['samples']['16']>0
w.close(); pump(.4)
print('PASS cue timing, pause/retry, completion, raw data, close')


