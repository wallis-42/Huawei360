#!/usr/bin/env python3
"""Hold the Huawei 360 camera awake without streaming, and log when (if ever) it drops off the USB bus.

Diagnostic for the camera's idle power-off: it switches itself off (and drops off USB) about 5 minutes after the last session
was closed. While a session is held open with keep-alives it stays on. This opens communication, then only sends the normal
keep-alive (about every 0.5 s) and asks for the temperature status every 30 s. The LED stays off, no video is read.

  python hua360_hold.py              # run until Ctrl+C or until the camera drops
  python hua360_hold.py --minutes 15

Close it before starting hua360_view.py / hua360_snap.py: only one program can talk to the camera at a time.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hua360  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=0, help="stop after this many minutes (default: run until Ctrl+C)")
    ap.add_argument("--reset", action="store_true", help="force a software replug (USB reset) first")
    ap.add_argument("--every", type=float, default=30.0, help="seconds between status checks (default 30)")
    a = ap.parse_args(argv)

    cam = hua360.Camera(hua360.UsbTransport())
    cam.open(reset=a.reset)
    t0 = time.time()
    print("camera open, holding it awake (Ctrl+C to stop). Started %s" % time.strftime("%H:%M:%S"), file=sys.stderr)
    try:
        while True:
            time.sleep(a.every)
            el = time.time() - t0
            try:
                st = cam.command([0x7A, 0x03, 0x30])
                print("%s  %5.1f min  alive (status reply %s, keep-alive failures %d)" % (
                    time.strftime("%H:%M:%S"), el / 60, bytes(st[:8]).hex(" ") if st else "none", cam.ka_failures), file=sys.stderr)
            except Exception as e:
                print("%s  %5.1f min  CAMERA STOPPED ANSWERING: %s: %s" % (time.strftime("%H:%M:%S"), el / 60, type(e).__name__, e),
                      file=sys.stderr)
                break
            if a.minutes and el >= a.minutes * 60:
                print("reached %.1f minutes: the camera stayed up" % a.minutes, file=sys.stderr)
                break
    except KeyboardInterrupt:
        print("stopped by you after %.1f minutes" % ((time.time() - t0) / 60), file=sys.stderr)
    finally:
        try:
            cam.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
