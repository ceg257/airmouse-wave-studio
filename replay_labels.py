"""Replay-time action labels read from a recording folder.

Two sources are supported (both live in a recording folder next to emg.csv):

1. manual marks written by wave_core.Engine.mark(): ``label`` is a plain class
   name, ``event`` is ``start``/``end`` (old collector.py also wrote
   ``auto_end``/``interrupted``).
2. guided capture (guided_capture.py): ``label`` is a JSON object with
   ``trial/attempt/action/phase``; the execution phase is ``phase == 1``.  The
   interval is inferred from two adjacent ``guide_cue_painted`` rows: the
   phase-1 paint is the start, the next paint (normally phase 2) is the end.

Time base: events.csv ``timeline_s`` is the *original* display clock of the
session (it can start at e.g. 478.76 s).  The replay engine rebuilds its own
monotonic display clock from 0 at the first recorded block, so every interval is
translated by ``timeline_s`` of the first emg row (measured alignment error
<= ~20 ms on three recordings).  Intervals that predate the first row are clamped
to 0.

Truth / rollback: if ``events.edited.csv`` exists it is the truth; otherwise
events.csv is read.  ``save()`` never rewrites events.csv: it copies the original
to ``events.csv.bak_<timestamp>`` and writes the normalized interval list to
``events.edited.csv``.  Delete events.edited.csv (or restore a .bak) to roll back.

No third-party imports: this module is usable from tests and from the GUI.
"""

import bisect
import csv
import json
import shutil
import time
import zlib
from datetime import datetime
from pathlib import Path

EDITED_NAME = 'events.edited.csv'
BACKUP_FMT = 'events.csv.bak_%Y%m%d_%H%M%S_%f'
EDIT_FIELDS = ['label', 'start_timeline_s', 'end_timeline_s', 'source', 'edited_unix_ns']
SOURCES = ('manual', 'guide')
# Distinct from the nine channel colors in studio_qt.COLORS.
PALETTE = ['#ff5c8a', '#ffa94d', '#ffe066', '#a0e57c', '#66d9e8',
           '#b197fc', '#f68b6b', '#94d82d', '#f783ac', '#63e6be']
GUIDE_START = 'guide_cue_painted'
MANUAL_CLOSE = ('end', 'auto_end', 'interrupted')


def color_for(label):
    """Stable class colour: same label -> same colour across sessions."""
    return PALETTE[zlib.crc32(str(label).encode('utf-8')) % len(PALETTE)]


def _json_label(text):
    text = (text or '').strip()
    if not text.startswith('{'):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def total_seconds(folder):
    """Approximate recording length from metadata (samples / configured rate)."""
    try:
        meta = json.loads((Path(folder) / 'metadata.json').read_text(encoding='utf-8'))
        rates = meta.get('rates_hz') or {}
        emg_hz = rates.get('16') or meta.get('requested_emg_hz') or 500
        samples = (meta.get('samples') or {}).get('16')
        if samples:
            return float(samples) / float(emg_hz)
    except (OSError, ValueError, TypeError, ZeroDivisionError):
        pass
    return None


def _emg_time_columns(folder):
    """(timeline_offset_s, (host_ns_list, timeline_list) or None) from emg.csv.

    Reads only the header for the offset; the full column arrays are built lazily
    by :func:`_nearest` users (only needed for old rows missing timeline_s).
    """
    path = Path(folder) / 'emg.csv'
    try:
        with path.open(encoding='utf-8', newline='') as handle:
            reader = csv.DictReader(handle)
            names = reader.fieldnames or []
            if 'timeline_s' not in names:
                return None, None
            for row in reader:
                try:
                    return float(row['timeline_s']), (names, path)
                except (TypeError, ValueError):
                    return None, (names, path)
    except OSError:
        pass
    return None, None


class _HostClock:
    """Maps event host_monotonic_ns -> stored timeline_s for old events.csv rows."""

    def __init__(self, names, path):
        self.ok = 'host_monotonic_ns' in names and 'timeline_s' in names
        self.host = []
        self.timeline = []
        if not self.ok:
            return
        try:
            with path.open(encoding='utf-8', newline='') as handle:
                for row in csv.DictReader(handle):
                    try:
                        self.host.append(int(row['host_monotonic_ns']))
                        self.timeline.append(float(row['timeline_s']))
                    except (TypeError, ValueError):
                        continue
        except OSError:
            self.ok = False

    def at(self, host_ns):
        if not self.ok or not self.host or host_ns is None:
            return None
        index = bisect.bisect_left(self.host, host_ns)
        if index <= 0:
            return self.timeline[0]
        if index >= len(self.host):
            return self.timeline[-1]
        before = self.host[index - 1]
        after = self.host[index]
        return self.timeline[index - 1 if host_ns - before <= after - host_ns else index]


def _row_time(row, clock):
    value = (row.get('timeline_s') or '').strip()
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    try:
        host = int(row['host_monotonic_ns'])
    except (KeyError, TypeError, ValueError):
        return None
    return clock.at(host)


def _manual_intervals(rows, clock):
    intervals = []
    open_mark = None
    for row in rows:
        if _json_label(row.get('label')):
            continue
        event = (row.get('event') or '').strip()
        label = (row.get('label') or '').strip() or 'unlabeled'
        if event == 'start':
            if open_mark:  # a previous start without end: close conservatively
                open_mark['end'] = _row_time(row, clock) or open_mark['start']
                intervals.append(open_mark)
            open_mark = dict(label=label, start=_row_time(row, clock), source='manual')
        elif event in MANUAL_CLOSE and open_mark:
            end = _row_time(row, clock)
            open_mark['end'] = end if end is not None else open_mark['start']
            intervals.append(open_mark)
            open_mark = None
    if open_mark:
        open_mark['end'] = open_mark['start']  # closed later by caller using total duration
        open_mark['open'] = True
        intervals.append(open_mark)
    return intervals


