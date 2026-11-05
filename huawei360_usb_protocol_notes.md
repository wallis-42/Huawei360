# Huawei 360 Camera – USB protocol notes

## Device

- VID 0x12D1 (4817), PID 0x109B (4251). Android filter also lists an HID-class (0x03) interface entry.
- App picks the first interface that has one bulk IN and one bulk OUT endpoint (a.b.a.b.h()).
- Max transfer chunk 16384 bytes; response buffer 65536; "short" control response buffer 64.

## Transport = USB Mass Storage Bulk-Only Transport style (SCSIHost, a/b/a/b.java, a/b/a/a.java)

- Claim interface, then everything over bulk endpoints.
- Command block wrapper (CBW, 31 bytes): "USBC" (55 53 42 43), random 32-bit tag, dCBWDataTransferLength (little-endian),
  flags (0x80 = device->host read, 0x00 = host->device write), LUN 0, CB length 0x10 (16), then a 16-byte vendor CDB.
- Read: send CBW on OUT, read IN in 16 KB chunks until a 13-byte CSW ("USBS" 55 53 42 53 + tag) arrives; payload = bytes before CSW.
- Write: send CBW, send data on OUT in <=16 KB chunks, read CSW from IN.
- Reset: control transfer bmRequestType 0x21, bRequest 0xFF (Bulk-Only Mass Storage Reset), wIndex = interface number, no data.

## Command layer (a/a/a/a.java "UsbCore", a/b/a/c.java keep-alive)

- 16-byte CDBs start with 0x7A (122) as opcode, byte 1 = sub-command.
- Keep-alive/poll CDB: 7A 03 FF ... (sent periodically, short-read mode).
- Example init CDB: 7A 00 02 00 00 00 00 00 01 01 00 00 <0x..> 01 00 00.
- Settings commands carry parameters at fixed offsets (photo_resolution, video_resolution, evb, white_balance, filter, bitrate, logo_type, date/time, orientation).
- Live view start/stop, frame fetch and the video format (H.264) are now mapped, see the session/command table below.

## Windows Script:

- Bind the interface to WinUSB (Zadig), use libusb/WinUSB, replicate CBW/CSW framing, send 7A/7B-opcode CDBs, poll `7A 05 01` for H.264 frames, decode with FFmpeg/AV, expose as a virtual camera (e.g. DirectShow/Media Foundation virtual cam or an equirect viewer).

## Session / command sequence (a/a/a/a.java UsbCore, a/b/a/c.java keep-alive) – all CDBs are 16 bytes, unlisted bytes = 0

All 16-byte CDBs below are sent inside the 31-byte CBW. "short" = reply buffer 64 bytes, "long" = up to 64 KB (read in 16 KB bulk-IN chunks). Replies end with the 13-byte CSW.
Reply byte 0 is a status: 0 = OK, 1 = busy (retry), 0xFF = camera in power-save (re-send Open Communication, then retry), anything else = error.

