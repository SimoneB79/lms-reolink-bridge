#!/usr/bin/env python3
"""
LMS/Lyrion - Reolink camera audio bridge  (v2.0)
================================================
Virtual Squeezebox players for Reolink cameras via go2rtc talkback.

v2 design:
  - ONE persistent WAV channel per camera (silence when idle, music when playing)
  - volume applied IN-PROCESS on PCM samples (instant, no stream restarts,
    Lyrion pause fades work smoothly)
  - single live session per camera, with keeper + duplicate detection
  - correct SlimProto state machine (STMc/STMh/STMa/STMs/STMt/STMp/STMr/STMf)

Environment:
  LMS_HOST, LMS_PORT (3483), GO2RTC_BASE, DEFAULT_VOLUME, LOG_LEVEL,
  PLAYERS = "NAME:GO2RTC_STREAM:MAC:HTTP_PORT,..."
"""
import os, socket, struct, subprocess, threading, time, urllib.request, logging
from http.server import BaseHTTPRequestHandler, HTTPServer
import array

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('reolink-bridge')

LMS_HOST = os.environ.get('LMS_HOST', 'localhost')
LMS_PORT = int(os.environ.get('LMS_PORT', '3483'))
GO2RTC = os.environ.get('GO2RTC_BASE', 'http://localhost:1984')
DEFAULT_VOLUME = float(os.environ.get('DEFAULT_VOLUME', '0.4'))

PLAYERS = []
for entry in os.environ.get('PLAYERS', '').split(','):
    parts = entry.strip().split(':')
    if len(parts) >= 4:
        PLAYERS.append((parts[0], parts[1], ':'.join(parts[2:-1]), int(parts[-1])))
if not PLAYERS:
    raise SystemExit('PLAYERS not configured')

CAPS = 'Model=reolink-bridge,AccuratePlayPoints=1,HasDigitalOut=1,Firmware=2.0,mp3,flc,ogg,aif,pcm'
RATE = 48000          # bytes/s of 24kHz s16le mono
CHUNK = 9600          # 200ms of audio

MY_IP = None


def pack_msg(op, payload):
    return op.encode() + struct.pack('>I', len(payload)) + payload


def apply_volume(data, vol):
    """Gain on s16le samples (software volume: instant, no restarts)."""
    if vol >= 0.995:
        return data
    a = array.array('h')
    a.frombytes(data)
    v = int(vol * 256)
    for i in range(len(a)):
        x = (a[i] * v) >> 8
        if x > 32767:
            x = 32767
        elif x < -32768:
            x = -32768
        a[i] = x
    return a.tobytes()