def _guide_intervals(rows, clock):
    painted = []          # (time, phase, action, trial, attempt)
    invalid_times = []
    for row in rows:
        event = (row.get('event') or '').strip()
        payload = _json_label(row.get('label'))
        if not payload:
            continue
        when = _row_time(row, clock)
        if when is None:
            continue
        if event == GUIDE_START:
            try:
                phase = int(payload.get('phase'))
            except (TypeError, ValueError):
                continue
            painted.append((when, phase, payload.get('action') or 'unknown',
                            payload.get('trial'), payload.get('attempt')))
        elif event == 'guide_trial_invalid':
            invalid_times.append(when)
    painted.sort(key=lambda item: item[0])
    intervals = []
    for index, item in enumerate(painted):
        when, phase, action, trial, attempt = item
        if phase != 1:
            continue
        end = painted[index + 1][0] if index + 1 < len(painted) else None
        if end is None:
            end = when + 2.0  # execution phase default duration; caller may clamp
            intervals.append(dict(label=action, start=when, end=end, source='guide',
                                  trial=trial))
            continue
        # A pause/retry inside the window means this attempt is not a clean action.
        if any(when < bad <= end for bad in invalid_times):
            continue
        intervals.append(dict(label=action, start=when, end=end, source='guide', trial=trial))
    return intervals


def _read_edited(folder):
    path = Path(folder) / EDITED_NAME
    if not path.exists():
        return None
    intervals = []
    try:
        with path.open(encoding='utf-8', newline='') as handle:
            for row in csv.DictReader(handle):
                try:
                    start = float(row['start_timeline_s'])
                    end = float(row['end_timeline_s'])
                except (KeyError, TypeError, ValueError):
                    continue
                label = (row.get('label') or '').strip() or 'unlabeled'
                source = (row.get('source') or 'edited').strip() or 'edited'
                intervals.append(dict(label=label, start=start, end=end, source=source))
    except OSError:
        return None
    return intervals


def load(folder):
    """Return ``(intervals, info)`` for a recording folder.

    ``intervals`` are dictionaries with ``label/start/end/source`` in the replay
    display clock (0 = first recorded block).  ``info`` carries truth source,
    counts and human-readable warnings for the UI.
    """
    folder = Path(folder)
    info = dict(folder=str(folder), truth=None, n_events=0, skipped=0, n=0, labels=[],
                warnings=[], offset_s=0.0, total_s=total_seconds(folder), edited=False)
    edited = _read_edited(folder)
    if edited is not None:
        info.update(truth=EDITED_NAME, edited=True)
        return _normalise(edited, info)
    events = folder / 'events.csv'
    if not events.exists():
        info['warnings'].append('该目录没有 events.csv，无法显示动作标签')
        return [], info
    info['truth'] = 'events.csv'
    try:
        with events.open(encoding='utf-8', newline='') as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        info['warnings'].append('读取 events.csv 失败：%s' % exc)
        return [], info
    info['n_events'] = len(rows)
    if not rows:
        return [], info
    offset, columns = _emg_time_columns(folder)
    clock = _HostClock(*columns) if columns else _HostClock([], None)
    if offset is None:
        info['warnings'].append('旧版记录缺少 timeline_s，无法把标签定位到回放时间轴')
        return [], info
    info['offset_s'] = offset
    intervals = _manual_intervals(rows, clock) + _guide_intervals(rows, clock)
    return _normalise(intervals, info, offset)


def _normalise(intervals, info, offset=0.0):
    total = info.get('total_s')
    clean = []
    skipped = 0
    for item in intervals:
        try:
            start = float(item['start']) - offset
            end = float(item['end']) - offset
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if item.get('open') and total:
            end = float(total)
        if end < start:
            start, end = end, start
        start = max(0.0, start)
        if total:
            end = min(max(end, start), float(total))
            start = min(start, float(total))
        if end - start < 0.02:
            skipped += 1
            continue
        item = dict(item, start=start, end=end)
        clean.append(item)
    clean.sort(key=lambda item: (item['start'], item['end'], item['label']))
    info['skipped'] += skipped
    info['n'] = len(clean)
    info['labels'] = sorted({item['label'] for item in clean})
    return clean, info


def save(folder, intervals):
    """Back up events.csv and write events.edited.csv (the truth file).

    Returns ``(edited_path, backup_path)``.  events.csv itself is never modified.
    """
    folder = Path(folder)
    original = folder / 'events.csv'
    backup = None
    if original.exists():
        backup = folder / datetime.now().strftime(BACKUP_FMT)
        shutil.copy2(original, backup)
    target = folder / EDITED_NAME
    temp = folder / (EDITED_NAME + '.tmp')
    stamp = time.time_ns()
    with temp.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=EDIT_FIELDS)
        writer.writeheader()
        for item in sorted(intervals, key=lambda value: (value['start'], value['end'])):
            writer.writerow(dict(label=item['label'], start_timeline_s=round(float(item['start']), 6),
                                 end_timeline_s=round(float(item['end']), 6),
                                 source=item.get('source', 'edited'), edited_unix_ns=stamp))
    temp.replace(target)
    return target, backup
