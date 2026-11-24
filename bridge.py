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

    def connect(self):
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
        payload = struct.pack('>4sBBBIIIIHIIIIHIIH',
            event.encode(), 0, 0, 0,
            65536, 0, 0, 0, 0xffff, int(time.time() * 1000) & 0x7fffffff,
            65536, 0, ms // 1000, 0, ms, 0, 0)
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
                self.vol = max(0.0, min(1.0, gainL / 100.0))
                log.info('%s: volume %.2f', self.name, self.vol)
        elif op == 'strm':
            cmd = chr(p[0]) if p else ''
            if cmd in ('q', 'f'):
                self.stop_play()
                self.send_stat('STMf')
            elif cmd == 't':
                self.send_stat('STMt', self.elapsed_ms())
            elif cmd == 's':
                url = p[32:].split(b'\x00')[0].decode('latin1', 'replace')
                log.info('%s: PLAY %s', self.name, url)
                self.start_play(url)
                self.send_stat('STMp')
            elif cmd == 'p':
                self.stop_play()
                self.send_stat('STMp')
            elif cmd == 'u':
                self.send_stat('STMp')

    def elapsed_ms(self):
        return int((time.time() - self.start_ts) * 1000) if self.playing else 0

    def start_play(self, url):
        self.stop_play()
        self.proc = subprocess.Popen([
            'ffmpeg', '-loglevel', 'error', '-re', '-i', url,
            '-af', 'volume=%.2f' % self.vol, '-ar', '24000', '-ac', '1',
            '-f', 'mp3', '-b:a', '96k', 'pipe:1'
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.playing = True
        self.start_ts = time.time()
        self.http_thread = threading.Thread(target=self.serve_http, daemon=True)
        self.http_thread.start()
        file_url = 'http://%s:%d/stream.mp3' % (MY_IP, self.port)
        try:
            urllib.request.urlopen(urllib.request.Request(
                '%s/api/ffmpeg?dst=%s&file=%s' % (GO2RTC, self.stream, file_url),
                method='POST'), timeout=15).read()
            log.info('%s: audio injected into %s', self.name, self.stream)
        except Exception as e:
            log.warning('%s: inject failed: %s', self.name, e)

    def stop_play(self):
        self.playing = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except Exception:
                self.proc.kill()
        self.proc = None

    def serve_http(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        proc = self.proc

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                try:
                    while proc and proc.poll() is None:
                        data = proc.stdout.read(4096)
                        if not data:
                            break
                        self.wfile.write(b'%x\r\n' % len(data) + data + b'\r\n')
                        self.wfile.flush()
                except Exception:
                    pass
                try:
                    self.wfile.write(b'0\r\n\r\n')
                except Exception:
                    pass

            def log_message(self, *a):
                pass

        srv = HTTPServer(('0.0.0.0', self.port), H)
        srv.timeout = 1
        while self.playing:
            srv.handle_request()
        srv.server_close()

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