| Step                                     | CDB                                                                        | Notes                                                                                                                                                                                                                                                                                                                                      |
| ---------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| USB reset                                | control transfer 0x21 / 0xFF, wIndex = interface                           | done before Open when not "soft" open                                                                                                                                                                                                                                                                                                      |
| Open communication                       | `7A 00 01 00 00 00 00 00 00`                                               | repeat every 100 ms while reply[0]==1 (max ~200 tries); on 0 start keep-alive                                                                                                                                                                                                                                                              |
| Keep-alive                               | `7A 03 FF`                                                                 | short read, every 500 ms; 10 consecutive failures (5 s) = timeout/re-open                                                                                                                                                                                                                                                                  |
| "App in gallery" (idle)                  | `7A 00 02 00 00 00 00 00 01 01 00 00 2C 01 00 00`                          | short. Differs from Close only in byte 9 (=1). **Puts the camera into power-save**: every reply afterwards has status 0xFF until Open communication is sent again. Do not use for live view.                                                                                                                                               |
| Close communication                      | `7A 00 02 00 00 00 00 00 01 00 00 00 2C 01 00 00`                          | [8]=1, [9]=0, [12..15]=300 LE                                                                                                                                                                                                                                                                                                              |
| Get camera info                          | `7A 03 01`                                                                 | reply: firmware version string at offset 97 (up to 32 chars, starts with 'v')                                                                                                                                                                                                                                                              |
| Start live view                          | `7A 01 01 00 00 00 00 00 00 <res> 00`                                      | [8]=0, [9]=resolution code, [10]=0                                                                                                                                                                                                                                                                                                         |
| Stop live view                           | `7A 01 02`                                                                 |                                                                                                                                                                                                                                                                                                                                            |
| Start / stop recording                   | `7A 01 03` / `7A 01 04`                                                    |                                                                                                                                                                                                                                                                                                                                            |
| Take picture                             | `7A 01 05 00 00 00 00 00 <orientation>`                                    | [8]=orientation                                                                                                                                                                                                                                                                                                                            |
| Reset camera to defaults                 | `7A 01 82`                                                                 |                                                                                                                                                                                                                                                                                                                                            |
| Power off                                | `7A 01 F0`                                                                 | app sends this on overheat                                                                                                                                                                                                                                                                                                                 |
| FW: enter upgrade / examine bin / update | `7A 01 83` / `7A 01 84` (long timeout) / `7A 01 81`                        | do NOT use casually                                                                                                                                                                                                                                                                                                                        |
| Clear picture buffer                     | `7A 01 85`                                                                 |                                                                                                                                                                                                                                                                                                                                            |
| **Get live-view frame**                  | `7A 05 01 00 00 00 00 00 00`                                               | long read, see below                                                                                                                                                                                                                                                                                                                       |
| Get picture / thumbnail                  | `7A 05 02` / `7A 05 03`                                                    | picture buffer 320 KB, thumbnail 145 KB; payload at offset 20                                                                                                                                                                                                                                                                              |
| Settings (write path)                    | `7B 04 <sub> 00 <len>` + `<len>` data bytes sent on bulk OUT (CBW flags 0) | opcode is **0x7B**, not 0x7A. e.g. photo resolution `7B 04 03 00 01` + 1 byte (3 or 4); date/time `7B 04 01 00 18` + 24 bytes (year LE at data[2..3], month, day, hour, min, sec, ..). Combined settings block offsets: photo_res [2], video_res [3], evb [6], white balance [7], filter [32], bitrate [35], logo [39] (a.java ~1429-1490) |

Resolution codes (byte 9 of start live view and byte 4 of every frame reply): 9 = 1920x960, 10 = 1280x640 (default if unknown). **Only 9 and 10 exist** (see scan results below); every other code falls back to 10.

### Live-view frame reply layout

- byte 0 = status (0 ok, 1 busy, 0xFF power-save)
- byte 1 = "last chunk" flag: keep issuing `7A 05 01` and append payloads until byte 1 == 1; that is one complete frame
- byte 4 = resolution code
- byte 20 = thermal status (app shows overheat warning and sends power off at the hot level; 3 = cold)
- bytes 28-31 = payload length (little endian, must be <= bytes returned)
- payload starts at offset 32; frame buffer max 1 MiB
- Payload is **H.264 Annex-B** (start codes 00 00 00 01; app checks NAL types to find keyframes; decoded with Android MediaCodec video/avc, frame-rate hint 60). Decode with FFmpeg/libavcodec on Windows.
- The 2:1 sizes suggest an equirect or side-by-side fisheye layout; not yet confirmed. libViewController.so (OpenCV/TBB) does the 360 rendering.

### Confirmed on hardware (Windows, WinUSB, proto/hua360.py)

