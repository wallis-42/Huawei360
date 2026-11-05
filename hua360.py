#!/usr/bin/env python3
"""Huawei 360 Camera (VID 12D1 / PID 109B) WinUSB prototype.

Protocol reverse-engineered from com.huawei.cvIntl60 1.9.12.20 (see ../huawei360_usb_protocol_notes.md).

Setup (Windows, camera already bound to WinUSB):
    pip install pyusb libusb-package

Usage:
    python hua360.py --probe                      # open, read firmware string, close (safe first test)
    python hua360.py --res 10 --out live.h264     # stream 1280x640 H.264 to a file, Ctrl-C to stop
    python hua360.py --res 10 --out - | ffplay -f h264 -i -
    python hua360.py -v ...                       # hex-dump every command/response to stderr
"""
import argparse
import struct
import sys
import threading
import time

VID, PID = 0x12D1, 0x109B
EP_IN, EP_OUT = 0x82, 0x03
INTERFACE = 0
CHUNK = 16384
LONG_BUF, SHORT_BUF = 65536, 64
RES = {9: (1920, 960), 10: (1280, 640), 11: (3840, 1920)}

# Settings-block values as the app's menus map them (a.c.b + strings.xml). Untested on hardware until noted otherwise.
WB_NAMES = ["auto", "sunny", "cloudy", "tungsten", "fluorescent"]                      # block[7] = index
FILTER_NAMES = ["none", "faded", "nimbus", "tea", "twilight", "sapphire", "vintage", "b&w", "newspaper"]  # block[32] = index
EV_ORDER = [1, 2, 3, 4, 5, 6, 0, 7, 8, 9, 10, 11, 12]                                    # block[6] values, darkest -> brightest
EV_NAMES = {1: "-2.0", 2: "-1.7", 3: "-1.3", 4: "-1.0", 5: "-0.7", 6: "-0.3", 0: "0",
            7: "+0.3", 8: "+0.7", 9: "+1.0", 10: "+1.3", 11: "+1.7", 12: "+2.0"}


class CamError(Exception):
    pass


class UsbTransport:
    """Thin pyusb wrapper. Anything with the same four methods can replace it (used by the self-test)."""

    def __init__(self, wait=10.0):
        """wait = seconds to keep looking for the camera (it can take a few seconds to show up after being idle or asleep)."""
        import usb.core
        import usb.util
        backend = None
        try:
            import libusb_package
            import usb.backend.libusb1 as l1
            backend = l1.get_backend(find_library=libusb_package.find_library)
        except ImportError as e:
            print("libusb-package not available (%s); using the system libusb" % e, file=sys.stderr)
        t0 = time.time()
        dev = None
        shown = False
        while True:
            try:
                dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
            except Exception as e:  # e.g. NoBackendError
                raise CamError("USB search failed: %s: %s (is libusb-package installed?)" % (type(e).__name__, e))
            if dev is not None or time.time() - t0 >= wait:
                break
            if not shown:
                print("camera %04X:%04X not visible yet, waiting up to %.0f s ..." % (VID, PID, wait), file=sys.stderr)
                shown = True
            time.sleep(0.5)
        if dev is None:
            self._report_missing(usb.core, backend)
            raise CamError("Camera %04X:%04X not found after %.0f s (plugged in? WinUSB bound?)" % (VID, PID, time.time() - t0))
        if shown:
            print("camera appeared after %.1f s" % (time.time() - t0), file=sys.stderr)
        self._usb, self._util, self.dev = usb.core, usb.util, dev
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured
        try:
            usb.util.claim_interface(dev, INTERFACE)
        except (NotImplementedError, usb.core.USBError) as e:
            raise CamError("camera found but its interface could not be claimed (%s: %s). Usually the device is not bound to WinUSB "
                           "(Windows may have swapped the driver after idle/sleep: check Device Manager, or reinstall with Zadig) "
                           "or another program still holds it." % (type(e).__name__, e))

    @staticmethod
    def _report_missing(usb_core, backend):
        """Say what the PC does see, to tell 'camera asleep / not enumerated' from 'driver not WinUSB' from 'other mode'."""
        print("--- camera not found: USB devices libusb can see ---", file=sys.stderr)
        try:
            found = list(usb_core.find(find_all=True, backend=backend))
        except Exception as e:
            print("  could not list devices: %s: %s" % (type(e).__name__, e), file=sys.stderr)
            return
        for d in found:
            mark = "   <-- Huawei" if d.idVendor == VID else ""
            if d.idVendor == VID or d.idVendor in (0x18D1,):
                print("  %04X:%04X%s" % (d.idVendor, d.idProduct, mark), file=sys.stderr)
        print("  (%d devices in total; only Huawei entries are listed)" % len(found), file=sys.stderr)
        if not any(d.idVendor == VID for d in found):
            print("  No Huawei device is visible. Likely causes: camera asleep / powered down (press its button or replug), cable or port\n"
                  "  power saving (Windows USB selective suspend), or the device is bound to a driver other than WinUSB (check Device Manager:\n"
                  "  it should be under 'Universal Serial Bus devices'). A different Huawei PID would mean the camera is in another mode.", file=sys.stderr)

    def bulk_out(self, data, timeout):
        return self.dev.write(EP_OUT, data, timeout)

    def bulk_in(self, size, timeout):
        return bytes(self.dev.read(EP_IN, size, timeout))

    def reset(self):
        # Bulk-Only Mass Storage Reset: bmRequestType 0x21, bRequest 0xFF, wIndex = interface
        self.dev.ctrl_transfer(0x21, 0xFF, 0, INTERFACE, None, 2000)

    def hard_reset(self):
        """Software equivalent of unplugging and replugging: clear endpoint halts, Bulk-Only reset,
        libusb device reset, then drain any stale bytes left in the camera's IN pipe. Each step is best-effort."""
        for ep in (EP_IN, EP_OUT):
            try:
                self.dev.clear_halt(ep)
            except Exception as e:
                print("clear_halt %02x: %s" % (ep, e), file=sys.stderr)
        try:
            self.reset()
        except Exception as e:
            print("bulk-only reset: %s" % e, file=sys.stderr)
        try:
            self.dev.reset()
        except Exception as e:
            print("device reset: %s" % e, file=sys.stderr)
        for _ in range(8):  # drain stale data
            try:
                if not bytes(self.dev.read(EP_IN, 16384, 150)):
                    break
            except Exception:
                break

    def close(self):
        try:
            self._util.release_interface(self.dev, INTERFACE)
            self._util.dispose_resources(self.dev)
        except Exception:
            pass


