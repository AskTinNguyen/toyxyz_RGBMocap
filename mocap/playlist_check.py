"""Optional playlist smoke check (never runs inference): open each of the first N playlist items through the
Playlist panel, confirm playback starts and the 3D view follows the video, then confirm auto-advance from
the last checked item's clip end. Writes a JSON report and closes the window."""
import json
import sys
import time
from pathlib import Path

import psutil
from PySide6.QtCore import QTimer

STEP_PLAY_MS = 1500
READY_TIMEOUT_S = 20


def schedule_check(window, steps, output: Path, finish):
    process = psutil.Process()
    steps = max(1, min(steps, len(window.playlist_items)))
    report = {'items': [], 'steps_requested': steps, 'playlist_items': len(window.playlist_items)}
    state = {'i': 0}

    def mem():
        info = process.memory_info()
        return dict(private_mib=round(info.private / 1024 ** 2, 1), os_threads=process.num_threads())

    report['start'] = mem()

    def finish_report():
        report['end'] = mem()
        report['inference_modules_loaded'] = 'torch' in sys.modules
        report['ok'] = all(x.get('played') and x.get('frames_advanced', 0) > 0 for x in report['items']) and report.get('auto_advance', {}).get('advanced', False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('PLAYLIST_CHECK ' + json.dumps(report), flush=True)
        window.close()
        finish()

    def open_item():
        i = state['i']
        if i >= steps:
            return check_auto_advance()
        entry = {'index': i, 'item': window.playlist_items[i]}
        report['items'].append(entry)
        state['t0'] = time.perf_counter()
        result = window.playlist_play(i)
        entry['opened'] = result is not False
        entry['waited_for_task'] = result is None
        if not entry['opened']:
            entry['status'] = window.status.text()
            state['i'] += 1
            return QTimer.singleShot(200, open_item)
        wait_play()

    def wait_play():
        entry = report['items'][-1]
        if window.timer.isActive() and window.playlist_index == entry['index']:
            entry['seconds_to_play'] = round(time.perf_counter() - state['t0'], 2)
            entry['played'] = True
            state['start_frame'] = window.slider.value()
            return QTimer.singleShot(STEP_PLAY_MS, measure)
        if time.perf_counter() - state['t0'] > READY_TIMEOUT_S:
            entry['played'] = False
            entry['status'] = window.status.text()
            state['i'] += 1
            return QTimer.singleShot(200, open_item)
        QTimer.singleShot(100, wait_play)

    def measure():
        entry = report['items'][-1]
        entry['frames_advanced'] = window.slider.value() - state['start_frame']
        entry['video_frame'] = window.video_view.index
        entry['motion_frame'] = window.motion_view.index
        entry['clip'] = [window.slider.clip_in, window.slider.clip_out]
        entry['window'] = list(window._playlist_window) if window._playlist_window else None
        entry['active_result'] = window.active_result
        entry['has_motion_preview'] = window.preview_data is not None
        entry['playlist_index_after'] = window.playlist_index
        entry.update(mem())
        window.stop_playback()
        state['i'] += 1
        QTimer.singleShot(200, open_item)

    def check_auto_advance():
        last = report['items'][-1]['index'] if report['items'] else 0
        window.playlist_play(last, autoplay=False)
        window.playlist_auto.setChecked(True)

        def go():
            if window.busy or window.playlist_index != last:
                return QTimer.singleShot(250, go)
            if window.info is None:
                report['auto_advance'] = {'advanced': False, 'reason': 'item did not load'}
                return finish_report()
            end = window._playlist_window[1] if window._playlist_window else window.slider.clip_out - 1
            window.slider.setValue(max(window.slider.clip_in, end - 9))
            state['t1'] = time.perf_counter()
            state['from'] = window.playlist_index
            window.toggle_play()
            QTimer.singleShot(100, wait_advance)

        QTimer.singleShot(1500, go)

    def wait_advance():
        expected = state['from'] + 1 if state['from'] + 1 < len(window.playlist_items) else None
        if expected is None:
            report['auto_advance'] = {'advanced': not window.timer.isActive(), 'note': 'last item: playback should stop at clip end'}
            if not window.timer.isActive() or time.perf_counter() - state['t1'] > 5:
                window.stop_playback()
                return finish_report()
        elif window.playlist_index == expected and window.timer.isActive():
            report['auto_advance'] = {'advanced': True, 'from': state['from'], 'to': window.playlist_index,
                                      'seconds': round(time.perf_counter() - state['t1'], 2)}
            window.stop_playback()
            return finish_report()
        elif time.perf_counter() - state['t1'] > 10 + READY_TIMEOUT_S:
            report['auto_advance'] = {'advanced': False, 'from': state['from'], 'index_now': window.playlist_index,
                                      'status': window.status.text()}
            window.stop_playback()
            return finish_report()
        QTimer.singleShot(100, wait_advance)

    QTimer.singleShot(1500, open_item)