- Descriptors: interface 0, class FF / subclass 06 / protocol 50, bulk IN 0x82, bulk OUT 0x03, 512-byte packets (HS). Strings: "ABILITY" / "HA CAM". Windows binds WinUSB itself.
- Open sequence works: first `7A 00 01` replies busy (01), second replies 00. Close (`7A 00 02 ... 2C 01`) replies 00.
- `7A 03 01` reply: [1..7] = "VSC010Z", serial-like "123456789ABC" at offset 33, firmware string at offset 97 = "v1.3B00".
- Live view works cleanly with res codes 9 and 10 (any other code just gives code 10, see scan results below). The 'app in gallery' command is not needed (and idles the camera).
- Stream: H.264 High, yuvj420p full range, bt709, ~29-30 fps, ~3.7 Mbit/s at 1280x640 (ffplay reports 25 fps tbr, real rate ~29).
- Image is a 2:1 panorama already combined by the camera (black bands at top/bottom, strong lens distortion, camera-orientation dependent). Stitch seams and orientation still need correcting in software.

### Live-view resolution limits (tested)

- Res code 10 (1280x640) and 9 (1920x960) give clean live view (~29 fps). These are the only two the original app ever requests for live view (ui_Liveview/b.java: setting 0 -> 10, setting 1 -> 9).
- Res code 11 is accepted but is NOT 3840x1920: the stream is really 1280x640 (the camera echoes code 10). The earlier 'garbled tiles' were 1280x640 frames decoded as if they were 3840x1920. **Use --res 9 as the maximum for live view.**
- Scan of every code 0-16 (one-off script, since removed; firmware v1.3B00): codes 0-8 and 11-16 are all accepted (start status 0) but every one echoes code 10 and decodes to 1280x640 at ~28 fps. There is no lower live-view resolution; the camera only has 1920x960 and 1280x640.
- The app's 'thumbnail' (`7A 05 03`) is a small JPEG preview of the last still picture, not a live stream.
- Remaining ways to get sharper pictures: raise the camera's H.264 bitrate (settings command, key "bitrate" at a.java ~1350-1360 / settings block offset [35]), and use the stills path (`7A 05 02` get picture, photo resolution via `7B 04 03`) for full-resolution snapshots.

### Not yet verified / next

