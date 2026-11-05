#!/usr/bin/env python3
"""360 metadata helpers for the Huawei 360 camera tools (no third-party packages).

* JPEG stills: the camera writes a Google Photos "GPano" XMP block that says the camera was rolled 180 degrees, so viewers such as
  VLC start the panorama upside down although the pixels are upright. set_pano_pose() rewrites that block (default: no rotation).
* MP4 video: add_spherical() marks a video as equirectangular 360 (Spherical Video V2: st3d + sv3d boxes) so VLC and other players
  render it on a globe, with an optional initial pose.
"""
import re
import struct

XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


# ------------------------------------------------------------------ JPEG
def _segments(jpeg):
    """Split the JPEG header into marker segments. Returns (list of (marker, payload), offset where the rest starts)."""
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    segs, i = [], 2
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF and 0xE0 <= jpeg[i + 1] <= 0xEF:
        n = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        segs.append((jpeg[i + 1], jpeg[i + 4:i + 2 + n]))
        i += 2 + n
    return segs, i


def jpeg_size(jpeg):
    """(width, height) from the first SOF marker."""
    i = 2
    while i + 9 < len(jpeg):
        if jpeg[i] != 0xFF:
            i += 1
            continue
        m = jpeg[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", jpeg[i + 5:i + 9])
            return w, h
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", jpeg[i + 2:i + 4])[0]
    raise ValueError("no SOF marker found")


def pano_xmp(width, height, heading=None, pitch=0.0, roll=0.0, initial=None):
    """XMP packet (bytes) with the GPano fields for a full equirectangular panorama.

    initial: optional (heading, pitch, horizontal_fov) in degrees = where the viewer should look when it opens (fov may be None)."""
    pose = ""
    if initial is not None:
        pose += "<GPano:InitialViewHeadingDegrees>%.1f</GPano:InitialViewHeadingDegrees><GPano:InitialViewPitchDegrees>%.1f</GPano:InitialViewPitchDegrees>" % (initial[0], initial[1])
        if len(initial) > 2 and initial[2]:
            pose += "<GPano:InitialHorizontalFOVDegrees>%.1f</GPano:InitialHorizontalFOVDegrees>" % initial[2]
    if heading is not None:
        pose += "<GPano:PoseHeadingDegrees>%.1f</GPano:PoseHeadingDegrees>" % heading
    pose += "<GPano:PosePitchDegrees>%.1f</GPano:PosePitchDegrees><GPano:PoseRollDegrees>%.1f</GPano:PoseRollDegrees>" % (pitch, roll)
    return (
        "<x:xmpmeta xmlns:x='adobe:ns:meta/' x:xmptk='CV60'>\n"
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n\n"
        " <rdf:Description rdf:about=''\n  xmlns:GPano='http://ns.google.com/photos/1.0/panorama/'>\n"
        "  <GPano:UsePanoramaViewer>True</GPano:UsePanoramaViewer>\n"
        " <GPano:ProjectionType>equirectangular</GPano:ProjectionType>" + pose +
        "<GPano:CroppedAreaImageWidthPixels>%d</GPano:CroppedAreaImageWidthPixels>"
        "<GPano:CroppedAreaImageHeightPixels>%d</GPano:CroppedAreaImageHeightPixels>"
        "<GPano:FullPanoWidthPixels>%d</GPano:FullPanoWidthPixels>"
        "<GPano:FullPanoHeightPixels>%d</GPano:FullPanoHeightPixels>"
        "<GPano:CroppedAreaLeftPixels>0</GPano:CroppedAreaLeftPixels><GPano:CroppedAreaTopPixels>0</GPano:CroppedAreaTopPixels>"
        "</rdf:Description>\n</rdf:RDF>\n</x:xmpmeta>" % (width, height, width, height)
    ).encode("ascii")


def set_pano_pose(jpeg, heading=None, pitch=0.0, roll=0.0, keep_from=None, initial=None):
    """Return the JPEG with its panorama XMP replaced by one with the given pose (default: upright, no rotation).

    keep_from: another JPEG (e.g. the camera's original) whose EXIF segment should be carried over, for pictures
    that were re-encoded and lost it."""
    segs, rest = _segments(jpeg)
    w, h = jpeg_size(jpeg)
    kept = [(m, p) for m, p in segs if not (m == 0xE1 and p.startswith(XMP_HEADER))]
    if keep_from is not None and not any(m == 0xE1 and p.startswith(b"Exif\x00\x00") for m, p in kept):
        try:
            osegs, _ = _segments(keep_from)
            kept = [(m, p) for m, p in osegs if m == 0xE1 and p.startswith(b"Exif\x00\x00")] + kept
        except ValueError:
            pass
    # JFIF (APP0) must come first, then EXIF, then XMP
    kept.sort(key=lambda mp: 0 if mp[0] == 0xE0 else 1)
    kept.append((0xE1, XMP_HEADER + pano_xmp(w, h, heading, pitch, roll, initial)))
    out = bytearray(b"\xff\xd8")
    for m, p in kept:
        if len(p) + 2 > 0xFFFF:
            continue  # cannot fit in one segment; drop it
        out += bytes([0xFF, m]) + struct.pack(">H", len(p) + 2) + p
    out += jpeg[rest:]
    return bytes(out)


def pano_pose(jpeg):
    """Read the (heading, pitch, roll) currently stored in the picture's GPano block, None for missing fields."""
    segs, _ = _segments(jpeg)
    for m, p in segs:
        if m == 0xE1 and p.startswith(XMP_HEADER):
            txt = p.decode("latin1")
            res = []
            for tag in ("PoseHeadingDegrees", "PosePitchDegrees", "PoseRollDegrees"):
                r = re.search(r"<GPano:%s>([-0-9.]+)<" % tag, txt)
                res.append(float(r.group(1)) if r else None)
            return tuple(res)
    return None


# ------------------------------------------------------------------ MP4
def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _fixed(deg):
    return struct.pack(">i", int(round(deg * 65536)))


def spherical_v2_boxes(yaw=0.0, pitch=0.0, roll=0.0):
    """(st3d, sv3d) boxes for an equirectangular, mono 360 video with the given pose in degrees."""
    st3d = _box(b"st3d", b"\x00\x00\x00\x00" + b"\x00")
    svhd = _box(b"svhd", b"\x00\x00\x00\x00" + b"hua360\x00")
    prhd = _box(b"prhd", b"\x00\x00\x00\x00" + _fixed(yaw) + _fixed(pitch) + _fixed(roll))
    equi = _box(b"equi", b"\x00\x00\x00\x00" + struct.pack(">4I", 0, 0, 0, 0))
    proj = _box(b"proj", prhd + equi)
    sv3d = _box(b"sv3d", svhd + proj)
    return st3d, sv3d


def _children(data, start, end):
    """Child boxes in data[start:end] as (kind, box_start, box_end, header_len)."""
    out, i = [], start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        out.append((kind, i, i + size, hdr))
        i += size
    return out


def _find(data, start, end, kind):
    for k, s, e, h in _children(data, start, end):
        if k == kind:
            return s, e, h
    return None


def add_spherical(path, yaw=0.0, pitch=0.0, roll=0.0):
    """Mark the video track of an .mp4 as 360 equirectangular (in place). Returns True when the tags were written.

    Works on files whose boxes use 32-bit sizes (ffmpeg's normal output). Existing st3d / sv3d boxes are replaced."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    top = _children(data, 0, len(data))
    moov = next(((s, e, h) for k, s, e, h in top if k == b"moov"), None)
    mdat = next(((s, e, h) for k, s, e, h in top if k == b"mdat"), None)
    if not moov:
        return False
    chain = [moov]  # boxes whose size must grow: moov > trak > mdia > minf > stbl > stsd > sample entry
    video_trak = None
    for k, s, e, h in _children(data, moov[0] + moov[2], moov[1]):
        if k != b"trak":
            continue
        mdia = _find(data, s + h, e, b"mdia")
        hdlr = _find(data, mdia[0] + mdia[2], mdia[1], b"hdlr") if mdia else None
        if hdlr and data[hdlr[0] + hdlr[2] + 8:hdlr[0] + hdlr[2] + 12] == b"vide":
            video_trak = (s, e, h)
            break
    if not video_trak:
        return False
    chain.append(video_trak)
    cur = video_trak
    for kind in (b"mdia", b"minf", b"stbl", b"stsd"):
        nxt = _find(data, cur[0] + cur[2], cur[1], kind)
        if not nxt:
            return False
        chain.append(nxt)
        cur = nxt
    stsd = cur
    # stsd is a full box with a 4 byte entry count after the header; the first sample entry follows
    entry = _children(data, stsd[0] + stsd[2] + 8, stsd[1])
    if not entry:
        return False
    k, es, ee, eh = entry[0]
    chain.append((es, ee, eh))

    # drop any existing st3d / sv3d inside the sample entry (children start after the 78 byte VisualSampleEntry fields)
    removed = 0
    for kk, ks, ke, kh in reversed(_children(data, es + eh + 78, ee)):
        if kk in (b"st3d", b"sv3d"):
            del data[ks:ke]
            removed += ke - ks
    end_of_entry = ee - removed

    st3d, sv3d = spherical_v2_boxes(yaw, pitch, roll)
    add = st3d + sv3d
    data[end_of_entry:end_of_entry] = add
    delta = len(add) - removed
    for s_, e_, h_ in chain:  # grow every ancestor and the sample entry itself (box starts do not move)
        size = struct.unpack(">I", data[s_:s_ + 4])[0]
        data[s_:s_ + 4] = struct.pack(">I", size + delta)
    # if media data sits after moov, its chunk offsets move by delta
    if mdat and mdat[0] > moov[0]:
        ms = chain[0][0]  # moov start; its end moved because it grew
        _shift_chunk_offsets(data, (ms, ms + struct.unpack(">I", data[ms:ms + 4])[0], chain[0][2]), delta)
    with open(path, "wb") as f:
        f.write(data)
    return True


def _shift_chunk_offsets(data, moov, delta):
    s, e, h = moov
    for k, ts, te, th in _children(data, s + h, e):
        if k != b"trak":
            continue
        mdia = _find(data, ts + th, te, b"mdia")
        minf = _find(data, mdia[0] + mdia[2], mdia[1], b"minf") if mdia else None
        stbl = _find(data, minf[0] + minf[2], minf[1], b"stbl") if minf else None
        if not stbl:
            continue
        for kk, ss, se, sh in _children(data, stbl[0] + stbl[2], stbl[1]):
            if kk == b"stco":
                n = struct.unpack(">I", data[ss + sh + 4:ss + sh + 8])[0]
                for j in range(n):
                    p = ss + sh + 8 + 4 * j
                    data[p:p + 4] = struct.pack(">I", struct.unpack(">I", data[p:p + 4])[0] + delta)
            elif kk == b"co64":
                n = struct.unpack(">I", data[ss + sh + 4:ss + sh + 8])[0]
                for j in range(n):
                    p = ss + sh + 8 + 8 * j
                    data[p:p + 8] = struct.pack(">Q", struct.unpack(">Q", data[p:p + 8])[0] + delta)


def main(argv=None):
    import argparse
    import glob
    ap = argparse.ArgumentParser(description="Fix the 360 tags of pictures and videos made earlier.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("photo", help="make 360 JPEGs start the right way up (rewrites only the panorama tag, pixels are untouched)")
    p1.add_argument("files", nargs="+", help="JPEG files; wildcards allowed, e.g. snapshots\\*.jpg")
    p1.add_argument("--roll", type=float, default=0.0, help="roll in degrees to store (default 0; the camera itself says 180)")
    p1.add_argument("--pitch", type=float, default=0.0)
    p1.add_argument("--start-yaw", type=float, default=None, help="where the viewer should look when it opens: heading in degrees")
    p1.add_argument("--start-pitch", type=float, default=0.0, help="start view: up/down in degrees")
    p1.add_argument("--start-fov", type=float, default=None, help="start view: horizontal field of view in degrees (zoom)")
    p2 = sub.add_parser("video", help="mark .mp4 files as 360 equirectangular video so players render them on a globe")
    p2.add_argument("files", nargs="+", help="MP4 files; wildcards allowed")
    p2.add_argument("--yaw", type=float, default=0.0)
    p2.add_argument("--pitch", type=float, default=0.0)
    p2.add_argument("--roll", type=float, default=0.0)
    a = ap.parse_args(argv)
    names = [n for pat in a.files for n in (glob.glob(pat) or [pat])]
    for n in names:
        try:
            if a.cmd == "photo":
                with open(n, "rb") as f:
                    data = f.read()
                initial = None if a.start_yaw is None and a.start_fov is None and not a.start_pitch else (a.start_yaw or 0.0, a.start_pitch, a.start_fov)
                new = set_pano_pose(data, pitch=a.pitch, roll=a.roll, initial=initial)
                with open(n, "wb") as f:
                    f.write(new)
                print("%s: pose %s -> %s" % (n, pano_pose(data), pano_pose(new)))
            else:
                print("%s: %s" % (n, "360 tagged" if add_spherical(n, a.yaw, a.pitch, a.roll) else "not changed (no video track found)"))
        except Exception as e:
            print("%s: failed (%s: %s)" % (n, type(e).__name__, e))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