class Camera:
    def __init__(self, transport, verbose=False):
        self.t = transport
        self.verbose = verbose
        self.lock = threading.Lock()
        self.tag = 0x1000
        self._ka_thread = None
        self._ka_stop = threading.Event()
        self.ka_failures = 0
        self.last_csw = None
        self.setting_log = []

    # ---- logging -------------------------------------------------------
    def _log(self, title, data, limit=64):
        if self.verbose:
            h = " ".join("%02x" % b for b in data[:limit])
            more = " ..(%d bytes)" % len(data) if len(data) > limit else ""
            print("[%s] %s%s" % (title, h, more), file=sys.stderr)

    # ---- bulk-only framing --------------------------------------------
    def _next_tag(self):
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        return self.tag

    @staticmethod
    def _cbw(cdb, length, read, tag):
        cdb = bytes(cdb).ljust(16, b"\0")[:16]
        return (b"USBC" + struct.pack(">I", tag) + struct.pack("<I", length)
                + bytes([0x80 if read else 0x00, 0, 16]) + cdb)

    @staticmethod
    def _is_csw(buf, tag):
        return (len(buf) >= 13 and buf[-13:-9] == b"USBS"
                and buf[-9:-5] == struct.pack(">I", tag))

    def command(self, cdb, short=False, timeout=2000):
        """Send a 16-byte vendor CDB with a device->host data phase; returns the data bytes."""
        want = SHORT_BUF if short else LONG_BUF
        with self.lock:
            tag = self._next_tag()
            cbw = self._cbw(cdb, want, True, tag)
            self._log("CBW>", cbw, 31)
            self.t.bulk_out(cbw, timeout)
            data = bytearray()
            empty = 0
            while True:
                chunk = self.t.bulk_in(CHUNK, timeout)
                if not chunk:
                    empty += 1
                    if empty > 3:
                        raise CamError("empty reads from device")
                    continue
                if self._is_csw(chunk, tag):
                    data += chunk[:-13]
                    self.last_csw = chunk[-1]
                    break
                data += chunk
                if len(data) > want:
                    raise CamError("response larger than %d bytes" % want)
            self._log("RSP<", data)
            return bytes(data)

    def write_command(self, cdb, payload, timeout=2000):
        """Host->device data phase (used by the 0x7B settings commands)."""
        with self.lock:
            tag = self._next_tag()
            self.t.bulk_out(self._cbw(cdb, len(payload), False, tag), timeout)
            for i in range(0, len(payload), CHUNK):
                self.t.bulk_out(bytes(payload[i:i + CHUNK]), timeout)
            csw = self.t.bulk_in(CHUNK, timeout)
            if not self._is_csw(csw, tag):
                raise CamError("bad CSW after write: %r" % csw)
            self.last_csw = csw[-1]  # bCSWStatus: 0 = ok, 1 = command failed, 2 = phase error

    # ---- session -------------------------------------------------------
    def open(self, tries=200, reset=False):
        """Open communication. reset=True first does a full software replug (hard_reset), and afterwards stops any
        live view / clears the picture buffer left over from an earlier session."""
        try:
            if reset:
                self.t.hard_reset()
            else:
                self.t.reset()
        except Exception as e:
            print("reset failed (continuing): %s" % e, file=sys.stderr)
        for _ in range(tries):
            r = self.command([0x7A, 0x00, 0x01])
            if r and r[0] == 0:
                self._start_keepalive()
                if reset:
                    for cdb in ([0x7A, 1, 2], [0x7A, 1, 0x85]):  # stop live view, clear picture buffer
                        try:
                            self.command(cdb)
                        except Exception as e:
                            print("post-reset cleanup %s: %s" % (bytes(cdb).hex(" "), e), file=sys.stderr)
                return
            if r and r[0] == 1:
                time.sleep(0.1)  # busy
                continue
            raise CamError("open communication failed, status %s" % (r[:1].hex() or "none"))
        raise CamError("open communication timed out")

    def close(self):
        self._stop_keepalive()
        try:
            r = self.command([0x7A, 0, 2, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0x2C, 1, 0, 0])
            return r[:1] == b"\0"
        except Exception:
            return False

    def _start_keepalive(self):
        self._ka_stop.clear()
        self.ka_failures = 0

        def run():
            while not self._ka_stop.wait(0.5):
                try:
                    self.command([0x7A, 0x03, 0xFF], short=True)
                    self.ka_failures = 0
                except Exception:
                    self.ka_failures += 1
        self._ka_thread = threading.Thread(target=run, daemon=True)
        self._ka_thread.start()

    def _stop_keepalive(self):
        self._ka_stop.set()
        if self._ka_thread:
            self._ka_thread.join(2)
            self._ka_thread = None

    # ---- features ------------------------------------------------------
    def thermal_raw(self):
        """Raw first 16 bytes of the two thermal/status replies the app polls (7A 03 30 / 7A 03 34), hex strings."""
        out = {}
        for name, op in (("7A0330", 0x30), ("7A0334", 0x34)):
            try:
                r = self.command([0x7A, 0x03, op])
                out[name] = bytes(r[:16]).hex(" ")
            except Exception as e:
                out[name] = "error: %s" % e
        return out

    def thumbnail(self):
        """Fetch the small JPEG preview of the last picture taken (7A 05 03). Returns bytes or None if not ready."""
        r = self.command([0x7A, 5, 3])
        if r[:1] == b"\x01":
            return None
        if r[:1] != b"\0":
            raise CamError("get thumbnail status %s" % r[:1].hex())
        n = struct.unpack("<I", r[16:20])[0]
        if n > len(r) - 20:
            raise CamError("bad thumbnail length %d (reply %d)" % (n, len(r)))
        return r[20:20 + n]

    def firmware(self):
        r = self.command([0x7A, 0x03, 0x01])
        raw = r[97:129]
        return raw.split(b"\0")[0].decode("ascii", "replace"), r

    # ---- settings (opcode 0x7A reads, 0x7B writes) ---------------------
    def get_settings(self):
        """Full 48-byte settings block (7A 04 60). Offsets: [2] photo res, [3] video res, [6] evb,
        [7] white balance, [10..19] date/time, [32] filter, [35] bitrate, [39] logo. Byte 0 = status."""
        r = self.command([0x7A, 0x04, 0x60])
        if r[:1] != b"\0":
            raise CamError("get settings failed, status %s%s" % (r[:1].hex(), " (power-save, reopen)" if r[:1] == b"\xff" else ""))
        return r

    def get_bitrate(self):
        v = self.command([0x7A, 0x04, 0x0C])[0]
        if v == 0xFF:  # 0xFF is the "camera in power-save" status, not a value (valid settings are 1..254)
            raise CamError("camera is in power-save (0xFF): reopen communication")
        return v

    def set_bitrate(self, value):
        """Set the H.264 bitrate (the original app writes 8 for 1280x640 and 16 for 1920x960; unit unknown).
        Tries the single-value command, then read-modify-write of the full settings block.
        Returns (old, new). self.setting_log records each step (CSW status + read-back). Call before start_live()."""
        if not 1 <= value <= 254:
            raise CamError("bitrate must be 1..254")
        self.setting_log = []
        old = self.get_bitrate()
        self.setting_log.append("before: 7A 04 0C -> %d, block[35] -> %d" % (old, self.get_settings()[35]))
        try:
            self.write_command([0x7B, 0x04, 0x0C, 0x00, 0x01], bytes([value]))
            self.setting_log.append("write 7B 04 0C (single): CSW status %s" % self.last_csw)
        except Exception as e:
            self.setting_log.append("write 7B 04 0C failed: %s" % e)
        new = self.get_bitrate()
        self.setting_log.append("after single: 7A 04 0C -> %d, block[35] -> %d" % (new, self.get_settings()[35]))
        if new != value:
            blk = bytearray(self.get_settings()[:48].ljust(48, b"\0"))
            self.setting_log.append("block read: " + blk.hex(" "))
            # The original app always sends the current date/time inside this block; a camera whose clock was never
            # set may refuse a block with an empty date, so fill it in like the app does.
            import datetime
            now = datetime.datetime.now()
            blk[10:12] = struct.pack("<H", now.year)
            blk[12], blk[13], blk[14], blk[15], blk[16] = now.month, now.day, now.hour, now.minute, now.second
            blk[35] = value
            self.setting_log.append("block write: " + bytes(blk).hex(" "))
            try:
                self.write_command([0x7B, 0x04, 0x60, 0x00, 0x30], bytes(blk))
                self.setting_log.append("write 7B 04 60 (block): CSW status %s" % self.last_csw)
            except Exception as e:
                self.setting_log.append("write 7B 04 60 failed: %s" % e)
            new = self.get_bitrate()
            after = self.get_settings()
            self.setting_log.append("after block: 7A 04 0C -> %d, block[35] -> %d" % (new, after[35]))
            self.setting_log.append("block after: " + after[:48].hex(" "))
            self.setting_log.append("camera date now: %d-%02d-%02d %02d:%02d:%02d (a block write that changed this was accepted)" % (
                struct.unpack("<H", after[10:12])[0], after[12], after[13], after[14], after[15], after[16]))
        return old, new

    # ---- settings block read-modify-write ------------------------------
    SETTING_OFFSETS = {"photo_res": 2, "video_res": 3, "evb": 6, "white_balance": 7, "filter": 32,
                       "bitrate": 35, "logo": 39}
    PHOTO_RES = {3: "5376x2688", 4: "3840x1920"}  # tested on hardware; settings menu lists 5376x2688 first

    def update_settings(self, **edits):
        """Read the 48-byte settings block, apply edits (keys of SETTING_OFFSETS) and write it back with the PC's
        local date/time, exactly as the phone app does each session. The camera has no battery-backed clock.
        Returns the block read back afterwards."""
        import datetime
        blk = bytearray(self.get_settings()[:48].ljust(48, b"\0"))
        now = datetime.datetime.now()
        blk[10:12] = struct.pack("<H", now.year)
        blk[12], blk[13], blk[14], blk[15], blk[16] = now.month, now.day, now.hour, now.minute, now.second
        blk[18:20] = struct.pack("<H", now.microsecond // 1000)
        for k, v in edits.items():
            blk[self.SETTING_OFFSETS[k]] = v
        self.write_command([0x7B, 0x04, 0x60, 0x00, 0x30], bytes(blk))
        return self.get_settings()

    def sync_clock(self):
        return self.update_settings()

    # ---- still pictures ------------------------------------------------
    def snapshot(self, orientation=2, timeout=40.0):
        """Take a still picture and download it. Live view should be running (the phone app always shoots from live
        view). Returns the picture bytes (JPEG). orientation = 0..3 (the app derives it from the phone's rotation)."""
        r = self.command([0x7A, 0x01, 0x05, 0, 0, 0, 0, 0, orientation & 0xFF])
        if r[:1] == b"\xff":
            raise CamError("camera in power-save, reopen needed")
        if r[:1] not in (b"\x00", b"\x01"):
            raise CamError("take picture failed, status %s" % r[:1].hex())
        deadline = time.time() + timeout
        data = bytearray()
        offset = last_offset = 0
        while True:
            if time.time() > deadline:
                raise CamError("timed out waiting for the picture (got %d bytes)" % len(data))
            r = self.command([0x7A, 0x05, 0x02, 0, 0, 0, 0, 0] + list(struct.pack("<I", offset)))
            status = r[0]
            if status == 1:  # busy: picture not ready yet
                time.sleep(0.1)
                continue
            if status == 0xFF:
                raise CamError("camera in power-save during download")
            if status != 0:
                raise CamError("get picture failed, status %02x" % status)
            n = struct.unpack("<I", r[16:20])[0]
            if n > len(r) - 20:
                raise CamError("bad picture chunk length %d (reply %d)" % (n, len(r)))
            data += r[20:20 + n]
            last_offset, offset = offset, offset + n
            if r[1] == 1:  # last chunk
                break
            if n == 0:
                time.sleep(0.05)
        # tell the camera the download is finished (the app repeats the last request offset), then free its buffer
        try:
            self.command([0x7A, 0x05, 0x82, 0, 0, 0, 0, 0] + list(struct.pack("<I", last_offset)))
            self.command([0x7A, 0x01, 0x85])
        except Exception as e:
            print("warning: finishing commands failed: %s" % e, file=sys.stderr)
        return bytes(data)

    def start_live(self, res):
        r = self.command([0x7A, 1, 1, 0, 0, 0, 0, 0, 0, res, 0])
        if r[:1] != b"\0":
            raise CamError("start live view failed, status %s" % r[:1].hex())

    def stop_live(self):
        self.command([0x7A, 1, 2])

    def get_frame(self):
        """Returns (h264_bytes, res_code, thermal) for one complete frame, or None if camera busy."""
        out = bytearray()
        while True:
            r = self.command([0x7A, 5, 1, 0, 0, 0, 0, 0, 0])
            if len(r) < 32:
                raise CamError("short frame reply (%d bytes)" % len(r))
            status = r[0]
            if status == 1:
                return None
            if status == 0xFF:
                raise CamError("camera in power-save, reopen needed")
            if status != 0:
                raise CamError("get frame status %02x" % status)
            n = struct.unpack("<I", r[28:32])[0]
            if n > len(r) - 32:
                raise CamError("bad payload length %d (reply %d)" % (n, len(r)))
            if len(out) + n > 1 << 20:
                raise CamError("frame exceeds 1 MiB")
            out += r[32:32 + n]
            if r[1] == 1:  # last chunk
                self.last_header = bytes(r[:32])
                return bytes(out), r[4], r[20]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="open, print firmware, close")
    ap.add_argument("--res", type=int, default=10, choices=[9, 10], help="live view: 9=1920x960 10=1280x640 (the only two sizes the camera has)")
    ap.add_argument("--reset", action="store_true", help="force a software replug (USB reset) before opening the camera")
    ap.add_argument("--out", default="live.h264", help="output file, or - for stdout")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    ap.add_argument("--bitrate", type=int, default=0, help="set camera H.264 bitrate before streaming (app uses 8 or 16)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    cam = Camera(UsbTransport(), a.verbose)
    try:
        cam.open(reset=a.reset)
        print("communication open", file=sys.stderr)
        fw, _ = cam.firmware()
        print("firmware: %r" % fw, file=sys.stderr)
        try:
            print("camera bitrate setting: %d" % cam.get_bitrate(), file=sys.stderr)
        except Exception as e:
            print("could not read bitrate: %s" % e, file=sys.stderr)
        if a.bitrate:
            old, new = cam.set_bitrate(a.bitrate)
            for line in cam.setting_log:
                print("  " + line, file=sys.stderr)
            print("bitrate %d -> %d (requested %d)" % (old, new, a.bitrate), file=sys.stderr)
        if a.probe:
            return 0
        sink = sys.stdout.buffer if a.out == "-" else open(a.out, "wb")
        cam.start_live(a.res)
        print("live view started code %d (%s) -> %s" % (a.res, "%dx%d" % RES[a.res] if a.res in RES else "size unknown", a.out), file=sys.stderr)
        t0 = last = time.time()
        frames = nbytes = 0
        try:
            while not a.seconds or time.time() - t0 < a.seconds:
                if cam.ka_failures >= 10:
                    raise CamError("keep-alive lost")
                f = cam.get_frame()
                if f is None:
                    time.sleep(0.005)
                    continue
                data, rc, thermal = f
                sink.write(data)
                frames += 1
                nbytes += len(data)
                if thermal not in (0, 1) and frames % 30 == 1:
                    print("thermal status %d (app powers camera off when hot)" % thermal, file=sys.stderr)
                now = time.time()
                if now - last >= 2:
                    print("%.1f fps, %.2f Mbit/s, res code %d" % (frames / (now - t0), nbytes * 8 / (now - t0) / 1e6, rc),
                          file=sys.stderr)
                    last = now
        except KeyboardInterrupt:
            pass
        finally:
            try:
                cam.stop_live()
            except Exception as e:
                print("stop live failed: %s" % e, file=sys.stderr)
            if sink is not sys.stdout.buffer:
                sink.close()
    finally:
        cam.close()
        cam.t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
