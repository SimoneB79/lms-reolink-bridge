# lms-reolink-bridge

Play music from **Lyrion Music Server (LMS)** on your **Reolink cameras**: each
camera shows up in Lyrion as a normal Squeezebox player, and the audio reaches
the camera speaker through **go2rtc** talkback.

```
Lyrion (LMS) <--slimproto :3483--> lms-reolink-bridge <--HTTP WAV 24 kHz--> go2rtc <--RTSP talkback PCMU 8 kHz--> Reolink camera
```

## Highlights

- Cameras appear as players in Lyrion: play, pause, resume, volume (including
  Lyrion pause fades and mute), multi-track playlists
- **Hot channel**: a permanent silent stream per camera, so playback starts in
  ~0.2 s instead of the 8-12 s of a per-play talkback setup
- **Software volume** applied directly on PCM samples (instant, no restarts)
- Correct SlimProto state machine: end-of-track is reported (`STMd`), so
  playlists advance to the next song and the player stops at the end
- **Backpressure pacing**: the live channel dictates the real-time pace, so no
  audio is ever discarded, even if the source delivers faster than real time
- **Announcements just work**: inject a file/TTS through the go2rtc API
  (`POST /api/ffmpeg?dst=CAM01&file=...`) and the music pauses, then **resumes
  automatically** a few seconds after the announcement ends; the bridge checks
  the go2rtc API and never interrupts an announcement in progress
- Robust keeper: re-attaches the camera channel after go2rtc/camera restarts
  (poll 2 s, retry 5 s), never interrupts an announcement in progress, and
  drops stalled consumers automatically (a dead go2rtc exec can no longer
  hold the channel hostage)
- Single Python file, standard library only (plus ffmpeg in the image)

## Requirements

- Lyrion Music Server 9.x (slimproto port 3483 reachable from the bridge)
- go2rtc 1.9.x with your Reolink streams configured (the camera must support
  two-way audio / PCMU talkback)
- Docker (recommended) or Python 3.12+ with ffmpeg on PATH

## Quick start

```yaml
# compose.yaml
services:
  lms-cam-bridge:
    build: .
    container_name: lms-cam-bridge
    restart: unless-stopped
    network_mode: host        # go2rtc must reach the bridge HTTP ports
    environment:
      LMS_HOST: "192.168.0.10"
      LMS_PORT: "3483"
      GO2RTC_BASE: "http://192.168.0.10:1984"
      PLAYERS: "CAM01:CAM01:02:00:00:0d:01:01:8101,CAM02:CAM02:02:00:00:0d:02:02:8102,CAM03:CAM03:02:00:00:0d:03:03:8103"
      DEFAULT_VOLUME: "0.3"
```

```bash
docker compose up -d --build   # after any change: rebuild (COPY happens at build time)
docker logs -f lms-cam-bridge
```

Then open Lyrion: the players appear (rename them as you like from the Lyrion
UI). Play something and set the volume from Lyrion: the slider works, with the
same perceptual curve as a real player.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LMS_HOST` | localhost | Lyrion host |
| `LMS_PORT` | 3483 | slimproto port |
| `GO2RTC_BASE` | http://localhost:1984 | go2rtc API base URL |
| `PLAYERS` | required | comma list of `NAME:GO2RTC_STREAM:MAC:HTTP_PORT`; the MAC must be unique per player |
| `DEFAULT_VOLUME` | 0.4 | volume (0-1) before the first volume command from Lyrion |
| `LOG_LEVEL` | INFO | logging level |

## Announcements / TTS

Inject audio on the same camera through the go2rtc API:

```bash
curl -X POST "http://go2rtc:1984/api/ffmpeg?dst=CAM03&file=https://example/chime.mp3"
```

The announcement takes over the camera speaker; the music stops at the source
(nothing is lost) and resumes from the same position a few seconds after the
announcement ends.

## Limitations

- Reolink talkback audio is 8 kHz mu-law: telephone quality. Fine for
  background music and announcements, not hi-fi
- The camera audio channel is held open permanently (that is what makes the
  start instant); a per-play mode is possible but adds 8-12 s of latency
- Run only one bridge instance against the same Lyrion (player MACs must be
  unique)

## Tests

Fully offline, loopback only: no real Lyrion, go2rtc or cameras involved.

```bash
python3 -m unittest discover -s tests -v
```

The suite (18 tests) covers volume parsing (Lyrion sends 16.16 fixed-point
gains), the volume applied on the live channel, default volume, end-of-track
events, failed sources, server stop, pause/resume, two-track playlists, pacing
of over-producing sources, odd-length PCM chunks, keeper resilience, the
announcement-aware recovery and the stalled/zombie consumer protection.

## License

MIT