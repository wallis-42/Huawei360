#!/usr/bin/env python3
"""Interactive 360 viewer + OBS virtual camera for the Huawei 360 camera.

Requires (Windows):  pip install pyusb libusb-package numpy opencv-python pyvirtualcam av
                     (av = PyAV decodes the H.264 stream; without it ffmpeg on PATH is used instead)
                     OBS Studio installed (virtual camera)

Run:   python hua360_view.py           (opens the control panel; everything is controlled from there)
       python hua360_view.py --vcam    (also start feeding the OBS Virtual Camera)
       python hua360_view.py --window  (also open the large view window; it can be toggled from the panel)

The control panel (hua360_panel.py, Tk) is the only way to control the viewer: camera settings (exposure, white balance,
filter, watermark, live-view size), view and mount, snapshots, recording, saving and the virtual camera switch.
Closing the panel quits. The large view window is view only (mouse drag = look, wheel = zoom).
"Mount" = how the camera is physically rotated; tune it until the horizon is level, then press Save settings.
Settings are stored in hua360_view.json next to this script.
The camera has no clock battery: the viewer sets its date/time on every connect (--no-clock to skip).
The camera reports only a coarse temperature status (OK / HOT / OVERHEAT / COLD), not degrees; it is shown in the HUD.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hua360  # noqa: E402
import hua360_img  # noqa: E402
import hua360_meta  # noqa: E402
from hua360_img import rot  # noqa: E402

# Spherical-video pose written into recordings = mount (yaw, pitch, roll) times these signs. Flip a sign here if VLC shows a
# recording rotated the wrong way round.
VIDEO_POSE_SIGN = (1, 1, 1)
SETTINGS = os.path.join(HERE, "hua360_view.json")
RES = {9: (1920, 960), 10: (1280, 640), 11: (3840, 1920)}

# (yaw, pitch, roll) in degrees applied to the camera's coordinate frame
PRESETS = {
    ord("1"): (0, 0, 0),
    ord("2"): (0, 90, 0),
    ord("3"): (0, -90, 0),
    ord("4"): (0, 0, 90),
    ord("5"): (0, 0, -90),
    ord("6"): (180, 0, 0),
}


# ---------------------------------------------------------------- maths
def _to_equirect_maps(rays, src_w, src_h):
    x, y, z = rays[..., 0], rays[..., 1], rays[..., 2]
    lon = np.arctan2(x, z)
    lat = np.arcsin(np.clip(y, -1, 1))
    mx = ((lon / (2 * math.pi) + 0.5) * src_w).astype(np.float32)
    my = ((0.5 - lat / math.pi) * src_h).astype(np.float32)
    return mx, my


def perspective_map(out_w, out_h, fov_deg, view_yaw, view_pitch, mount, src_w, src_h):
    """cv2.remap maps that render a rectilinear view out of an equirect frame."""
    f = 0.5 * out_w / math.tan(math.radians(fov_deg) / 2)
    xs = (np.arange(out_w) - (out_w - 1) / 2) / f
    ys = -(np.arange(out_h) - (out_h - 1) / 2) / f
    gx, gy = np.meshgrid(xs, ys)
    rays = np.stack([gx, gy, np.ones_like(gx)], axis=-1)
    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
    R = rot(*mount) @ rot(view_yaw, view_pitch, 0)
    return _to_equirect_maps(rays @ R.T, src_w, src_h)


def equirect_map(out_w, out_h, mount, src_w, src_h):
    """Re-orient the whole equirect frame by the mount rotation."""
    lon = (np.arange(out_w) + 0.5) / out_w * 2 * math.pi - math.pi
    lat = math.pi / 2 - (np.arange(out_h) + 0.5) / out_h * math.pi
    glon, glat = np.meshgrid(lon, lat)
    rays = np.stack([np.cos(glat) * np.sin(glon), np.sin(glat), np.cos(glat) * np.cos(glon)], axis=-1)
    return _to_equirect_maps(rays @ rot(*mount).T, src_w, src_h)


def render(frame, mx, my):
    return cv2.remap(frame, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


# ---------------------------------------------------------------- camera source
VERSION = "v3.5"


class AvDecoder:
    """In-process H.264 decoding with PyAV (pip install av). No pipes, no timestamps to get wrong."""

    name = "pyav"

    def __init__(self, push):
        import av
        try:
            av.logging.set_level(av.logging.ERROR)
        except Exception:
            pass
        self.av = av
        self.push = push
        self._new()

    def _new(self):
        self.codec = self.av.CodecContext.create("h264", "r")
        self.errors = 0

    def feed(self, au):
        try:
            for fr in self.codec.decode(self.av.Packet(au)):
                self.push(fr.to_ndarray(format="bgr24"))
            self.errors = 0
        except self.av.error.FFmpegError:
            self.errors += 1
            if self.errors > 30:  # stream got corrupted: start over, wait for next keyframe
                self._new()

    def close(self):
        pass


class FfmpegDecoder:
    """Fallback: pipe to the ffmpeg command line tool."""

    name = "ffmpeg"

    def __init__(self, push, w, h):
        self.push, self.w, self.h = push, w, h
        self.ff = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-probesize", "200000", "-f", "h264", "-framerate", "30",
             "-i", "pipe:0", "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)  # ffmpeg messages go to this console
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        n = self.w * self.h * 3
        while True:
            buf = self.ff.stdout.read(n)  # buffered reader: returns n bytes or EOF
            if not buf or len(buf) < n:
                return
            self.push(np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3))

    def feed(self, au):
        self.ff.stdin.write(au)
        self.ff.stdin.flush()

    def close(self):
        try:
            self.ff.stdin.close()
        except Exception:
            pass
        self.ff.kill()


def make_decoder(push, w, h, prefer):
    if prefer in ("auto", "pyav"):
        try:
            return AvDecoder(push)
        except ImportError:
            if prefer == "pyav":
                raise
            print("PyAV not installed (pip install av): falling back to the ffmpeg command line", file=sys.stderr)
    return FfmpegDecoder(push, w, h)


class Source(threading.Thread):
    """USB reader -> H.264 decoder -> latest BGR frame."""

    def __init__(self, res, decoder="auto", bitrate=0, photo_code=3, orientation=2, clock=True, logo=None, reset=False, initial=None):
        super().__init__(daemon=True)
        self.initial = dict(initial or {})   # evb / white_balance / filter to apply on connect
        self.pending = {}                    # edits queued from the UI, applied on the USB thread
        self.cam_settings = {}               # last known evb / white_balance / filter values
        self.res_changed = False
        self.ui = None      # shared UI state (set by main): body / blur settings used for stills
        self.start_view = None  # callable -> (yaw, pitch, fov) or None: where players should look when a still / recording opens
        self.mount = None   # mount list (set by main): written into recordings as the 360 pose
        self.rec_toggle = threading.Event()
        self.rec_file = None
        self.rec_path = None
        self.rec_started = False
        self.rec_frames = 0
        self.rec_t0 = 0.0
        self.rec_bytes = 0
        self.reset = reset
        self.logo = logo
        self.photo_code = photo_code
        self.orientation = orientation
        self.clock = clock
        self.thermal = None
        self.snap_req = threading.Event()
        self.snap_msg = ""
        self.snap_msg_t = 0.0
        self.snap_count = 0
        self.res = res
        self.bitrate = bitrate
        self.bitrate_info = ""
        self.usb_bytes = 0
        self.w, self.h = RES[res]
        self.prefer = decoder
        self.frame = None
        self.seq = 0
        self.status = "starting"
        self.decoder_name = "?"
        self.usb_frames = 0
        self.dec_frames = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def _push(self, img):
        with self.lock:
            self.frame = img
            self.seq += 1
            self.dec_frames += 1

    def run(self):
        sys.path.insert(0, HERE)
        import hua360
        while not self.stop.is_set():
            dec = None
            cam = None
            try:
                dec = make_decoder(self._push, self.w, self.h, self.prefer)
                self.decoder_name = dec.name
                self.status = "opening camera"
                cam = hua360.Camera(hua360.UsbTransport())
                cam.open(reset=self.reset)
                self.reset = True  # after any failure, reconnect with a full software replug
                self.status = "setting up"
                try:
                    if self.clock:
                        edits = {"photo_res": self.photo_code}
                        if self.logo is not None:
                            edits["logo"] = self.logo
                        edits.update(self.initial)
                        blk = cam.update_settings(**edits)  # also sets the camera clock
                        self._remember(blk)
                        print("camera clock set, photo size %s, watermark (block[39]) = %d" % (
                            hua360.Camera.PHOTO_RES.get(self.photo_code, "?"), blk[39]), file=sys.stderr)
                except Exception as e:
                    print("could not set camera clock/photo size: %s" % e, file=sys.stderr)
                try:
                    print("thermal raw: %s" % cam.thermal_raw(), file=sys.stderr)
                except Exception:
                    pass
                if self.bitrate:
                    old, new = cam.set_bitrate(self.bitrate)
                    self.bitrate_info = "br %d" % new
                    for line in cam.setting_log:
                        print("  " + line, file=sys.stderr)
                    print("bitrate %d -> %d (requested %d)" % (old, new, self.bitrate), file=sys.stderr)
                else:
                    try:
                        self.bitrate_info = "br %d" % cam.get_bitrate()
                    except Exception:
                        pass
                cam.start_live(self.res)
                self.status = "live"
                while not self.stop.is_set():
                    if cam.ka_failures >= 10:
                        raise hua360.CamError("keep-alive lost")
                    if self.snap_req.is_set():
                        self.snap_req.clear()
                        self._shoot(cam, hua360)
                    if self.pending:
                        self._apply_pending(cam)
                        if self.res_changed:
                            self.res_changed = False
                            if dec.name != "pyav":
                                raise hua360.CamError("live view size changed, restarting the decoder")
                    if self.rec_toggle.is_set():
                        self.rec_toggle.clear()
                        self._toggle_rec()
                    f = cam.get_frame()
                    if f is None:
                        time.sleep(0.005)
                        continue
                    if f[2] != self.thermal:
                        print("camera temperature status: %s (%d)" % (thermal_text(f[2]), f[2]), file=sys.stderr)
                        self.thermal = f[2]
                    if self.rec_file:
                        self._record(f[0])
                    dec.feed(f[0])
                    self.usb_frames += 1
                    self.usb_bytes += len(f[0])
            except Exception as e:  # reconnect loop
                self.status = "no signal (%s: %s)" % (type(e).__name__, e)
                print("source error: %s: %s" % (type(e).__name__, e), file=sys.stderr)
                time.sleep(2)
            finally:
                self._stop_rec()
                if cam:
                    try:
                        try:
                            cam.stop_live()
                        except Exception:
                            pass
                        cam.close()
                        cam.t.close()
                    except Exception:
                        pass
                if dec:
                    dec.close()

    def queue(self, **edits):
        """Queue camera setting edits (evb / white_balance / filter) from the UI thread."""
        with self.lock:
            self.pending.update(edits)

    def _remember(self, blk):
        for k in ("evb", "white_balance", "filter", "logo"):
            self.cam_settings[k] = blk[hua360.Camera.SETTING_OFFSETS[k]]

    def _apply_pending(self, cam):
        with self.lock:
            edits, self.pending = dict(self.pending), {}
        res = edits.pop("res", None)
        if res is not None and res != self.res:
            old, self.res = self.res, res
            self.w, self.h = RES[res]
            try:
                cam.stop_live()
                cam.start_live(res)
                self.res_changed = True
                self._say("live view %dx%d" % (self.w, self.h))
            except Exception:
                print("could not switch live view %d -> %d, reconnecting" % (old, res), file=sys.stderr)
                raise  # the reconnect loop restarts at the new size
        if not edits:
            return
        try:
            self._remember(cam.update_settings(**edits))
            print("camera settings now: %s" % self.cam_settings, file=sys.stderr)
        except Exception as e:
            print("could not change camera settings %s: %s: %s" % (edits, type(e).__name__, e), file=sys.stderr)
            return
        if "logo" in edits:  # the camera only applies the watermark when live view (re)starts
            cam.stop_live()
            cam.start_live(self.res)
            self.res_changed = True
            self._say("watermark %s (live view restarted)" % ("on" if edits["logo"] else "off"))

    def _toggle_rec(self):
        if self.rec_file:
            self._stop_rec()
            return
        folder = os.path.join(HERE, "recordings")
        os.makedirs(folder, exist_ok=True)
        self.rec_path = os.path.join(folder, "rec_res%d_%s.h264" % (self.res, time.strftime("%Y%m%d_%H%M%S")))
        self.rec_file = open(self.rec_path, "wb")
        self.rec_started = False
        self.rec_frames = self.rec_bytes = 0
        self.rec_t0 = time.time()
        self._say("recording started (waiting for a keyframe)")

    def _record(self, au):
        if not self.rec_started:
            if not has_idr(au):
                return
            self.rec_started = True
            self.rec_t0 = time.time()
        self.rec_file.write(au)
        self.rec_frames += 1
        self.rec_bytes += len(au)

    def _stop_rec(self):
        if not self.rec_file:
            return
        f, path = self.rec_file, self.rec_path
        self.rec_file = None
        f.close()
        secs = max(0.1, time.time() - self.rec_t0)
        fps = self.rec_frames / secs if self.rec_frames else 0
        self._say("recording stopped: %d frames, %.1f MB, %.1f s" % (self.rec_frames, self.rec_bytes / 1e6, secs))
        if self.rec_frames and fps > 1:
            pose = [s * m for s, m in zip(VIDEO_POSE_SIGN, self.mount or (0, 0, 0))]
            sv = self.start_view() if self.start_view else None
            if sv:  # start view = extra yaw / pitch on top of the mount levelling (video has no zoom field)
                pose[0] += VIDEO_POSE_SIGN[0] * sv[0]
                pose[1] += VIDEO_POSE_SIGN[1] * sv[1]
            pose = tuple(pose)
            threading.Thread(target=self._remux, args=(path, fps, pose), daemon=True).start()

    def _remux(self, path, fps, pose=(0, 0, 0)):
        """Best effort: wrap the raw H.264 in an .mp4 with ffmpeg (stream copy) and drop the raw file when that works."""
        import shutil
        import subprocess
        exe = shutil.which("ffmpeg")
        if not exe:
            print("recording kept as raw H.264 (%s); ffmpeg not found for .mp4 (VLC / ffplay play it)" % path, file=sys.stderr)
            return
        mp4 = path[:-5] + ".mp4"
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-framerate", "%.2f" % fps, "-f", "h264", "-i", path,
                            "-c", "copy", mp4], capture_output=True)
        if r.returncode == 0 and os.path.getsize(mp4) > 0:
            os.remove(path)
            try:
                tagged = hua360_meta.add_spherical(mp4, *pose)  # 360 flag: players render it on a globe
            except Exception as e:
                tagged = False
                print("could not add the 360 tags: %s: %s" % (type(e).__name__, e), file=sys.stderr)
            self._say("saved %s%s" % (os.path.basename(mp4), " (360 tagged)" if tagged else ""))
            print("recording saved %s (%.1f fps, 360 pose %s)" % (mp4, fps, pose if tagged else "not tagged"), file=sys.stderr)
        else:
            try:
                os.remove(mp4)
            except OSError:
                pass
            print("mp4 remux failed, raw file kept: %s" % path, file=sys.stderr)

    def _say(self, msg):
        self.snap_msg, self.snap_msg_t = msg, time.time()
        print(msg, file=sys.stderr)

    def _shoot(self, cam, hua360):
        """Take a full-resolution still on the USB thread (live view just carries on afterwards)."""
        self.status = "snapshot"
        t0 = time.time()
        try:
            data = cam.snapshot(self.orientation)
            ui = self.ui or {}
            suffix, note = "", ""
            sv = self.start_view() if self.start_view else None
            try:
                if ui.get("body") == "blur":
                    data = hua360_img.blur_still(data, ui.get("blur_deg", 22), ui.get("blur_str", 4), initial=sv)
                    suffix, note = "_blur", ", blurred"
                else:  # the camera tags its pictures as rolled 180 degrees, which makes VLC start upside down
                    data = hua360_meta.set_pano_pose(data, initial=sv)
            except Exception as e:
                print("could not post-process the snapshot (saved as the camera sent it): %s: %s" % (type(e).__name__, e), file=sys.stderr)
            folder = os.path.join(HERE, "snapshots")
            os.makedirs(folder, exist_ok=True)
            self.snap_count += 1
            name = "snap_%s_%s_%d%s.jpg" % (hua360.Camera.PHOTO_RES.get(self.photo_code, "full"),
                                            time.strftime("%Y%m%d_%H%M%S"), self.snap_count, suffix)
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(data)
            self.snap_msg = "saved %s (%.1f MB, %.1f s%s)" % (name, len(data) / 1e6, time.time() - t0, note)
            print("snapshot " + self.snap_msg, file=sys.stderr)
        except Exception as e:
            self.snap_msg = "snapshot failed: %s: %s" % (type(e).__name__, e)
            print(self.snap_msg, file=sys.stderr)
            if isinstance(e, hua360.CamError):
                pass
            else:
                raise
        finally:
            self.snap_msg_t = time.time()
            self.status = "live"

    def latest(self):
        with self.lock:
            return self.frame, self.seq


def fit(img, w, h):
    """Letterbox img into a w x h canvas (keeps the aspect ratio) so the virtual camera size never changes."""
    ih, iw = img.shape[:2]
    if (iw, ih) == (w, h):
        return img
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
    small = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    canvas = np.zeros((h, w, 3), np.uint8)
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = small
    return canvas


def has_idr(au):
    """True if this Annex-B access unit contains an IDR slice (NAL type 5), i.e. a clean place to start a recording."""
    i, n = 0, len(au)
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= n:
            return False
        if au[i + 3] & 0x1F == 5:
            return True
        i += 3


def cam_line(src):
    cs = src.cam_settings
    if not cs:
        return "camera settings: unknown"
    s = "ev %s (z/x) | wb %s (w) | filter %s (c)" % (
        hua360.EV_NAMES.get(cs.get("evb"), "?"),
        hua360.WB_NAMES[cs["white_balance"]] if cs.get("white_balance", 99) < len(hua360.WB_NAMES) else "?",
        hua360.FILTER_NAMES[cs["filter"]] if cs.get("filter", 99) < len(hua360.FILTER_NAMES) else "?")
    if src.rec_file:
        s += " | REC %d s" % (time.time() - src.rec_t0)
    return s


def thermal_text(t):
    return {0: "temp OK", 1: "TEMP HOT", 2: "OVERHEATING", 3: "TEMP COLD"}.get(t, "temp ?" if t is None else "temp code %d" % t)


def draw_hud(img, lines):
    """Yellow text on a solid black box in the top-left corner (scales with image size)."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    h, w = img.shape[:2]
    scale = max(0.5, w / 1800.0)
    thick = max(1, int(round(scale * 1.6)))
    pad = int(8 * scale) + 2
    sizes = [cv2.getTextSize(t, font, scale, thick)[0] for t in lines]
    line_h = max(s[1] for s in sizes) + pad
    box_w = min(w, max(s[0] for s in sizes) + 2 * pad)
    cv2.rectangle(img, (0, 0), (box_w, line_h * len(lines) + pad), (0, 0, 0), -1)
    for i, t in enumerate(lines):
        cv2.putText(img, t, (pad, pad + (i + 1) * line_h - pad // 2), font, scale, (0, 255, 255), thick, cv2.LINE_AA)


# ---------------------------------------------------------------- app
def load_settings():
    d = {"mount": [0, 0, 0], "fov": 90, "yaw": 0, "pitch": 0}
    try:
        d.update(json.load(open(SETTINGS)))
    except Exception:
        pass
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", type=int, default=10, choices=[9, 10], help="live view: 9 = 1920x960, 10 = 1280x640 (the only two sizes the camera has)")
    ap.add_argument("--size", default="1280x720", help="output size WxH of perspective view / virtual camera")
    ap.add_argument("--decoder", default="auto", choices=["auto", "pyav", "ffmpeg"], help="H.264 decoder (auto = PyAV if installed)")
    ap.add_argument("--bitrate", type=int, default=0, help="set camera H.264 bitrate before streaming (original app: 8 at res 10, 16 at res 9)")
    ap.add_argument("--photo", default="big", choices=["big", "small"], help="snapshot size: big = 5376x2688, small = 3840x1920")
    ap.add_argument("--orientation", type=int, default=2, choices=[0, 1, 2, 3], help="snapshot rotation code")
    ap.add_argument("--body", choices=["logo", "raw", "blur"], default=None,
                    help="how the camera body at the bottom of the picture is shown: logo = camera watermark, raw = nothing, blur = blurred (default: saved setting, else leave as is)")
    ap.add_argument("--blur-size", type=float, default=None, help="blur: degrees around straight down that are blurred (default 22)")
    ap.add_argument("--blur-strength", type=float, default=None, help="blur: strength 1..10 (default 4)")
    ap.add_argument("--start-yaw", type=float, default=None, help="360 tag: heading players should start looking at, degrees (stills and recordings)")
    ap.add_argument("--start-pitch", type=float, default=None, help="360 tag: start up/down angle, degrees")
    ap.add_argument("--start-fov", type=float, default=None, help="360 tag: start zoom as horizontal field of view, degrees (stills only; video has no such field)")
    ap.add_argument("--start-from-view", action="store_true", help="360 tag: start where the live view is looking when the still / recording is taken (overrides --start-*)")
    ap.add_argument("--reset", action="store_true", help="force a software replug (USB reset) when the script starts; the viewer always does this when it reconnects after an error")
    ap.add_argument("--no-clock", action="store_true", help="do not set the camera clock / photo size on connect")
    ap.add_argument("--ev", choices=[hua360.EV_NAMES[x] for x in hua360.EV_ORDER], default=None, help="exposure compensation to set on connect")
    ap.add_argument("--wb", choices=hua360.WB_NAMES, default=None, help="white balance to set on connect")
    ap.add_argument("--filter", choices=hua360.FILTER_NAMES, default=None, help="camera filter to set on connect")
    ap.add_argument("--window", action="store_true", help="also open the large view window at start (toggle it from the panel)")
    ap.add_argument("--vcam", action="store_true", help="start with virtual camera output enabled")
    a = ap.parse_args(argv)
    ow, oh = map(int, a.size.lower().split("x"))

    st = load_settings()
    mount = list(map(float, st["mount"]))
    view = {"yaw": float(st["yaw"]), "pitch": float(st["pitch"]), "fov": float(st["fov"])}
    vcam = None
    try:
        import tkinter  # noqa: F401
    except Exception as e:
        print("The control panel needs tkinter (%s). Install Python from python.org with tcl/tk, or add it to your install." % e, file=sys.stderr)
        return 2
    ui = {"flat": False, "vcam": a.vcam, "window": a.window, "quit": False, "actions": [], "out": None, "fps": 0.0, "mbps": 0.0,
          "body": None, "blur_deg": 22.0, "blur_str": 4.0}

    print("hua360_view %s" % VERSION, file=sys.stderr)
    initial = {}
    ev_by_name = {n: k for k, n in hua360.EV_NAMES.items()}
    for key, cli, saved, table in (("evb", a.ev, st.get("ev"), ev_by_name),
                                   ("white_balance", a.wb, st.get("wb"), {n: i for i, n in enumerate(hua360.WB_NAMES)}),
                                   ("filter", a.filter, st.get("filter"), {n: i for i, n in enumerate(hua360.FILTER_NAMES)})):
        name = cli if cli is not None else saved
        if name in table:
            initial[key] = table[name]
    body = a.body if a.body is not None else st.get("body")
    if body is None and st.get("logo") in ("on", "off"):  # settings file from an older version
        body = "logo" if st["logo"] == "on" else "raw"
    ui["body"] = body if body in ("logo", "raw", "blur") else None
    ui["blur_deg"] = float(a.blur_size if a.blur_size is not None else st.get("blur_deg", 22.0))
    ui["blur_str"] = float(a.blur_strength if a.blur_strength is not None else st.get("blur_str", 4.0))
    src = Source(a.res, a.decoder, a.bitrate, 3 if a.photo == "big" else 4, a.orientation, not a.no_clock,
                 {"logo": 1, "raw": 0, "blur": 0}.get(ui["body"]), a.reset, initial)
    src.ui, src.mount = ui, mount
    if a.start_from_view:
        src.start_view = lambda: (view["yaw"], view["pitch"], view["fov"])
    elif a.start_yaw is not None or a.start_pitch is not None or a.start_fov is not None:
        _sv = (a.start_yaw or 0.0, a.start_pitch or 0.0, a.start_fov)
        src.start_view = lambda: _sv
    src.start()
    import hua360_panel
    win = "Huawei 360 view (drag = look, wheel = zoom)"
    drag = {"on": False, "x": 0, "y": 0}

    def mouse(ev, x, y, flags, _):
        if ev == cv2.EVENT_LBUTTONDOWN:
            drag.update(on=True, x=x, y=y)
        elif ev == cv2.EVENT_LBUTTONUP:
            drag["on"] = False
        elif ev == cv2.EVENT_MOUSEMOVE and drag["on"]:
            k = view["fov"] / ow * 1.0
            view["yaw"] -= (x - drag["x"]) * k
            view["pitch"] = max(-89, min(89, view["pitch"] + (y - drag["y"]) * k))
            drag.update(x=x, y=y)
        elif ev == cv2.EVENT_MOUSEWHEEL:
            view["fov"] = max(20, min(140, view["fov"] - (10 if flags > 0 else -10)))

    def run_loop():
        """Render / virtual camera loop, on its own thread; the Tk panel owns the main thread."""
        vcam = None
        win_open = False
        try:
            cache = {"key": None, "maps": None}
            wcache = {"key": None, "w": None}
            last_seq = -1
            out = None
            t_fps = time.time()
            shown = 0
            fps = 0.0
            mbps = 0.0
            last_bytes = 0
            while True:
                frame, seq = src.latest()
                if frame is not None:
                    h, w = frame.shape[:2]
                    key = (ui["flat"], round(view["yaw"], 1), round(view["pitch"], 1), view["fov"], tuple(mount), w, h,
                       ui["body"] == "blur", ui["blur_deg"], ui["blur_str"])
                    changed = key != cache["key"]
                    if changed:
                        if ui["flat"]:
                            cache["maps"] = equirect_map(w, h, mount, w, h)
                        else:
                            cache["maps"] = perspective_map(ow, oh, view["fov"], view["yaw"], view["pitch"], mount, w, h)
                        cache["key"] = key
                    if seq != last_seq or changed or out is None:
                        src_frame = frame
                        if ui["body"] == "blur":  # soften the camera body at the bottom before projecting
                            wk = (tuple(mount), ui["blur_deg"], w, h)
                            if wk != wcache["key"]:
                                wcache["key"], wcache["w"] = wk, hua360_img.nadir_weight(w, h, mount, ui["blur_deg"])
                            src_frame = hua360_img.blur_with_weight(frame, wcache["w"], ui["blur_str"])
                        out = render(src_frame, *cache["maps"])
                        last_seq = seq
                        shown += 1
                ui["out"] = out
                if out is None:
                    disp = np.zeros((oh // 2, ow // 2, 3), np.uint8)
                else:
                    disp = out.copy()
                now = time.time()
                if now - t_fps >= 1:
                    mbps = (src.usb_bytes - last_bytes) * 8 / (now - t_fps) / 1e6
                    last_bytes = src.usb_bytes
                    fps, shown, t_fps = shown / (now - t_fps), 0, now
                    ui["fps"], ui["mbps"] = fps, mbps

                if ui["vcam"] and out is not None:
                    if vcam is None:
                        try:
                            import pyvirtualcam
                            vcam = pyvirtualcam.Camera(ow, oh, 30, fmt=pyvirtualcam.PixelFormat.BGR)
                            print("virtual camera: %s (%dx%d)" % (vcam.device, ow, oh), file=sys.stderr)
                        except Exception as e:
                            print("virtual camera unavailable: %s" % e, file=sys.stderr)
                            ui["vcam"] = False
                            vcam = None
                    if vcam is not None:
                        try:
                            vcam.send(fit(out, ow, oh))  # fixed size, so flat / perspective switching never changes the camera
                        except Exception as e:
                            print("virtual camera send failed (%s: %s), switching it off" % (type(e).__name__, e), file=sys.stderr)
                            ui["vcam"] = False
                            try:
                                vcam.close()
                            except Exception:
                                pass
                            vcam = None
                elif vcam is not None and not ui["vcam"]:
                    try:
                        vcam.close()
                    except Exception:
                        pass
                    vcam = None

                srcsize = "%dx%d" % (frame.shape[1], frame.shape[0]) if frame is not None else "-"
                draw_hud(disp, [
                    "%s | %s | src %s | usb %d dec %d | %.1f Mb/s %s" % (
                        src.status, src.decoder_name, srcsize, src.usb_frames, src.dec_frames, mbps, src.bitrate_info),
                    "%s | mount y%.0f p%.0f r%.0f | fov %.0f | %.0f fps%s" % (
                        "flat" if ui["flat"] else "view", mount[0], mount[1], mount[2], view["fov"], fps,
                        " | VCAM" if ui["vcam"] else "")] + (
                    ["%s" % thermal_text(src.thermal)] if not src.snap_msg or time.time() - src.snap_msg_t > 6
                    else [src.snap_msg]) + [cam_line(src)])
                # optional large view window: view only (mouse = look / zoom), everything else is on the panel
                if ui["window"] and not win_open:
                    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
                    cv2.resizeWindow(win, ow // 2 + 200, oh // 2 + 100)
                    cv2.setMouseCallback(win, mouse)
                    win_open = True
                elif not ui["window"] and win_open:
                    cv2.destroyWindow(win)
                    cv2.waitKey(1)
                    win_open = False
                if win_open:
                    cv2.imshow(win, disp)
                    cv2.waitKey(10)
                    if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:  # closed with the X
                        ui["window"] = False
                        win_open = False
                else:
                    time.sleep(0.01)

                if ui["quit"]:
                    break
                action = ui["actions"].pop(0) if ui["actions"] else None
                if action == "snapshot":
                    src.snap_req.set()
                    src.snap_msg, src.snap_msg_t = "taking snapshot...", time.time() + 30
                elif action == "record":
                    src.rec_toggle.set()
                elif action == "save_frame" and frame is not None:
                    tag = "res%d_%dx%d_%d" % (src.res, frame.shape[1], frame.shape[0], int(time.time()))
                    cv2.imwrite(os.path.join(HERE, "raw_%s.png" % tag), frame)
                    if out is not None:
                        cv2.imwrite(os.path.join(HERE, "view_%s.png" % tag), out)
                    print("saved raw_%s.png and view_%s.png in %s" % (tag, tag, HERE), file=sys.stderr)
                elif action == "reset_mount":
                    mount[:] = [0, 0, 0]
                elif isinstance(action, tuple) and action[0] == "preset" and action[1] in PRESETS:
                    mount[:] = PRESETS[action[1]]
                elif action == "save_settings":
                    cs = src.cam_settings
                    extra = {}
                    if cs:
                        extra = {"ev": hua360.EV_NAMES.get(cs.get("evb"), "0"),
                                 "wb": hua360.WB_NAMES[cs["white_balance"]] if cs.get("white_balance", 99) < len(hua360.WB_NAMES) else "auto",
                                 "filter": hua360.FILTER_NAMES[cs["filter"]] if cs.get("filter", 99) < len(hua360.FILTER_NAMES) else "none",
                                 "body": ui["body"] or ("logo" if cs.get("logo") else "raw"),
                         "blur_deg": ui["blur_deg"], "blur_str": ui["blur_str"]}
                    json.dump(dict({"mount": mount, "fov": view["fov"], "yaw": view["yaw"], "pitch": view["pitch"]}, **extra),
                              open(SETTINGS, "w"))
                    print("saved %s" % SETTINGS, file=sys.stderr)

        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            ui["quit"] = True  # whatever ended the loop, the panel (and so the app) closes
            if vcam is not None:
                try:
                    vcam.close()
                except Exception:
                    pass
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    render_thread = threading.Thread(target=run_loop, daemon=True, name="render")
    render_thread.start()
    hua360_panel.run_panel(ui, view, mount, src, PRESETS, ow, oh)  # blocks until the panel is closed
    ui["quit"] = True
    render_thread.join(5)
    src.stop.set()
    src.join(4)  # let the camera thread stop live view and close communication
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