- Remaining `7B 04 xx` settings sub-commands (a.java ~1370-1520), e.g. orientation, exposure, white balance, bitrate.
- Orientation: the app passes an orientation value (from the phone's sensor) in take-picture; check whether the camera has its own orientation setting for the live stream.
- Virtual camera / 360 viewer on Windows.

### Bitrate findings (tested, firmware v1.3B00)

- Reading `7A 04 0C` gives the camera's bitrate setting (value in reply byte 0; 0xFF = power-save status, not a value). Camera default reads 2. The 48-byte settings block (`7A 04 60`) reads [2]=3 photo res, [3]=9 video res, date [10..16], [35]=0.
- **Settings block writes DO work**: `7B 04 60 00 30` + 48 bytes returned CSW 0 and the camera clock changed from the factory 2015-01-01 to the PC time (read back via `7A 04 60`). So the write path, framing and data layout are right.
- The bitrate field is ignored: both the single `7B 04 0C 00 01 <v>` and block byte [35]=0x10 leave the read-back at 2/0 and the real stream rate unchanged (~8 Mb/s at res 9, ~3.7 Mb/s at res 10, with or without --bitrate 16). Conclusion: live-view bitrate cannot be changed over USB on this firmware (probably only used for recording modes, or fixed by resolution).
- The original app writes 8 (res 10) / 16 (res 9) for live view and 4 / 8 for recording modes, always inside the full settings block with the current date/time.
- Side effect of testing: camera clock now set to the PC time (the app does this every session anyway).
- Other settings (exposure `evb` [6], white balance [7], filter [32], photo res [2]) use the same block and should be changeable the same way; untested.

## Snapshot (still picture) protocol - confirmed on hardware

- Photo size is a settings-block field, block[2]: 3 = 5376x2688, 4 = 3840x1920 (earlier notes had these swapped).
- Sequence: (optional) write settings block with photo size + local date/time -> start live view and pull a few frames so auto exposure settles ->
  `7A 01 05 [8]=orientation(0..3)` take picture (status 0/1 ok, 0xFF = power-save) ->
  loop `7A 05 02 [8..11]=offset LE`: status 1 = busy (sleep 0.1, retry); bytes 16..19 = chunk length LE, JPEG data at offset 20, byte1 = 1 on last chunk ->
  `7A 05 82 [8..11]=offset of last request` (picture done) -> `7A 01 85` (clear picture buffer).
- A 5376x2688 shot is ~4 MB and takes about 2.5 s.
- Live view does NOT need a restart after a shot: just keep polling `7A 05 01` (strategy A, ~45 frames within 1.5 s). No replug needed.
  (An earlier crash came from a run started while the previous session was still mid-shot.)
- Camera clock has no battery (resets to 2015-01-01): write the settings block with local time on every connect (`update_settings()`); hua360_view.py v3.4 does this automatically.

## Temperature

- No numeric temperature has been found in the protocol. The app only gets a coarse thermal status: frame header byte 20 (also byte 4 of `7A 03 30` / `7A 03 34` replies).
  App behaviour: 1 = hot warning dialog, 2 = overheating, app sends power off (`7A 01 F0`), 3 = cold warning, 0 = normal.
- hua360_view.py v3.4 shows this in the HUD and prints changes to the console, and dumps the raw `7A 03 30`/`7A 03 34` reply bytes at start ("thermal raw:") in case a finer value is hidden there.

## Exposure / white balance / filter values (from the app's menus; same 48-byte settings block)

- White balance, block[7]: 0 auto, 1 sunny, 2 cloudy, 3 tungsten, 4 fluorescent.
- Filter, block[32]: 0 none, 1 faded, 2 nimbus, 3 tea, 4 twilight, 5 sapphire, 6 vintage, 7 black/white, 8 newspaper.
- Exposure compensation, block[6]: 0 = 0 EV; 1..6 = -2.0, -1.7, -1.3, -1.0, -0.7, -0.3; 7..12 = +0.3, +0.7, +1.0, +1.3, +1.7, +2.0.
- Written exactly like the clock/photo size/logo (read block, edit byte, write block). Implemented in hua360_view.py (keys z/x, w, c); not yet confirmed on hardware.
- Logo/watermark, block[39]: 1 = on, 0 = off (confirmed: 0 removes the logo).

## 360 tags (stills and video)

- The camera's JPEGs carry a Google "GPano" XMP block (tool string "CV60") with ProjectionType equirectangular and **PoseRollDegrees = 180**, with the pixels already upright
  (checked on a 5376x2688 picture taken with orientation code 2). Viewers that honour the pose, such as VLC, therefore start the panorama upside down.
  hua360_meta.set_pano_pose() rewrites the block with roll 0; only the header changes, the pixels are identical.
- Live-view H.264 is the raw sensor-orientation stream, so it needs the mount rotation to look level. Recordings are tagged as equirectangular 360 video (Spherical Video V2: `st3d` + `sv3d` with `prhd` pose and `equi`)
  with the viewer's mount values as pose. The sign convention of that pose in players is a first guess (VIDEO_POSE_SIGN in hua360_view.py).
- Camera body: the base of the camera is at the bottom of the upright stills. The "Logo" setting (block[39] = 1) draws the camera's watermark over it; "Raw" (0) shows it; "Blur" is done in software (feathered blur of a cap around straight down).

## Idle power-off

- About 5 minutes after the last session is closed (close communication `7A 00 02`), the camera switches itself off and vanishes from USB (gone from Device Manager; only a physical replug brings it back). Windows power management is not involved (the drop happens with USB power management settings changed or restored).
- While a session is held open with the normal keep-alive (`7A 03 FF`, about every 0.5 s) and nothing else, it stays up (tested for 12+ minutes, LED off, no streaming); it drops 5 minutes after that session ends. hua360_hold.py is the diagnostic that does this.