class CamPlayer:
    def __init__(self, name, stream, mac, port):
        self.name, self.stream, self.port = name, stream, port
        self.macb = bytes(int(x, 16) for x in mac.split(':'))
        self.vol = DEFAULT_VOLUME
        self.paused = False
        self.playing = False
        self.proc = None
        self.sock = None
        self._current_url = None
        self._rx_bytes = 0
        self._last_rx = 0.0
        self._start_ts = 0.0
        self._first_byte = False
        self._live_conns = set()

    # ---------------- slimproto client ----------------
    def connect(self):
        self.start_live()
        while True:
            try:
                self.sock = socket.create_connection((LMS_HOST, LMS_PORT), timeout=10)
                self.sock.settimeout(1.0)
                self.hello()
                self.loop()
            except Exception as e:
                log.info('%s: disconnected (%s); retry 5s', self.name, e)
                time.sleep(5)

    def hello(self):
        payload = struct.pack('>BB6s16sHII2s', 12, 0, self.macb, b'\x00' * 16, 0, 0, 0, b'it') + CAPS.encode()
        self.sock.sendall(pack_msg('HELO', payload))
        self.sock.sendall(pack_msg('SETD', b'\x00' + self.name.encode()))
        log.info('%s: HELO (mac %s)', self.name, ':'.join('%02x' % b for b in self.macb))

    def send_stat(self, event, ms=0):
        rx = self._rx_bytes
        payload = struct.pack('>4sBBBIIIIHIIIIHIIH',
                              event.encode(), 0, 0, 0,
                              65536, min(65535, rx), (rx >> 32) & 0xffffffff, rx & 0xffffffff,
                              0xffff, int(time.time() * 1000) & 0x7fffffff,
                              65536, min(65535, int((time.time() - self._start_ts) * 24000)) if self.playing else 0,
                              ms // 1000, 0, ms % 1000, 0, 0)
        try:
            self.sock.sendall(pack_msg('STAT', payload))
        except Exception:
            pass

    def loop(self):
        buf = b''
        while True:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError('closed by server')
            buf += chunk
            while len(buf) >= 6:
                length = struct.unpack('>H', buf[:2])[0]      # includes opcode
                op = buf[2:6]
                if len(buf) < 2 + length:
                    break
                payload = buf[6:6 + max(0, length - 4)]
                buf = buf[2 + length:]
                self.handle(op.decode('latin1').lower(), payload)

    def handle(self, op, p):
        if op == 'setd' and p and p[0] == 0:
            self.sock.sendall(pack_msg('SETD', b'\x00' + self.name.encode()))
        elif op == 'audg' and len(p) >= 18:
            adjust, gainL = p[8], struct.unpack('>I', p[10:14])[0]
            if adjust and gainL:
                self.vol = max(0.0, min(1.0, gainL / 100.0))
                log.info('%s: volume %.2f (software, immediato)', self.name, self.vol)
        elif op == 'strm':
            cmd = chr(p[0]) if p else ''
            if cmd in ('s', 'p', 'u', 'q', 'f'):
                log.info('%s: comando %r', self.name, cmd)
            if cmd == 's':
                self.send_stat('STMc')
                raw = p[24:].split(b'\x00')[0].decode('latin1', 'replace').strip()
                if raw.startswith('GET '):
                    parts = raw.split(' ')
                    path = parts[1] if len(parts) > 1 else raw[4:]
                    port = struct.unpack('>H', p[18:20])[0] or 80
                    ip = socket.inet_ntoa(p[20:24])
                    if ip in ('0.0.0.0', '127.0.0.1'):
                        ip = LMS_HOST
                    url = 'http://%s:%d%s' % (ip, port, path)
                else:
                    url = raw
                log.info('%s: PLAY %s', self.name, url)
                self.start_play(url)
                self.send_stat('STMh')
            elif cmd == 'p':
                self.paused = True
                self.send_stat('STMp')
            elif cmd == 'u':
                self.paused = False
                if not (self.proc and self.proc.poll() is None) and self._current_url:
                    self.start_play(self._current_url)
                self.send_stat('STMr')
            elif cmd in ('q', 'f'):
                self.stop_play()
                self.send_stat('STMf')
            elif cmd == 't':
                self.send_stat('STMt', self.elapsed_ms())

    def elapsed_ms(self):
        return int((time.time() - self._start_ts) * 1000) if self.playing else 0

    # ---------------- audio source ----------------
    def start_play(self, url):
        self.stop_play()
        self._current_url = url
        self.proc = subprocess.Popen(
            ['ffmpeg', '-loglevel', 'error', '-re', '-fflags', 'nobuffer', '-i', url,
             '-ar', '24000', '-ac', '1', '-f', 's16le', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.playing = True
        self.paused = False
        self._first_byte = False
        self._start_ts = time.time()
        self._last_rx = time.time()

    def stop_play(self):
        self.playing = False
        self.paused = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except Exception:
                self.proc.kill()
        self.proc = None

    # ---------------- hot channel ----------------
    def start_live(self):
        threading.Thread(target=self.serve_live, daemon=True).start()
        threading.Thread(target=self.live_keeper, daemon=True).start()
        threading.Thread(target=self.status_ticker, daemon=True).start()

    def serve_live(self):
        player = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                hdr = struct.pack('<4sI4s4sIHHIIHH4sI',
                                  b'RIFF', 0xFFFFFFFF, b'WAVE', b'fmt ', 16, 1, 1, 24000, RATE, 2, 16,
                                  b'data', 0xFFFFFFFF)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.end_headers()
                self.wfile.write(hdr)
                player._live_conns.add(self.connection)
                pending = b''
                import select
                try:
                    while True:
                        out = pending
                        pending = b''
                        proc = player.proc
                        if proc is not None and proc.poll() is None and not player.paused:
                            r, _, _ = select.select([proc.stdout], [], [], 0.15)
                            if r:
                                data = proc.stdout.read1(16384)
                                if data:
                                    if not player._first_byte:
                                        player._first_byte = True
                                        log.info('%s: primo byte dopo %.1fs', player.name,
                                                 time.time() - player._start_ts)
                                        player.send_stat('STMa')
                                        player.send_stat('STMs')
                                    out += apply_volume(data, player.vol)
                                    player._rx_bytes += len(data)
                                    player._last_rx = time.time()
                        if len(out) >= CHUNK:
                            self.wfile.write(out[:CHUNK])
                            pending = out[CHUNK:]
                            if len(pending) > 60000:
                                log.warning('%s: pending %d, scarto', player.name, len(pending))
                                pending = pending[-CHUNK:]
                        else:
                            self.wfile.write(out + b'\x00' * (CHUNK - len(out)))
                        self.wfile.flush()
                        time.sleep(0.2)
                except Exception:
                    pass
                finally:
                    player._live_conns.discard(self.connection)

            def log_message(self, *a):
                pass

        srv = None
        while srv is None:
            try:
                srv = HTTPServer(('0.0.0.0', self.port), H)
                srv.timeout = 1
                log.info('%s: canale live su porta %d', self.name, self.port)
            except OSError as e:
                log.warning('%s: porta %d occupata (%s), retry 5s', self.name, self.port, e)
                time.sleep(5)
        while True:
            try:
                srv.handle_request()
            except Exception:
                time.sleep(0.2)

    def live_keeper(self):
        while True:
            try:
                if not self._live_conns:
                    url = 'http://%s:%d/live.wav' % (MY_IP, self.port)
                    log.info('%s: (ri)apertura canale live -> %s', self.name, self.stream)
                    threading.Thread(target=lambda: urllib.request.urlopen(urllib.request.Request(
                        '%s/api/ffmpeg?dst=%s&file=%s' % (GO2RTC, self.stream, url),
                        method='POST'), timeout=60).read(), daemon=True).start()
                    time.sleep(30)
                # duplicate detection: piu' di una connessione = audio sovrapposto
                if len(self._live_conns) > 1:
                    log.warning('%s: %d connessioni live (rischio sovrapposizione!)',
                                self.name, len(self._live_conns))
                time.sleep(10)
            except Exception as e:
                log.warning('%s: keeper: %s', self.name, e)
                time.sleep(15)

    def status_ticker(self):
        while True:
            time.sleep(1)
            if self.playing and not self.paused:
                self.send_stat('STMt', self.elapsed_ms())


def main():
    global MY_IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((LMS_HOST, LMS_PORT))
        MY_IP = s.getsockname()[0]
    finally:
        s.close()
    log.info('bridge v2.0: LMS=%s:%s go2rtc=%s ip=%s players=%d',
             LMS_HOST, LMS_PORT, GO2RTC, MY_IP, len(PLAYERS))
    for cfg in PLAYERS:
        threading.Thread(target=CamPlayer(*cfg).connect, daemon=True).start()
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
