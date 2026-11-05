# Huawei360

Use the discontinued Huawei 360 panoramic camera (USB 12D1:109B, "HA CAM") on Windows, without the Android app.
The camera is not a UVC webcam. It speaks a vendor protocol over a single WinUSB bulk interface. The protocol details are in
[huawei360_usb_protocol_notes.md](huawei360_usb_protocol_notes.md).

## Setup

- Windows with the camera bound to WinUSB (Windows did this automatically here; otherwise use Zadig).
- Python 3.9 or newer, then `pip install -r requirements.txt`:
  `pyusb` and `libusb-package` (USB access), `numpy` and `opencv-python` (image maths, preview, JPEG/PNG), `av` (decodes the H.264 stream),
  `pyvirtualcam` (needs OBS Studio installed, for the OBS Virtual Camera).
- The control panel uses tkinter, which comes with the python.org Windows installer.
- Either USB-C orientation works.
- Only one program can talk to the camera at a time. Close the viewer before running the snapshot tool or `hua360.py`.

## Files

| File                              | What it is                                                                                         |
| --------------------------------- | -------------------------------------------------------------------------------------------------- |
| `hua360_panel.bat`                | Double-click launcher: viewer + control panel + OBS Virtual Camera switched on                     |
| `hua360_view.py`                  | The 360 viewer and OBS Virtual Camera; opens the control panel                                     |
| `hua360_panel.py`                 | The control panel window (Tk). Started by the viewer, not run on its own                           |
| `hua360_hold.py`                  | Diagnostic: holds the camera awake with keep-alives only (no streaming; LED off) and logs when it drops off USB. The camera switches itself off about 5 minutes after a session closes |
| `hua360_meta.py`                  | 360 tags: makes stills start the right way up in VLC, marks recordings as 360 video; also a small fix-up tool (below) |
| `hua360_img.py`                   | Image helpers: rotation maths and the blur of the camera body                                      |
| `hua360_snap.py`                  | Command line tool for full-resolution still pictures                                               |
| `hua360.py`                       | The protocol library (`Camera`, `UsbTransport`), plus a small command line to stream raw H.264     |
| `huawei360_usb_protocol_notes.md` | Reverse engineering notes: transport, commands, reply layouts, findings                            |
| `requirements.txt`                | Python packages                                                                                    |
| `hua360_view.json`                | Created by the panel's Save settings: mount, zoom, look direction, exposure, white balance, filter, watermark |
| `snapshots\`, `recordings\`       | Output folders (created when first used)                                                           |

Generated output (`snapshots\`, `recordings\`, `hua360_view.json`, saved PNGs, `__pycache__`) is git-ignored.

## hua360_panel.bat

Runs `python hua360_view.py --res 9 --size 1280x720 --vcam --reset %*`, so the camera starts from a clean state, the control panel opens and the
OBS Virtual Camera is on. Extra options can be added to the command line, for example `hua360_panel.bat --window --body blur`.
It pauses if the viewer exits with an error, so the message stays on screen.

## hua360_view.py: viewer and OBS virtual camera

```
python hua360_view.py [options]
```

Opens the control panel (there are no keyboard controls), connects to the camera, decodes the live H.264 stream, and renders it
either as a flat equirectangular picture or as a perspective view you can look around in. The result can be sent to the OBS Virtual Camera,
shown in a large window, recorded, or captured as a still.
On every connect it sets the camera's clock (the camera has no clock battery) and applies the photo size and any settings given below.
If the camera stops answering, the viewer reconnects by itself, using a full software replug.

| Option                  | Meaning                                                                                                                 |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `--res 9\|10`           | Live view size: 9 = 1920x960, 10 = 1280x640 (default 10). Also switchable in the panel                                  |
| `--size WxH`            | Size of the perspective view and of the virtual camera (default 1280x720). Fixed while running; the flat view is letterboxed into it |
| `--vcam`                | Start with the OBS Virtual Camera on (also a checkbox in the panel)                                                     |
| `--window`              | Also open the large view window at start (also a checkbox in the panel)                                                 |
| `--reset`               | Force a software replug (USB reset) before connecting, for a camera in a stuck state                                    |
| `--photo big\|small`    | Snapshot size: big = 5376x2688 (default), small = 3840x1920                                                             |
| `--orientation 0..3`    | Rotation code sent with a snapshot (default 2)                                                                          |
| `--body logo\|raw\|blur` | How the camera body at the bottom of the picture is shown: logo = the camera's watermark, raw = nothing, blur = a soft blur. Default: the saved setting, else leave as it is |
| `--blur-size DEG`       | Blur: degrees around straight down that are blurred (default 22)                                                         |
| `--blur-strength N`     | Blur: strength 1 to 10 (default 4)                                                                                       |
| `--start-yaw DEG`, `--start-pitch DEG` | 360 tag: where players (VLC etc.) should look when a still or recording opens. Recordings: added to the mount pose (sign convention unverified) |
| `--start-fov DEG`       | 360 tag: start zoom (horizontal field of view); stills only, video has no such field                                       |
| `--start-from-view`     | 360 tag: start where the live view is looking when the still / recording is taken (overrides `--start-*`)                |
| `--ev VALUE`            | Exposure compensation on connect: -2.0, -1.7, -1.3, -1.0, -0.7, -0.3, 0, +0.3, +0.7, +1.0, +1.3, +1.7, +2.0               |
| `--wb NAME`             | White balance on connect: auto, sunny, cloudy, tungsten, fluorescent                                                    |
| `--filter NAME`         | Camera filter on connect: none, faded, nimbus, tea, twilight, sapphire, vintage, b&w, newspaper                         |
| `--no-clock`            | Do not write the clock, photo size or any camera setting on connect                                                     |
| `--decoder NAME`        | H.264 decoder: auto (PyAV if installed, else ffmpeg on PATH), pyav, ffmpeg                                              |
| `--bitrate N`           | Try to set the camera's H.264 bitrate. Has no effect on this firmware (see below)                                       |

Values saved with the panel's Save settings button (`hua360_view.json`) are applied on the next start. The command line options override them.

### Control panel

| Control                                   | What it does                                                                                              |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Preview                                   | Small live picture of the current view. Drag = look around, mouse wheel = zoom                            |
| Exposure                                  | Exposure compensation slider, -2.0 to +2.0                                                                |
| White balance, Filter                     | Camera drop-down lists                                                                                    |
| Camera body (bottom): Logo / Raw / Blur  | Logo = the camera's watermark over its base, Raw = nothing, Blur = soft blur around straight down (live view, virtual camera and snapshots). Logo on/off restarts live view briefly, because the camera only applies it then |
| Blur size, Blur strength                  | How far from straight down the blur reaches (degrees) and how soft it is                                  |
| Live view                                 | 1920x960 or 1280x640                                                                                      |
| Zoom (FOV), Look left/right, Look down/up | The perspective view                                                                                      |
| Flat equirectangular view                 | Show the whole panorama instead of a perspective view                                                     |
| Camera mount: Yaw, Pitch, Roll, Presets 1-6, Reset | How the camera is physically rotated. Tune until the horizon is level, then Save settings        |
| Snapshot (full res)                       | Takes a JPEG into `snapshots\`; live view pauses for about 3 s. Tagged as a 360 photo that starts upright (the camera tags it as rolled 180 degrees, which makes VLC start upside down). In Blur mode the picture is blurred at the bottom and saved as `..._blur.jpg` |
| Start / Stop recording                    | Records the live stream into `recordings\`. Becomes an `.mp4` marked as 360 video (VLC and others render it on a globe, starting with your mount rotation as the pose) if `ffmpeg` is on PATH, otherwise stays a raw `.h264` (plays in VLC or ffplay, no 360 tag). Recordings are the camera's raw stream: no blur |
| Save frame PNG                            | Saves the raw camera frame and the current view as `raw_...png` and `view_...png`                         |
| Save settings                             | Writes `hua360_view.json`                                                                                 |
| OBS virtual camera                        | Switches the virtual camera output on or off                                                              |
| Large view window                         | Opens a bigger, view only window (mouse drag = look, wheel = zoom)                                        |
| Keep panel on top                         | Keeps the panel above other windows                                                                       |

The status line shows connection state, frames per second, data rate, the camera's temperature status and recent messages.
Camera changes (exposure, white balance, filter) apply immediately. Closing the panel closes the app: it stops live view and closes the camera first.

### OBS virtual camera

Run `hua360_panel.bat` (or `python hua360_view.py --vcam`) and choose "OBS Virtual Camera" in the video app. The OBS window does not need to be open.

## hua360_snap.py: still pictures

```
python hua360_snap.py [options]
```

Opens the camera, sets the clock and photo size, runs live view for a moment so auto exposure settles, takes the picture, downloads it and saves
it as `snapshots\snap_<size>_<date>_<time>_<n>.jpg`, then reports the real picture size. Live view carries on afterwards, so several shots in a row work without a replug.
Close the viewer first.

| Option               | Meaning                                                                                  |
| -------------------- | ---------------------------------------------------------------------------------------- |
| `--small`            | 3840x1920 instead of the default 5376x2688 (`--hires` is the old name for the default)   |
| `--count N`          | Number of shots in one session (default 1)                                               |
| `--orientation 0..3` | Rotation code sent with the shot (default 2). Try the others if the picture is rotated   |
| `--out FOLDER`       | Output folder (default `snapshots` next to the script)                                   |
| `--body logo\|raw\|blur` | Camera body at the bottom: logo = the camera's watermark, raw = nothing, blur = blurred copy (saved as `..._blur.jpg`). Default: leave the camera's watermark as it is |
| `--blur-size DEG`      | Blur: degrees around straight down that are blurred (default 22)                         |
| `--blur-strength N`    | Blur: strength 1 to 10 (default 4)                                                       |
| `--start-yaw DEG`, `--start-pitch DEG`, `--start-fov DEG` | 360 tag: where players should look (and how far zoomed in) when the picture opens |
| `--reset`            | Force a software replug before opening the camera                                        |
| `--live-res 9\|10`   | Live view size to run while shooting (default 10)                                        |
| `--settle SECONDS`   | Seconds of live view before the shot, for auto exposure (default 1.5)                    |
| `--no-live`          | Do not start live view before shooting (diagnostic)                                      |
| `-v`                 | Hex dump of the USB traffic                                                              |

## hua360.py: protocol library and raw stream

As a library, `Camera(UsbTransport())` provides `open(reset=False)` / `close()`, `firmware()`, `start_live(res)` / `stop_live()`, `get_frame()` (one H.264 access unit),
`snapshot(orientation)`, `thumbnail()`, `get_settings()` / `update_settings(**edits)` (read-modify-write of the 48-byte settings block, always with the PC's date and time),
`sync_clock()`, `thermal_raw()`, `get_bitrate()` / `set_bitrate()`. `UsbTransport.hard_reset()` is the software replug.
It also holds the name tables for exposure, white balance and filter values.

As a command line tool it streams the raw H.264 to a file or pipe:

```
python hua360.py --probe                                  # open, print firmware, close (safe first test)
python hua360.py --res 10 --out live.h264                 # stream to a file, Ctrl-C to stop
python hua360.py --res 10 --out - | ffplay -f h264 -i -  # watch it with ffplay
```

| Option                 | Meaning                                                        |
| ---------------------- | -------------------------------------------------------------- |
| `--probe`              | Open, print the firmware version and bitrate setting, close    |
| `--res 9\|10`          | Live view size (default 10)                                    |
| `--out FILE`           | Output file, or `-` for standard output (default `live.h264`)  |
| `--seconds N`          | Stop after N seconds (default: until Ctrl-C)                   |
| `--reset`              | Force a software replug before opening the camera              |
| `--bitrate N`          | Try to set the H.264 bitrate (no effect on this firmware)      |
| `-v`                   | Hex dump of the USB traffic                                    |

## hua360_meta.py: fix 360 tags on existing files

```
python hua360_meta.py photo snapshots\*.jpg          # make stills start the right way up (only the tag changes, pixels are untouched)
python hua360_meta.py video recordings\*.mp4          # mark recordings as 360 video
python hua360_meta.py video clip.mp4 --pitch -90      # with an initial pose (degrees)
```

`photo` options: `--roll` (default 0, the camera itself says 180), `--pitch`, and the start view `--start-yaw`, `--start-pitch`, `--start-fov`. `video` options: `--yaw`, `--pitch`, `--roll`. Wildcards are expanded by the tool,
and files are changed in place. Videos need to be `.mp4` (a raw `.h264` file cannot carry the tag).

## What the camera can and cannot do

- Live view: only 1920x960 (`--res 9`) and 1280x640 (`--res 10`) at about 30 fps. Every other resolution code falls back to 1280x640.
- Live-view bitrate cannot be changed over USB on firmware v1.3B00 (the write is accepted but ignored).
- Stills: 5376x2688 or 3840x1920 JPEG. Live view carries on afterwards, no replug needed.
- Temperature: only a coarse status (OK / hot / overheating / cold), shown in the panel. There is no value in degrees.
- Exposure, white balance, filter and the watermark are set through the same settings block as the clock, and work.
  Recording is the least tested feature.
- Idle power-off: about 5 minutes after the last program closes its session the camera switches itself off and disappears from USB. Only a physical unplug / replug brings it back.

## Troubleshooting

- "Camera not found": the scripts wait 10 s and then list what the PC can see. Check it is plugged in and bound to WinUSB, and that no other program (viewer, snapshot tool) is using it. If it has been idle for more than about 5 minutes, unplug and replug it (it powers itself off, see above).
- Camera stuck or no picture: start with `--reset` (software replug). If that does not help, unplug and replug it.
- The panel does not open: tkinter is missing from your Python install.
- No virtual camera in OBS or other apps: install OBS Studio, and `pip install pyvirtualcam`.

## Status

Prototype. Tested on firmware v1.3B00 on Windows with pyusb and libusb-package.
