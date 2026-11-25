#!/usr/bin/env python3
"""Test offline del bridge: finto Lyrion (FakeSock sui frame), finto ffmpeg
(subprocess reale con script locale) e finto consumer go2rtc (GET live.wav).

Tutto su 127.0.0.1; nessun contatto con Lyrion/go2rtc veri.
Esecuzione: python3 -m unittest discover -s tests -v
"""
import array
import itertools
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

os.environ.setdefault('LMS_HOST', '127.0.0.1')
os.environ.setdefault('LMS_PORT', '34839')          # mai contattato nei test
os.environ.setdefault('GO2RTC_BASE', 'http://127.0.0.1:9')
os.environ.setdefault('DEFAULT_VOLUME', '0.3')
os.environ.setdefault('PLAYERS', 'CAMT:testdst:02:00:00:0d:09:09:18101')

import bridge  # noqa: E402

PORTS = itertools.count(18200)


def lms_gain(x):
    """Guadagno (16.16) che Lyrion invierebbe per lo slider x (curva SqueezePlay)."""
    db = (-74 + 1.48 * x) if x <= 25 else (-37 + (37.0 / 75.0) * (x - 25))
    if db >= -30:
        return int(10 ** (db / 20) * 256 + 0.5) * 256
    return int(10 ** (db / 20) * 65536 + 0.5)


def pack_audg(new_l, new_r=None, old=0):
    # equivalente Python del pack('NNCCNN', ...) di Lyrion: N=uint32 BE, C=uint8
    new_r = new_l if new_r is None else new_r
    return struct.pack('>IIBBII', old, old, 1, 255, new_l, new_r)


def pack_strm(cmd, port=0, request=b'GET /s.wav?bytes=9600 HTTP/1.0'):
    p = bytes([ord(cmd), 0, ord('p'), 0])   # 0..3 comando/autostart/format/spare
    p += struct.pack('>I', 24000)            # 4..8 sample rate
    p += struct.pack('>I', 0)                # 8..12 replay gain
    p += b'\x00\x00' + struct.pack('>I', 0)  # 12..18 riserve
    p += struct.pack('>H', port)             # 18..20 server port
    p += socket.inet_aton('127.0.0.1')       # 20..24 server ip
    p += request + b'\x00'                   # 24.. request string
    return p


