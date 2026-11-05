#!/usr/bin/env python3
"""Take still pictures with the Huawei 360 camera and save them as JPEGs.

    python hua360_snap.py                  # 5376x2688 photo (largest)
    python hua360_snap.py --small          # 3840x1920 photo
    python hua360_snap.py --orientation 0  # try 0..3 if the picture is rotated
    python hua360_snap.py --count 3        # three shots in one session (tests whether the camera needs a replug)
    python hua360_snap.py -v               # hex-dump the USB traffic

The camera has no battery-backed clock, so the PC's local date/time is written to it first (like the phone app does).
Close hua360_view.py first: only one program can talk to the camera at a time.
"""
import argparse
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hua360  # noqa: E402
import hua360_img  # noqa: E402
import hua360_meta  # noqa: E402


def log(msg):
    print(msg, file=sys.stderr)


def pull(cam, secs):
    """Poll live-view frames for `secs`; returns (frames, error text or None)."""
    t0, n = time.time(), 0
    try:
        while time.time() - t0 < secs:
            if cam.get_frame() is not None:
                n += 1
    except Exception as e:
        return n, "%s: %s" % (type(e).__name__, e)
    return n, None


def resume_live(cam, live_res, photo_code):
    """After a shot, find out what the camera needs to stream again. Tries the cheapest recovery first."""
    n, err = pull(cam, 1.5)
    if n:
        return "A (just keep polling frames)", n
    log("  A failed: %s" % (err or "no frames"))
    try:
        cam.t.reset()
        cam.stop_live()
        cam.start_live(live_res)
        n, err = pull(cam, 1.5)
        if n:
            return "B (stop + start live view)", n
        log("  B failed: %s" % (err or "no frames"))
    except Exception as e:
        log("  B failed: %s: %s" % (type(e).__name__, e))
    try:
        cam._stop_keepalive()
        cam.open()  # USB reset + open communication
        cam.update_settings(photo_res=photo_code)
        cam.start_live(live_res)
        n, err = pull(cam, 1.5)
        if n:
            return "C (reopen + settings + start live view)", n
        log("  C failed: %s" % (err or "no frames"))
    except Exception as e:
        log("  C failed: %s: %s" % (type(e).__name__, e))
    return None, 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--small", action="store_true", help="3840x1920 instead of 5376x2688")
    ap.add_argument("--hires", action="store_true", help="(default now) 5376x2688")
    ap.add_argument("--orientation", type=int, default=2, choices=[0, 1, 2, 3], help="rotation code the app takes from the phone sensor")
    ap.add_argument("--out", default=os.path.join(HERE, "snapshots"), help="folder for the pictures")
    ap.add_argument("--live-res", type=int, default=10, choices=[9, 10], help="live view resolution to run while shooting")
    ap.add_argument("--settle", type=float, default=1.5, help="seconds of live view before the shot (auto exposure)")
    ap.add_argument("--reset", action="store_true", help="force a software replug (USB reset) before opening the camera")
    ap.add_argument("--body", choices=["logo", "raw", "blur"], default=None,
                    help="camera body at the bottom of the picture: logo = camera watermark, raw = nothing, blur = blurred (default: leave the camera's watermark setting as is)")
    ap.add_argument("--blur-size", type=float, default=22.0, help="blur: degrees around straight down that are blurred (default 22)")
    ap.add_argument("--blur-strength", type=float, default=4.0, help="blur: strength 1..10 (default 4)")
    ap.add_argument("--start-yaw", type=float, default=None, help="360 tag: heading players should start looking at, degrees")
    ap.add_argument("--start-pitch", type=float, default=0.0, help="360 tag: start up/down angle, degrees")
    ap.add_argument("--start-fov", type=float, default=None, help="360 tag: start zoom as horizontal field of view, degrees")
    ap.add_argument("--count", type=int, default=1, help="number of shots in this session")
    ap.add_argument("--no-live", action="store_true", help="do not start live view before shooting (diagnostic)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    os.makedirs(a.out, exist_ok=True)
    code = 4 if a.small else 3  # 3 = 5376x2688, 4 = 3840x1920 (tested)
    cam = hua360.Camera(hua360.UsbTransport(), a.verbose)
    try:
        cam.open(reset=a.reset)
        edits = {"photo_res": code}
        if a.body:
            edits["logo"] = 1 if a.body == "logo" else 0
        blk = cam.update_settings(**edits)  # also sets the camera clock
        log("watermark setting (block[39]) = %d" % blk[39])
        log("photo size setting: %s (requested %s)" % (hua360.Camera.PHOTO_RES.get(blk[2], blk[2]), hua360.Camera.PHOTO_RES[code]))
        log("camera clock now %d-%02d-%02d %02d:%02d:%02d" % (
            int.from_bytes(blk[10:12], "little"), blk[12], blk[13], blk[14], blk[15], blk[16]))
        live = False
        if not a.no_live:
            cam.start_live(a.live_res)
            live = True
            n, err = pull(cam, a.settle)
            log("live view ran %d frames%s" % (n, "" if not err else " (error: %s)" % err))
        for shot in range(1, a.count + 1):
            log("shot %d/%d ..." % (shot, a.count))
            t0 = time.time()
            data = cam.snapshot(a.orientation)
            ext = "jpg" if data[:2] == b"\xff\xd8" else "bin"
            suffix = ""
            sv = None if a.start_yaw is None and a.start_fov is None and not a.start_pitch else (a.start_yaw or 0.0, a.start_pitch, a.start_fov)
            if ext == "jpg":
                try:
                    if a.body == "blur":
                        data = hua360_img.blur_still(data, a.blur_size, a.blur_strength, initial=sv)
                        suffix = "_blur"
                    else:  # the camera tags its pictures as rolled 180 degrees, which makes VLC start upside down
                        data = hua360_meta.set_pano_pose(data, initial=sv)
                except Exception as e:
                    log("could not post-process the picture (saved as the camera sent it): %s: %s" % (type(e).__name__, e))
            name = "snap_%s_%s_%d%s.%s" % (hua360.Camera.PHOTO_RES.get(code, "photo%d" % code),
                                           datetime.datetime.now().strftime("%Y%m%d_%H%M%S"), shot, suffix, ext)
            path = os.path.join(a.out, name)
            with open(path, "wb") as f:
                f.write(data)
            log("saved %s (%d bytes, %.1f s)%s" % (path, len(data), time.time() - t0,
                "" if ext == "jpg" else " - not a JPEG, first bytes: " + data[:16].hex(" ")))
            try:
                import cv2
                import numpy as np
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    log("picture is %dx%d pixels" % (img.shape[1], img.shape[0]))
            except ImportError:
                pass
            if live:
                how, n = resume_live(cam, a.live_res, code)
                if how:
                    log("live view resumed after shot %d: strategy %s, %d frames" % (shot, how, n))
                else:
                    log("live view did NOT resume after shot %d: the camera needs a replug after a shot" % shot)
                    break
        if live:
            try:
                cam.stop_live()
            except Exception as e:
                log("stop live view: %s" % e)
    finally:
        ok = cam.close()
        log("close communication: %s" % ("ok" if ok else "no reply"))
        cam.t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
