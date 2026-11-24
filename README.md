# LMS Reolink Bridge 🎄🔊

Turn your **Reolink cameras into virtual Squeezebox players** visible in **Lyrion / Logitech Media Server (LMS)**.

Play music, playlists or radio in Lyrion and the audio comes out of your camera speakers (garden, driveway, front door...). Perfect for background music, announcements or Christmas carols 🎅.

```
                ┌─────────────┐   SlimProto    ┌──────────────────────┐
                │   Lyrion    │◄──────────────►│  lms-reolink-bridge  │
                │ (LMS)       │  virtual player│  (this container)    │
                └─────────────┘                └──────────┬───────────┘
                     ▲ play                          ffmpeg transcode
                     │                               + volume
        ┌────────────┴───────────┐            ┌──────────▼───────────┐
        │  your playlists/radio  │            │        go2rtc        │
        └────────────────────────┘            └──────────┬───────────┘
                                                       talkback (PCMU)
                                                ┌──────────▼───────────┐
                                                │   Reolink cameras    │
                                                └──────────────────────┘
```

## Requirements
- A working **Lyrion Music Server** (any recent version)
- **go2rtc** with your Reolink cameras already configured as sources (the bridge injects audio through `go2rtc /api/ffmpeg`, using the camera talkback channel — supported by most Reolink models)
- Docker

## Install
```bash
git clone https://github.com/SimoneB79/lms-reolink-bridge.git
cd lms-reolink-bridge
# edit compose.yaml: LMS_HOST, GO2RTC_BASE and your PLAYERS
docker compose up -d --build
```
Open Lyrion: the new players appear in the player list. Rename them there if you like. Press Play — done.

## Configuration (environment)
| Variable | Default | Description |
|---|---|---|
| `LMS_HOST` | localhost | Lyrion/LMS server address |
| `LMS_PORT` | 3483 | SlimProto port |
| `GO2RTC_BASE` | http://localhost:1984 | go2rtc base URL |
| `PLAYERS` | — | comma separated `NAME:GO2RTC_STREAM:MAC:HTTP_PORT` |
| `DEFAULT_VOLUME` | 0.4 | initial volume (0..1) |
| `LOG_LEVEL` | INFO | python logging level |

Each player needs a **unique fake MAC** (e.g. `02:00:00:0a:01:01`) and a **unique HTTP port** (8101, 8102...). `GO2RTC_STREAM` is the source name exactly as configured in go2rtc.

## What works
- Play / Pause / Stop from Lyrion (web, Material Skin, apps)
- Playlists and radio stations
- Volume slider (applied at track start)
- Multiple cameras at once (one virtual player each)

## Honest limitations
- Audio goes through the camera **talkback channel (PCMU, 8 kHz)**: intercom quality. Great for announcements and background music, not for hi-fi.
- Camera speakers are small: keep volume moderate.
- Volume changes apply at the start of the next track.

## Uninstall
```bash
docker compose down --volumes
```
The bridge registers nothing on the cameras; the players can be removed from Lyrion's player management if you don't want them listed.

## How it was built
The SlimProto protocol details were reverse-engineered from squeezelite and the LMS sources. Two non-obvious findings worth remembering:
- server→client frames use a 2-byte length **that includes the opcode**; client→server frames use a 4-byte length (big endian, payload only)
- the player **must declare supported formats as single-word capabilities** in HELO (`mp3,flc,ogg,aif,pcm`): without them LMS silently refuses to start playback

## Credits
Built by Simone (SimoneB79) with the help of OpenClaw. Inspired by the squeezelite player protocol.

## License
MIT — see LICENSE.
