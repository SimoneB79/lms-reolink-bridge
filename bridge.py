#!/usr/bin/env python3
"""
LMS/Lyrion - Reolink camera audio bridge
=========================================
Turns Reolink cameras (any camera with audio talkback supported by go2rtc)
into virtual SqueezeBox players visible in Lyrion Music Server (LMS).

How it works:
  - connects to LMS via SlimProto and registers one virtual player per camera
  - when you press Play in Lyrion, LMS streams the audio to the bridge
  - the bridge transcodes it (ffmpeg, 24kHz mono, volume) and injects it
    into the camera speaker through go2rtc (POST /api/ffmpeg?dst=<stream>)

Environment variables:
  LMS_HOST        IP/hostname of Lyrion/LMS server       (default: localhost)
  LMS_PORT        SlimProto port                          (default: 3483)
  GO2RTC_BASE     base URL of go2rtc                      (default: http://localhost:1984)
  PLAYERS         comma separated list of players:
                  NAME:GO2RTC_STREAM:MAC:HTTP_PORT
                  e.g. "Garden:cam_garden:02:00:00:0a:01:01:8101,Door:doorbell:02:00:00:0a:02:02:8102"
                  (NAME = player name shown in Lyrion, GO2RTC_STREAM = source name in go2rtc,
                   MAC = unique fake MAC per player, HTTP_PORT = unique internal port)
  DEFAULT_VOLUME  initial volume 0..1                     (default: 0.4)

Notes:
  - audio quality is intercom-grade (PCMU 8kHz): great for announcements,
    background music, Christmas carols... not for hi-fi listening.
  - volume from the Lyrion slider is applied at track start.

License: MIT
"""
import os, socket, struct, subprocess, threading, time, urllib.request, logging

LMS_HOST = os.environ.get('LMS_HOST', 'localhost')
LMS_PORT = int(os.environ.get('LMS_PORT', '3483'))
GO2RTC = os.environ.get('GO2RTC_BASE', 'http://localhost:1984')
DEFAULT_VOLUME = float(os.environ.get('DEFAULT_VOLUME', '0.4'))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('reolink-bridge')

PLAYERS = []
for entry in os.environ.get('PLAYERS', '').split(','):
    entry = entry.strip()
    if not entry:
        continue
    parts = entry.split(':')
    if len(parts) < 4:
        log.warning('invalid player entry (need NAME:STREAM:MAC:PORT): %s', entry)
        continue
    name, stream, mac = parts[0], parts[1], ':'.join(parts[2:-1])
    port = int(parts[-1])
    PLAYERS.append((name, stream, mac, port))

if not PLAYERS:
    log.error('No players configured: set PLAYERS="NAME:STREAM:MAC:PORT,..."')
    raise SystemExit(1)

# SlimProto capabilities: the single-word tokens are the supported FORMATS
# (required: without them LMS considers the player unable to play anything).
CAPS = 'Model=reolink-bridge,AccuratePlayPoints=1,HasDigitalOut=1,Firmware=1.0,mp3,flc,ogg,aif,pcm'

MY_IP = None

def my_ip():
    global MY_IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((LMS_HOST, LMS_PORT)); MY_IP = s.getsockname()[0]
    finally:
        s.close()
    return MY_IP

def pack_msg(op, payload):
    # client -> server framing: [opcode 4B][length u32 BE][payload]
    return op.encode() + struct.pack('>I', len(payload)) + payload

