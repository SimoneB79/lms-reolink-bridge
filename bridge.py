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

v2.3 (25/11/2026): canale live robusto ai consumer bloccati (zombie): timeout
  di stallo 30 s su ogni connessione, pulizia garantita in finally (anche se
  la scrittura dell'header fallisce), iniezioni scaglionate all'avvio
  (2 s tra le camere: evita SETUP simultanei alle camere) e avviso dopo 5
  tentativi falliti (con cadenza piu' lenta).

v2.2 (25/11/2026): keeper announcement-aware - controlla via API se un altro
  ffmpeg (annuncio TTS) sta usando lo stream go2rtc: in quel caso attende;
  altrimenti re-inietta il canale entro 5 s (prima: fino a 40 s di musica
  fermi dopo un annuncio). Retry/poll 5 s / 2 s.

v2.1 fixes (25/11/2026):
  - audg: Lyrion sends gains as 16.16 fixed point (65536 = 0 dB), NOT 0-100:
    the old parse collapsed any value to 1.00 and ignored 0 (volume always
    full, mute broken). Now vol = newL/65536, always applied; Q15 gain.
  - end of track: when the source ends, send STAT 'STMd' (decoder has no more
    data) so Lyrion advances to the next song or stops; on source failure send
    'STMn'. v2.0 never sent them: the player stayed in "play" forever at the
    end of the track and a playlist never went past the first song.
  - keeper: POST to go2rtc wrapped in try/except (no traceback while the stack
    is restarting)
  - PCM chunks with odd length no longer corrupt the s16le sample alignment

Environment:
  LMS_HOST, LMS_PORT (3483), GO2RTC_BASE, DEFAULT_VOLUME, LOG_LEVEL,
  PLAYERS = "NAME:GO2RTC_STREAM:MAC:HTTP_PORT,..."
"""
import json, os, socket, struct, subprocess, threading, time, urllib.request, logging
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
VERSION = '2.3'
KEEPER_POLL = 2.0     # s: quanto spesso il keeper controlla il consumer
KEEPER_RETRY = 5.0    # s: attesa tra i tentativi di (ri)iniezione del canale
CONN_STALL_TIMEOUT = 30.0   # s: consumer che non legge -> si chiude il canale
KEEPER_WARN_AFTER = 5       # tentativi prima di avvisare che il canale non attacca

MY_IP = None


def pack_msg(op, payload):
    return op.encode() + struct.pack('>I', len(payload)) + payload


def apply_volume(data, vol):
    """Gain on s16le samples (software volume: instant, no restarts).

    Q15 fixed-point gain: identity at vol=1.0, fine resolution at low volumes
    (a Q8 gain collapsed anything below ~1/256 to silence).
    """
    if vol >= 0.995:
        return data
    a = array.array('h')
    a.frombytes(data)
    v = int(round(vol * 32768))
    for i in range(len(a)):
        x = (a[i] * v) >> 15
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
        self._pcm_tail = b''      # odd byte of a chunk: keeps s16le alignment
        self._eof_signalled = False
        self._paused_total = 0.0    # tempo accumulato in pausa (elapsed onesto)
        self._pause_started = None

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
            # Lyrion 'audg' (Squeezebox2.pm): pack('NNCCNN', oldL, oldR, dvc,
            # preamp, newL, newR) [+ trailing seq]: no MAC in the payload.
            # Gains are 16.16 fixed point: 65536 = 0 dB (unity).
            new_l = struct.unpack('>I', p[10:14])[0]
            v = min(1.0, new_l / 65536.0)
            if v != self.vol:
                log.info('%s: volume %.3f (gain %d, software immediato)',
                         self.name, v, new_l)
            self.vol = v
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
                if self._pause_started is None:
                    self._pause_started = time.time()
                self.send_stat('STMp')
            elif cmd == 'u':
                if self._pause_started is not None:
                    self._paused_total += time.time() - self._pause_started
                    self._pause_started = None
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
        if not self.playing:
            return 0
        now = time.time()
        paused = self._paused_total
        if self._pause_started is not None:
            paused += now - self._pause_started
        return int((now - self._start_ts - paused) * 1000)

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
        self._eof_signalled = False
        self._pcm_tail = b''
        self._paused_total = 0.0
        self._pause_started = None
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

    def take_even(self, data):
        """Prepend the leftover byte and return only s16le-aligned bytes
        (an odd trailing byte is kept for the next chunk)."""
        data = self._pcm_tail + data
        if len(data) & 1:
            self._pcm_tail = data[-1:]
            return data[:-1]
        self._pcm_tail = b''
        return data

    def source_finished(self, returncode):
        """Source ended (not by a server command): tell Lyrion.

        STMd = 'decoder has no more data' -> Lyrion advances to the next song
        or ends the playlist; STMn = source failed -> it skips it.
        """
        if self._eof_signalled or not self.playing:
            return
        self._eof_signalled = True
        if self._first_byte and returncode == 0:
            log.info('%s: fine traccia -> STMd', self.name)
            self.send_stat('STMd')
        else:
            log.info('%s: sorgente fallita (rc=%s) -> STMn', self.name, returncode)
            self.send_stat('STMn')
        self.playing = False
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
                pending = b''
                import select
                player._live_conns.add(self.connection)
                try:
                    # consumer che smette di leggere (go2rtc morto/exec orfano):
                    # buffer di invio piccolo + timeout -> il keeper ricollega presto
                    self.connection.settimeout(CONN_STALL_TIMEOUT)
                    try:
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                                   2 * CHUNK)
                    except OSError:
                        pass
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/wav')
                    self.end_headers()
                    self.wfile.write(hdr)
                    while True:
                        out = pending
                        pending = b''
                        proc = player.proc
                        # backpressure: si legge solo cio' che il canale puo'
                        # suonare (rate tempo reale). ffmpeg resta bloccato
                        # sulla pipe piena: nessuno scarto di audio.
                        if (proc is not None and proc.poll() is None
                                and not player.paused and len(out) < CHUNK):
                            r, _, _ = select.select([proc.stdout], [], [], 0.15)
                            if r:
                                data = proc.stdout.read1(
                                    min(16384, 2 * CHUNK - len(out)))
                                if data:
                                    data = player.take_even(data)
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
                        # source ended (not by command): signal Lyrion once,
                        # after all pending audio has been written
                        if (player.playing and not player.paused
                                and player.proc is not None and not pending
                                and player.proc.poll() is not None):
                            player.source_finished(player.proc.returncode)
                        time.sleep(0.2)
                except Exception:
                    pass
                finally:
                    try:
                        self.connection.close()
                    except Exception:
                        pass
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

    def announcement_active(self):
        """True se un altro ffmpeg (es. annuncio TTS via API go2rtc) sta
        usando lo stream: in quel caso non re-iniettiamo il canale, perche'
        go2rtc Play() fermerebbe l'annuncio in corso."""
        try:
            with urllib.request.urlopen(GO2RTC + '/api/streams', timeout=5) as r:
                data = json.loads(r.read().decode('utf-8', 'replace'))
        except Exception:
            return False   # API non leggibile: non bloccare il recovery
        st = data.get('streams', data).get(self.stream) if isinstance(data, dict) else None
        if not isinstance(st, dict):
            return False
        for p in st.get('producers') or []:
            if not isinstance(p, dict):
                continue
            src = str(p.get('source') or '')
            if 'exec:' in src and 'live.wav' not in src:
                return True
        return False

    def _post_live(self):
        url = 'http://%s:%d/live.wav' % (MY_IP, self.port)
        try:
            urllib.request.urlopen(urllib.request.Request(
                '%s/api/ffmpeg?dst=%s&file=%s' % (GO2RTC, self.stream, url),
                method='POST'), timeout=60).read()
        except Exception as e:
            log.warning('%s: keeper: POST go2rtc non riuscito (%s)', self.name, e)

    def live_keeper(self):
        failures = 0
        while True:
            try:
                if self._live_conns:
                    failures = 0
                    # duplicate detection: piu' di una connessione = audio sovrapposto
                    if len(self._live_conns) > 1:
                        log.warning('%s: %d connessioni live (rischio sovrapposizione!)',
                                    self.name, len(self._live_conns))
                    time.sleep(KEEPER_POLL)
                elif self.announcement_active():
                    # un annuncio (altro exec ffmpeg) usa lo stream: aspetto,
                    # perche' la re-iniezione lo interromperebbe
                    time.sleep(KEEPER_POLL)
                else:
                    failures += 1
                    if failures == KEEPER_WARN_AFTER:
                        log.warning('%s: canale live non agganciato dopo %d tentativi: '
                                    'talkback bloccato? riavviare go2rtc (frigate)',
                                    self.name, failures)
                    log.info('%s: (ri)apertura canale live -> %s', self.name, self.stream)
                    threading.Thread(target=self._post_live, daemon=True).start()
                    time.sleep(KEEPER_RETRY if failures < KEEPER_WARN_AFTER else 30.0)
            except Exception as e:
                log.warning('%s: keeper: %s', self.name, e)
                time.sleep(KEEPER_POLL)

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
    log.info('bridge v%s: LMS=%s:%s go2rtc=%s ip=%s players=%d',
             VERSION, LMS_HOST, LMS_PORT, GO2RTC, MY_IP, len(PLAYERS))
    for cfg in PLAYERS:
        threading.Thread(target=CamPlayer(*cfg).connect, daemon=True).start()
        # iniezioni scaglionate: SETUP simultanei verso piu' camere
        # vengono rifiutati dalle Reolink
        time.sleep(2.0)
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