def wait_for(cond, timeout=8.0, interval=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(interval)
    return cond()


class FakeSock:
    """Registra i frame slimproto inviati dal player."""

    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def sendall(self, data):
        with self.lock:
            self.frames.append(bytes(data))

    def events(self):
        with self.lock:
            return [f[8:12].decode('latin1') for f in self.frames if f[0:4] == b'STAT']


REAL_POPEN = subprocess.Popen  # bridge.subprocess e' il modulo globale: il patch e' globale


class FakePopen:
    """Sostituisce ffmpeg con tests/fake_ffmpeg.py; parametri dall'URL."""

    def __init__(self, cmd, **kw):
        url = cmd[cmd.index('-i') + 1]
        self._p = REAL_POPEN(
            [sys.executable, str(PROJ / 'tests' / 'fake_ffmpeg.py'), url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.stdout = self._p.stdout

    def poll(self):
        return self._p.poll()

    @property
    def returncode(self):
        return self._p.returncode

    def terminate(self):
        return self._p.terminate()

    def kill(self):
        return self._p.kill()

    def wait(self, timeout=None):
        return self._p.wait(timeout)


class LiveConsumer(threading.Thread):
    """Simula il consumer go2rtc: legge il canale live WAV."""

    daemon = True

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.data = b''
        self.stop_flag = False
        self.error = None

    def run(self):
        try:
            self.resp = urlopen('http://127.0.0.1:%d/live.wav' % self.port, timeout=10)
        except Exception as e:
            self.error = e
            return
        resp = self.resp
        try:
            while not self.stop_flag:
                d = resp.read(4800)
                if not d:
                    break
                if self.data == b'' and len(d) >= 44:
                    d = d[44:]   # header WAV
                self.data += d
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def samples(self):
        n = len(self.data) // 2
        a = array.array('h')
        a.frombytes(self.data[:n * 2])
        return a


class FakeGo2rtc:
    """Finto go2rtc: /api/streams con i producer, POST /api/ffmpeg che attacca
    il consumer al canale live (come fa go2rtc con l'exec ffmpeg)."""

    def __init__(self, stream_name, live_port):
        self.stream = stream_name
        self.live_port = live_port
        self.announcement = False
        self.posts = []
        self.consumer = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/api/streams'):
                    producers = [{'url': 'rtsp://cam.example'}, {'url': 'internal'}]
                    if outer.announcement:
                        producers.append(
                            {'source': 'exec:ffmpeg -i https://example/annuncio.mp3'})
                    body = json.dumps(
                        {'streams': {outer.stream: {'producers': producers}}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                if self.path.startswith('/api/ffmpeg'):
                    outer.posts.append(time.time())
                    q = parse_qs(urlparse(self.path).query)
                    file = (q.get('file') or [''])[0]
                    if 'live.wav' in file:
                        outer.consumer = LiveConsumer(outer.live_port)
                        outer.consumer.start()
                    self.send_response(200)
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(('127.0.0.1', 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self):
        return 'http://127.0.0.1:%d' % self.port

    def drop_consumer(self):
        c, self.consumer = self.consumer, None
        if c:
            c.stop_flag = True
            try:
                c.resp.close()
            except Exception:
                pass

    def post_after(self, t0):
        for t in self.posts:
            if t >= t0:
                return t
        return None


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.port = next(PORTS)
        self.player = bridge.CamPlayer('CAMT', 'testdst', '02:00:00:0d:09:09', self.port)
        self.player.sock = FakeSock()
        self._old_popen = bridge.subprocess.Popen
        bridge.subprocess.Popen = FakePopen
        self._old_ip = bridge.MY_IP
        bridge.MY_IP = '127.0.0.1'
        self.player.start_live()
        self.assertTrue(wait_for(lambda: self.player._live_conns or True))
        time.sleep(0.3)

    def attach_consumer(self):
        """Collega il finto consumer go2rtc: senza di lui il loop non legge."""
        c = LiveConsumer(self.port)
        c.start()
        time.sleep(0.15)
        return c

    def tearDown(self):
        bridge.subprocess.Popen = self._old_popen
        bridge.MY_IP = self._old_ip
        try:
            self.player.stop_play()
        except Exception:
            pass


class TestVolume(BridgeCase):
    def test_default_volume_before_audg(self):
        self.assertEqual(self.player.vol, 0.3)

    def test_audg_slider_values(self):
        p = self.player
        for x in (100, 50, 25, 10, 0):
            p.handle('audg', pack_audg(lms_gain(x)))
            self.assertAlmostEqual(p.vol, min(1.0, lms_gain(x) / 65536.0), places=4)
        p.handle('audg', pack_audg(65536))
        self.assertEqual(p.vol, 1.0)
        p.handle('audg', pack_audg(0))
        self.assertEqual(p.vol, 0.0)

    def test_apply_volume_q15(self):
        src = array.array('h', [32767, -32768, 1000, -1000, 1, -1])
        data = src.tobytes()
        out = array.array('h'); out.frombytes(bridge.apply_volume(data, 1.0))
        self.assertEqual(list(out), list(src))
        out = array.array('h'); out.frombytes(bridge.apply_volume(data, 0.5))
        for a, b in zip(src, out):
            self.assertAlmostEqual(abs(a) * 0.5, abs(b), delta=2)
        out = array.array('h'); out.frombytes(bridge.apply_volume(data, 0.0))
        self.assertEqual(list(out), [0] * len(src))
        # volumi molto bassi NON collassano a zero (era il difetto del Q8)
        out = array.array('h'); out.frombytes(bridge.apply_volume(src.tobytes()[:2], 0.0026))
        self.assertGreater(abs(out[0]), 0)

    def test_volume_on_live_channel(self):
        p = self.player
        cons = LiveConsumer(self.port)
        cons.start()
        # volume da slider 50 PRIMA del play: tutta la musica e' a quel gain
        p.handle('audg', pack_audg(lms_gain(50)))
        expected = min(1.0, lms_gain(50) / 65536.0)
        p.handle('strm', pack_strm('s', request=b'GET /s.wav?bytes=48000&delay=0.05 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: 'STMs' in p.sock.events()), 'STMs non arrivato')
        self.assertTrue(wait_for(lambda: len(cons.data) >= 19200), 'canale live muto')
        time.sleep(0.6)
        amp = max((abs(s) for s in cons.samples()), default=0)
        self.assertGreater(amp, 200, 'nessun audio sul canale live')
        self.assertLess(abs(amp - 32767 * expected) / 32767, 0.02,
                        'ampiezza %d attesa ~%.0f' % (amp, 32767 * expected))
        # mute
        before = len(cons.data)
        p.handle('audg', pack_audg(0))
        time.sleep(0.8)
        tail = cons.data[before:][-4800:]  # solo l'ultima porzione: niente dati in volo
        tailn = len(tail) // 2
        a = array.array('h'); a.frombytes(tail[:tailn * 2])
        self.assertEqual(max((abs(s) for s in a), default=0), 0, 'mute non applicato')


class TestEndOfTrack(BridgeCase):
    def test_end_of_track_sends_one_stmd_and_stops_ticker(self):
        p = self.player
        self.attach_consumer()
        p.handle('strm', pack_strm('s', request=b'GET /s.wav?bytes=9600 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: 'STMs' in p.sock.events()), 'STMs non arrivato')
        self.assertTrue(wait_for(lambda: p.sock.events().count('STMd') == 1), 'STMd non arrivato')
        self.assertFalse(p.playing)
        ev = p.sock.events()
        self.assertEqual(ev.count('STMd'), 1)
        self.assertEqual(ev.count('STMn'), 0)
        time.sleep(1.4)  # nessun nuovo STAT (ticker fermo)
        self.assertEqual(len(p.sock.events()), len(ev),
                         'STAT extra dopo la fine: %s' % p.sock.events()[len(ev):])

    def test_failed_source_sends_stmn(self):
        p = self.player
        self.attach_consumer()
        p.handle('strm', pack_strm('s', request=b'GET /s.wav?bytes=0&rc=1 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: 'STMn' in p.sock.events()), 'STMn non arrivato')
        ev = p.sock.events()
        self.assertEqual(ev.count('STMd'), 0)
        self.assertEqual(ev.count('STMn'), 1)
        self.assertFalse(p.playing)

    def test_server_stop_sends_stmf_no_stmd(self):
        p = self.player
        self.attach_consumer()
        p.handle('strm', pack_strm('s', request=b'GET /s.wav?bytes=480000&delay=0.05 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: 'STMs' in p.sock.events()), 'STMs non arrivato')
        time.sleep(0.5)
        p.handle('strm', pack_strm('q'))
        self.assertTrue(wait_for(lambda: 'STMf' in p.sock.events()), 'STMf non arrivato')
        time.sleep(1.5)
        ev = p.sock.events()
        self.assertEqual(ev.count('STMd'), 0)
        self.assertEqual(ev.count('STMn'), 0)
        self.assertFalse(p.playing)

    def test_two_songs_playlist(self):
        p = self.player
        self.attach_consumer()
        p.handle('strm', pack_strm('s', request=b'GET /a.wav?bytes=4800 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: p.sock.events().count('STMd') == 1))
        p.handle('strm', pack_strm('s', request=b'GET /b.wav?bytes=4800 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: p.sock.events().count('STMs') >= 2), 'seconda traccia non partita')
        self.assertTrue(wait_for(lambda: p.sock.events().count('STMd') == 2))
        self.assertFalse(p.playing)


class TestPause(BridgeCase):
    def test_pause_resume(self):
        p = self.player
        cons = LiveConsumer(self.port)
        cons.start()
        p.handle('strm', pack_strm('s', request=b'GET /s.wav?bytes=480000&delay=0.4 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: 'STMs' in p.sock.events()), 'STMs non arrivato')
        time.sleep(0.5)
        p.handle('strm', pack_strm('p'))
        self.assertTrue(wait_for(lambda: 'STMp' in p.sock.events()), 'STMp non arrivato')
        before = len(cons.data)
        time.sleep(0.8)
        a = array.array('h')
        seg = cons.data[before:]
        a.frombytes(seg[:len(seg) // 2 * 2])
        self.assertEqual(max((abs(s) for s in a), default=0), 0, 'pausa non silenziosa')
        p.handle('strm', pack_strm('u'))
        self.assertTrue(wait_for(lambda: 'STMr' in p.sock.events()), 'STMr non arrivato')
        self.assertTrue(p.playing)
        # dopo la ripresa l'audio torna (attesa esplicita di nuovi byte non-zero)
        self.assertTrue(wait_for(lambda: len(cons.data) > before), 'audio non ripreso')


class TestPacing(BridgeCase):
    def test_overproducing_source_is_paced_not_discarded(self):
        # sorgente piu' veloce del tempo reale: il canale detta il passo,
        # nessun byte scartato, ritmo ~48000 B/s
        p = self.player
        p.handle('audg', pack_audg(65536))   # vol 1.0: i byte passano invariati
        cons = self.attach_consumer()
        with self.assertLogs('reolink-bridge', level='INFO') as lg:
            p.handle('strm', pack_strm('s',
                     request=b'GET /fast.wav?bytes=480000&pattern=ramp&delay=0.001 HTTP/1.0'))
            self.assertTrue(wait_for(lambda: 'STMs' in p.sock.events()))
            time.sleep(4.0)
        self.assertFalse([l for l in lg.output if 'scarto' in l or 'WARNING' in l], lg.output)
        data = cons.data
        a = array.array('h'); a.frombytes(data[:len(data) // 2 * 2])
        # salta il silenzio iniziale (avvio di ffmpeg, atteso)
        s0 = 0
        while s0 < len(a) and a[s0] == 0:
            s0 += 1
        self.assertLess(s0, len(a), 'canale sempre muto')
        # da li' in poi la rampa deve essere consecutiva: nessun salto
        n = min(len(a) - s0, 32000)
        bad = [j for j in range(n) if a[s0 + j] != j + 1]
        self.assertEqual(bad[:5], [], 'dati saltati: %s; s0=%d; tot %d B' % (bad[:8], s0, len(data)))
        self.assertGreater(len(data), 150000, 'canale troppo lento: %d B' % len(data))
        self.assertLess(len(data), 240000, 'canale piu veloce del reale: %d B' % len(data))
        p.handle('strm', pack_strm('q'))

    def test_elapsed_freezes_during_pause(self):
        p = self.player
        self.attach_consumer()
        p.handle('strm', pack_strm('s',
                 request=b'GET /s.wav?bytes=480000&delay=0.05 HTTP/1.0'))
        self.assertTrue(wait_for(lambda: 'STMs' in p.sock.events()))
        time.sleep(0.6)
        p.handle('strm', pack_strm('p'))
        e1 = p.elapsed_ms()
        time.sleep(1.2)
        e2 = p.elapsed_ms()
        self.assertLess(abs(e2 - e1), 300, 'elapsed avanza in pausa: %d -> %d' % (e1, e2))
        p.handle('strm', pack_strm('u'))
        time.sleep(0.5)
        self.assertGreater(p.elapsed_ms(), e1 + 200)


class TestKeeper(unittest.TestCase):
    def setUp(self):
        self.port = next(PORTS)
        self.player = bridge.CamPlayer('CAMT', 'testdst', '02:00:00:0d:09:09', self.port)
        self.player.sock = FakeSock()
        self._old_popen = bridge.subprocess.Popen
        bridge.subprocess.Popen = FakePopen
        self._old_ip = bridge.MY_IP
        bridge.MY_IP = '127.0.0.1'
        self.fake = FakeGo2rtc('testdst', self.port)
        self._old_g = bridge.GO2RTC
        bridge.GO2RTC = self.fake.url()
        self.player.start_live()
        time.sleep(0.3)

    def tearDown(self):
        bridge.GO2RTC = self._old_g
        bridge.subprocess.Popen = self._old_popen
        bridge.MY_IP = self._old_ip
        try:
            self.player.stop_play()
        except Exception:
            pass

    def test_announcement_detection(self):
        p = self.player
        self.assertFalse(p.announcement_active())
        self.fake.announcement = True
        self.assertTrue(p.announcement_active())
        self.fake.announcement = False
        self.assertFalse(p.announcement_active())

    def test_recovery_after_consumer_drop(self):
        p = self.player
        self.assertTrue(wait_for(lambda: p._live_conns, timeout=10), 'consumer non collegato')
        t0 = time.time()
        self.fake.drop_consumer()
        self.assertTrue(wait_for(lambda: not p._live_conns, timeout=5), 'consumer ancora attivo')
        self.assertTrue(wait_for(lambda: self.fake.post_after(t0) is not None, timeout=12),
                        'nessuna re-iniezione')
        dt = self.fake.post_after(t0) - t0
        self.assertLess(dt, 9.0, 're-iniezione troppo lenta: %.1f s' % dt)
        self.assertTrue(wait_for(lambda: p._live_conns, timeout=12), 'consumer non ricollegato')

    def test_keeper_waits_for_announcement(self):
        p = self.player
        self.assertTrue(wait_for(lambda: p._live_conns, timeout=10))
        self.fake.drop_consumer()
        self.assertTrue(wait_for(lambda: not p._live_conns, timeout=5))
        self.fake.announcement = True
        t0 = time.time()
        time.sleep(7)   # piu' di KEEPER_RETRY+KEEPER_POLL: non deve postare
        self.assertIsNone(self.fake.post_after(t0), 'il keeper ha interrotto un annuncio!')
        self.assertFalse(p._live_conns)
        self.fake.announcement = False
        self.assertTrue(wait_for(lambda: self.fake.post_after(t0) is not None, timeout=12),
                        're-iniezione mancante dopo la fine annuncio')
        self.assertTrue(wait_for(lambda: p._live_conns, timeout=12), 'consumer non ricollegato')


class TestRobustness(BridgeCase):
    def test_odd_chunk_keeps_alignment(self):
        p = self.player
        self.assertEqual(p.take_even(b'abc'), b'ab')
        self.assertEqual(p._pcm_tail, b'c')
        self.assertEqual(p.take_even(b'def'), b'cdef')
        self.assertEqual(p._pcm_tail, b'')

    def test_keeper_survives_unreachable_go2rtc(self):
        errs = []
        old_hook = threading.excepthook
        threading.excepthook = lambda a: errs.append(a)
        try:
            with self.assertLogs('reolink-bridge', level='WARNING') as lg:
                t = threading.Thread(target=self.player._post_live, daemon=True)
                t.start()
                t.join(5)
        finally:
            threading.excepthook = old_hook
        self.assertEqual(errs, [], 'eccezione non gestita in un thread: %s' % errs)
        self.assertTrue(any('keeper' in line for line in lg.output), lg.output)


if __name__ == '__main__':
    unittest.main()
