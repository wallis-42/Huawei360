#!/usr/bin/env python3
"""Image helpers for the Huawei 360 tools: rotation maths and the "blur the camera body" effect.

The camera's own body / tripod sits at the bottom of the panorama (the nadir). The viewer can hide it three ways:
the camera's watermark (a logo drawn over it), nothing (raw), or a feathered blur of a cap around the nadir.
"""
import math

import cv2
import numpy as np


def rot(yaw=0.0, pitch=0.0, roll=0.0):
    """Rotation matrix. Axes: x right, y up, z forward. Positive yaw = right, pitch = up, roll = clockwise."""
    y, p, r = map(math.radians, (yaw, pitch, roll))
    cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), math.sin(r)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rx = np.array([[1, 0, 0], [0, cp, sp], [0, -sp, cp]])
    Rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return Ry @ Rx @ Rz


def nadir_weight(w, h, mount, deg):
    """float32 (h, w) weights for an equirect frame as the camera delivers it (before the mount rotation is applied):
    1 within `deg` degrees of the level-corrected straight-down direction, fading to 0 over the outer 30 percent.
    Computed at a quarter of the size and scaled up, which is plenty for a soft edge."""
    sw, sh = max(8, w // 4), max(8, h // 4)
    lon = (np.arange(sw) + 0.5) / sw * 2 * math.pi - math.pi
    lat = math.pi / 2 - (np.arange(sh) + 0.5) / sh * math.pi
    glon, glat = np.meshgrid(lon, lat)
    R = rot(*mount)
    # direction d = R^T s in the level-corrected frame; only its vertical component (y) is needed
    dy = (np.cos(glat) * np.sin(glon)) * R[0, 1] + np.sin(glat) * R[1, 1] + (np.cos(glat) * np.cos(glon)) * R[2, 1]
    ang = np.degrees(np.arccos(np.clip(-dy, -1.0, 1.0)))  # angle from straight down
    wt = np.clip((deg - ang) / (0.3 * max(deg, 1e-3)), 0.0, 1.0).astype(np.float32)
    return cv2.resize(wt, (w, h), interpolation=cv2.INTER_LINEAR)


def blur_with_weight(img, weight, strength):
    """Blend a blurred copy of img in where weight is 1. strength 1..10 (blur radius grows with the picture width)."""
    h, w = img.shape[:2]
    sigma = max(1.0, strength * w / 400.0)
    f = max(1, int(sigma / 3))  # blur a smaller copy, then scale back up: much faster for big sigma
    small = cv2.resize(img, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA) if f > 1 else img
    small = cv2.GaussianBlur(small, (0, 0), sigma / f)
    blurred = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR) if f > 1 else small
    return cv2.blendLinear(img, blurred, 1.0 - weight, weight)  # result = (img*w1 + blurred*w2) / (w1 + w2)


def blur_still(jpeg, deg, strength, quality=95, initial=None):
    """Blur the bottom cap of a (camera-upright) panorama JPEG and return new JPEG bytes with the panorama tag and EXIF kept."""
    import hua360_meta
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode the picture")
    h, w = img.shape[:2]
    ang = 180.0 * (h - (np.arange(h) + 0.5)) / h  # angle from the bottom row, degrees
    col = np.clip((deg - ang) / (0.3 * max(deg, 1e-3)), 0.0, 1.0).astype(np.float32).reshape(h, 1)
    weight = np.repeat(col, w, axis=1)
    out = blur_with_weight(img, weight, strength)
    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("could not encode the picture")
    return hua360_meta.set_pano_pose(enc.tobytes(), keep_from=jpeg, initial=initial)
