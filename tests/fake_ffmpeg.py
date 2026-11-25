#!/usr/bin/env python3
"""Fake ffmpeg per i test: genera PCM s16le mono 24k dai parametri dell'URL.

Parametri query: bytes (quanti byte scrivere), amp (ampiezza del quadrato),
rc (exit code), delay (secondi tra chunk da 2400 byte), tail (pausa finale).
Non scarica davvero l'URL: usa solo i parametri.
"""
import array
import sys
import time
from urllib.parse import parse_qs, urlparse

url = sys.argv[1]
q = parse_qs(urlparse(url).query)
n = int(q.get('bytes', ['9600'])[0])
amp = int(q.get('amp', ['32767'])[0])
pattern = q.get('pattern', ['square'])[0]
rc = int(q.get('rc', ['0'])[0])
delay = float(q.get('delay', ['0.05'])[0])
tail = float(q.get('tail', ['0.3'])[0])

written = 0
count = 1   # rampa da 1: il valore 0 si confonderebbe col silenzio iniziale
while written < n:
    buf = array.array('h')
    for i in range(1200):
        if pattern == 'ramp':
            buf.append(min(32767, count))
            count += 1
        else:
            buf.append(amp if (i % 2 == 0) else -amp)
    data = buf.tobytes()
    if written + len(data) > n:
        data = data[:n - written]
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    written += len(data)
    time.sleep(delay)
time.sleep(tail)
sys.exit(rc)