class CamPlayer:
    def __init__(self, name, stream, mac, http_port):
        self.name, self.stream, self.mac, self.port = name, stream, mac, http_port
        self.macb = bytes(int(x, 16) for x in mac.split(':'))
        self.vol = DEFAULT_VOLUME
        self.proc = None
        self.playing = False
        self.start_ts = 0
        self.sock = None
        self.http_thread = None
        self._http_lock = threading.Lock()
        self._active_conns = set()
        self._live_conns = set()
        self._first_byte = False
        self._last_rx = 0.0
        self._rx_bytes = 0
        self._current_url = None
        self._vol_timer = None
        self.paused = False

    def _ticker(self):
        # STAT periodici spontanei: barra di avanzamento fluida
        while True:
            time.sleep(1)
            if self.playing and not self.paused:
                self.send_stat('STMt', self.elapsed_ms())

    def _watchdog(self):
        # chiude iniezioni rimaste mute (player fermo in LMS, stream senza dati)
        while True:
            time.sleep(5)
            if self.playing and not self.paused and self._last_rx and time.time() - self._last_rx > 15:
                log.info('%s: flusso LMS fermo da 15s -> silenzio (canale resta caldo)', self.name)
                self.playing = False
                self.send_stat('STMd')

    def start_http(self):
        if self.http_thread and self.http_thread.is_alive():
            return
        self.http_thread = threading.Thread(target=self.serve_live, daemon=True)
        self.http_thread.start()
        threading.Thread(target=self._live_keeper, daemon=True).start()

    def connect(self):
        self.start_http()
        while True:
            try:
                self.sock = socket.create_connection((LMS_HOST, LMS_PORT), timeout=10)
                self.sock.settimeout(1.0)
                self.hello()
                self.loop()
            except Exception as e:
                log.info('%s: disconnected (%s); retrying in 5s', self.name, e)
                self.stop_play()
                time.sleep(5)

    def hello(self):
        payload = struct.pack('>BB6s16sHII2s', 12, 0, self.macb, b'\x00' * 16, 0, 0, 0, b'it')
        payload += CAPS.encode()
        self.sock.sendall(pack_msg('HELO', payload))
        self.send_setd_name()
        log.info('%s: HELO sent (mac %s)', self.name, self.mac)

    def send_setd_name(self):
        # SETD answer: [id=0][name] - no intermediate length!
        self.sock.sendall(pack_msg('SETD', b'\x00' + self.name.encode()))

    def send_stat(self, event, ms=0):
        rx = int(getattr(self, '_rx_bytes', 0))
        fullness = min(65535, rx)
        out_full = min(65535, max(0, int((time.time() - getattr(self, 'start_ts', 0)) * 24000))) if self.playing else 0
        payload = struct.pack('>4sBBBIIIIHIIIIHIIH',
            event.encode(), 0, 0, 0,
            65536, fullness, (rx >> 32) & 0xffffffff, rx & 0xffffffff,
            0xffff, int(time.time() * 1000) & 0x7fffffff,
            65536, out_full, ms // 1000, 0, ms % 1000, 0, 0)
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
                raise ConnectionError('server closed connection')
            buf += chunk
            # server -> client framing: [length 2B BE, opcode INCLUDED][opcode 4B][payload]
            while len(buf) >= 6:
                length = struct.unpack('>H', buf[:2])[0]
                op = buf[2:6]
                plen = max(0, length - 4)
                if len(buf) < 2 + length:
                    break
                payload = buf[6:6 + plen]
                buf = buf[2 + length:]
                self.handle(op.decode('latin1'), payload)

    def handle(self, op, p):
        op = op.lower()
        if op == 'setd' and len(p) >= 1 and p[0] == 0:
            self.send_setd_name()
        elif op == 'audg' and len(p) >= 18:
            adjust, gainL = p[8], struct.unpack('>I', p[10:14])[0]
            if adjust and gainL:
                new = max(0.0, min(1.0, gainL / 100.0))
                if abs(new - self.vol) > 0.06:
                    self.vol = new
                    log.info('%s: volume %.2f', self.name, self.vol)
                    if self.playing and self._current_url:
                        # volume live: riavvia il flusso col nuovo volume (debounce 2.5s)
                        if self._vol_timer:
                            self._vol_timer.cancel()
                        t = threading.Timer(2.5, self._restart_with_volume)
                        t.daemon = True
                        t.start()
                        self._vol_timer = t
        elif op == 'strm':
            cmd = chr(p[0]) if p else ''
            if cmd in ('q', 'f'):
                self.stop_play()
                self.send_stat('STMf')
                self.send_stat('STMd')
            elif cmd == 't':
                self.send_stat('STMt', self.elapsed_ms())
            elif cmd == 's':
                # play: sequenza STAT corretta: CONNECT -> ENDOFHEADERS -> (AUTOSTART/TRACK_STARTED al primo byte)
                self.send_stat('STMc')
                raw = p[24:].split(b'\x00')[0].decode('latin1', 'replace').strip()
                if raw.startswith('GET '):
                    # direct streaming: la request_string e' una richiesta HTTP completa
                    path = raw.split(' ')[1] if ' ' in raw else raw[4:]
                    sport = struct.unpack('>H', p[18:20])[0] or 80
                    sip = socket.inet_ntoa(p[20:24])
                    if sip in ('0.0.0.0', '127.0.0.1'):
                        sip = LMS_HOST
                    url = 'http://%s:%d%s' % (sip, sport, path)
                else:
                    url = raw
                log.info('%s: PLAY %s', self.name, url)
                self.start_play(url)
                self.send_stat('STMh')
            elif cmd == 'p':
                # pausa: congela il flusso senza chiuderlo (LMS smette di mandare dati)
                self.paused = True
                self.send_stat('STMp')
            elif cmd == 'u':
                # ripresa: se il flusso e' morto lo riavvio
                self.paused = False
                if not (self.proc and self.proc.poll() is None) and self._current_url:
                    log.info('%s: resume -> riavvio flusso', self.name)
                    self.start_play(self._current_url)
                self.send_stat('STMr')

    def elapsed_ms(self):
        return int((time.time() - self.start_ts) * 1000) if self.playing else 0

    def start_play(self, url):
        self._current_url = url
        self.stop_play()
        # PCM 24k mono: entra nel canale live (WAV continuo verso go2rtc)
        self.proc = subprocess.Popen([
            'ffmpeg', '-loglevel', 'error', '-rw_timeout', '8000000', '-fflags', 'nobuffer', '-i', url,
            '-af', 'volume=%.2f' % self.vol, '-ar', '24000', '-ac', '1',
            '-f', 's16le', 'pipe:1'
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.playing = True
        self._first_byte = False
        self.start_ts = time.time()
        self._last_rx = time.time()

    def _restart_with_volume(self):
        url = self._current_url
        if url and self.playing and not getattr(self, '_restarting', False):
            self._restarting = True
            try:
                log.info('%s: riavvio flusso con volume %.2f', self.name, self.vol)
                self.start_play(url)
            finally:
                self._restarting = False

    def stop_play(self):
        self.playing = False
        self.paused = False
        for c in list(self._active_conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
        self._active_conns.clear()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except Exception:
                self.proc.kill()
        self.proc = None

    def serve_live(self):
        # canale SEMPRE CALDO: WAV PCM 24k mono infinito (silenzio quando non suona)
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import select
        player = self
        RATE = 48000  # bytes/s (24000 Hz * 2)

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                # header WAV streaming (size sconosciuta)
                hdr = struct.pack('<4sI4s4sIHHIIHH4sI',
                    b'RIFF', 0xFFFFFFFF, b'WAVE', b'fmt ', 16, 1, 1, 24000, RATE, 2, 16, b'data', 0xFFFFFFFF)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.end_headers()
                self.wfile.write(hdr)
                player._live_conns.add(self.connection)
                pending = b''
                try:
                    while True:
                        target = 9600  # 0.2s di audio
                        out = pending
                        pending = b''
                        proc = player.proc
                        if proc is not None and proc.poll() is None and not player.paused:
                            r, _, _ = select.select([proc.stdout], [], [], 0.15)
                            if r:
                                data = proc.stdout.read1(16384)
                                if data:
                                    out += data
                                    player._rx_bytes += len(data)
                                    player._last_rx = time.time()
                                    if not player._first_byte:
                                        player._first_byte = True
                                        log.info('%s: primo byte dopo %.1fs', player.name, time.time() - player.start_ts)
                                        player.send_stat('STMa')
                                        player.send_stat('STMs')
                        if len(out) >= target:
                            self.wfile.write(out[:target])
                            pending = out[target:]
                        else:
                            self.wfile.write(out + b'\x00' * (target - len(out)))
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
                log.info('%s: canale live attivo su porta %d (WAV 24k mono)', self.name, self.port)
            except OSError as e:
                log.warning('%s: porta %d occupata (%s), retry in 5s', self.name, self.port, e)
                time.sleep(5)
        while True:
            try:
                srv.handle_request()
            except Exception:
                time.sleep(0.2)

    def _live_keeper(self):
        # assicura che go2rtc abbia sempre una sessione aperta sul canale live
        import urllib.request
        while True:
            if not self._live_conns:
                url = 'http://%s:%d/live.wav' % (MY_IP, self.port)
                try:
                    threading.Thread(target=lambda: urllib.request.urlopen(urllib.request.Request(
                        '%s/api/ffmpeg?dst=%s&file=%s' % (GO2RTC, self.stream, url),
                        method='POST'), timeout=60).read(), daemon=True).start()
                    log.info('%s: POST canale live -> go2rtc (%s)', self.name, self.stream)
                except Exception as e:
                    log.warning('%s: live POST: %s', self.name, e)
            time.sleep(30)

def main():
    ip = my_ip()
    log.info('LMS-Reolink bridge starting: LMS=%s:%s go2rtc=%s my_ip=%s players=%d',
             LMS_HOST, LMS_PORT, GO2RTC, ip, len(PLAYERS))
    threads = [threading.Thread(target=p.connect, daemon=True) for p in
               [CamPlayer(*c) for c in PLAYERS]]
    for t in threads:
        t.start()
    while True:
        time.sleep(60)

if __name__ == '__main__':
    main()
