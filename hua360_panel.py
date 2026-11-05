#!/usr/bin/env python3
"""Control panel (Tk window) for hua360_view.py - the same controls as the keyboard, as sliders and buttons.

Started by the viewer (always) on the main thread; the render loop runs on another thread and shares state through a dict.
It is the only way to control the viewer, and closing it quits the viewer.
Not meant to be run on its own.
"""
import base64
import sys
import threading
import time

import cv2

import hua360

THERMAL = {0: "temp OK", 1: "TEMP HOT", 2: "OVERHEATING", 3: "TEMP COLD"}
PREVIEW_W = 360


def run_panel(ui, view, mount, src, presets, ow, oh):
    """Run the panel on the calling thread (must be the main thread); returns when the panel is closed."""
    _run(ui, view, mount, src, presets, ow, oh)


def _run(ui, view, mount, src, presets, ow, oh):
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as e:
        print("control panel unavailable (tkinter missing: %s)" % e, file=sys.stderr)
        return
    try:
        _build(tk, ttk, ui, view, mount, src, presets, ow, oh)
    except Exception as e:
        print("control panel stopped: %s: %s" % (type(e).__name__, e), file=sys.stderr)
    ui["quit"] = True  # the panel is the only control: when it ends, so does the viewer


def _build(tk, ttk, ui, view, mount, src, presets, ow, oh):
    root = tk.Tk()
    root.title("Huawei 360 controls")
    root.resizable(False, False)
    updating = [False]  # True while the panel itself moves a widget, so callbacks do not write back

    def wrap180(x):
        return (x + 180.0) % 360.0 - 180.0

    def push(action):
        ui["actions"].append(action)

    def scale(parent, row, text, lo, hi, getter, setter, length=230, res=1):
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=4)

        def cb(v):
            if not updating[0]:
                setter(float(v))
        s = tk.Scale(parent, from_=lo, to=hi, orient="horizontal", resolution=res, length=length, command=cb)
        s.grid(row=row, column=1, sticky="we", padx=4)
        updating[0] = True  # setting the start value must not write back to the camera/viewer
        try:
            s.set(getter())
        finally:
            updating[0] = False
        return s, getter

    def sync_scale(s, value):
        if abs(float(s.get()) - value) >= 0.5:
            updating[0] = True
            try:
                s.set(value)
            finally:
                updating[0] = False

    pad = {"padx": 6, "pady": 3}

    # ---------------- preview
    top = ttk.Frame(root)
    top.grid(row=0, column=0, sticky="we", **pad)
    prev_var = tk.BooleanVar(value=True)
    prev_lbl = tk.Label(top, text="(preview off)", width=PREVIEW_W // 8, height=2, bg="black", fg="gray")
    ttk.Checkbutton(top, text="Preview (drag = look, wheel = zoom)", variable=prev_var).grid(row=0, column=0, sticky="w")
    prev_lbl.grid(row=1, column=0)
    drag = {"x": 0, "y": 0}

    def on_press(ev):
        drag.update(x=ev.x, y=ev.y)

    def on_drag(ev):
        k = view["fov"] / PREVIEW_W
        view["yaw"] -= (ev.x - drag["x"]) * k
        view["pitch"] = max(-89, min(89, view["pitch"] + (ev.y - drag["y"]) * k))
        drag.update(x=ev.x, y=ev.y)

    def on_wheel(ev):
        view["fov"] = max(20, min(140, view["fov"] - (10 if ev.delta > 0 else -10)))

    prev_lbl.bind("<ButtonPress-1>", on_press)
    prev_lbl.bind("<B1-Motion>", on_drag)
    prev_lbl.bind("<MouseWheel>", on_wheel)

    # ---------------- camera settings
    cam = ttk.LabelFrame(root, text="Camera (applies immediately)")
    cam.grid(row=1, column=0, sticky="we", **pad)
    cam.columnconfigure(1, weight=1)
    ev_scale, _ = scale(cam, 0, "Exposure", 0, len(hua360.EV_ORDER) - 1,
                        lambda: hua360.EV_ORDER.index(src.cam_settings.get("evb", 0)) if src.cam_settings.get("evb", 0) in hua360.EV_ORDER else 6,
                        lambda v: src.queue(evb=hua360.EV_ORDER[int(v)]), res=1)
    ev_lbl = ttk.Label(cam, text="0 EV", width=8)
    ev_lbl.grid(row=0, column=2)

    ttk.Label(cam, text="White balance").grid(row=1, column=0, sticky="w", padx=4)
    wb = ttk.Combobox(cam, values=hua360.WB_NAMES, state="readonly", width=16)
    wb.grid(row=1, column=1, sticky="w", padx=4, pady=2)
    wb.bind("<<ComboboxSelected>>", lambda e: src.queue(white_balance=hua360.WB_NAMES.index(wb.get())))

    ttk.Label(cam, text="Filter").grid(row=2, column=0, sticky="w", padx=4)
    flt = ttk.Combobox(cam, values=hua360.FILTER_NAMES, state="readonly", width=16)
    flt.grid(row=2, column=1, sticky="w", padx=4, pady=2)
    flt.bind("<<ComboboxSelected>>", lambda e: src.queue(filter=hua360.FILTER_NAMES.index(flt.get())))

    body_var = tk.StringVar(value=ui["body"] or "raw")

    def on_body():
        ui["body"] = body_var.get()
        want = 1 if ui["body"] == "logo" else 0  # logo = camera watermark; raw and blur both need it off
        if src.cam_settings.get("logo") != want:
            src.queue(logo=want)

    bf = ttk.Frame(cam)
    bf.grid(row=3, column=0, columnspan=3, sticky="w", padx=4)
    ttk.Label(bf, text="Camera body (bottom):").pack(side="left")
    for val, text in (("logo", "Logo"), ("raw", "Raw"), ("blur", "Blur")):
        ttk.Radiobutton(bf, text=text, value=val, variable=body_var, command=on_body).pack(side="left", padx=6)
    bs_size, _ = scale(cam, 5, "Blur size (deg)", 5, 45, lambda: ui["blur_deg"], lambda v: ui.__setitem__("blur_deg", v))
    bs_str, _ = scale(cam, 6, "Blur strength", 1, 10, lambda: ui["blur_str"], lambda v: ui.__setitem__("blur_str", v))

    res_var = tk.IntVar(value=src.res)
    rf = ttk.Frame(cam)
    rf.grid(row=4, column=0, columnspan=3, sticky="w", padx=4)
    ttk.Label(rf, text="Live view:").pack(side="left")
    for code, text in ((9, "1920x960"), (10, "1280x640")):
        ttk.Radiobutton(rf, text=text, value=code, variable=res_var,
                        command=lambda: src.queue(res=res_var.get())).pack(side="left", padx=6)

    # ---------------- view
    vw = ttk.LabelFrame(root, text="View")
    vw.grid(row=2, column=0, sticky="we", **pad)
    vw.columnconfigure(1, weight=1)
    fov_s, _ = scale(vw, 0, "Zoom (FOV)", 20, 140, lambda: view["fov"], lambda v: view.__setitem__("fov", v))
    yaw_s, _ = scale(vw, 1, "Look left/right", -180, 180, lambda: wrap180(view["yaw"]), lambda v: view.__setitem__("yaw", v))
    pit_s, _ = scale(vw, 2, "Look down/up", -89, 89, lambda: view["pitch"], lambda v: view.__setitem__("pitch", v))
    flat_var = tk.BooleanVar(value=ui["flat"])
    ttk.Checkbutton(vw, text="Flat equirectangular view", variable=flat_var,
                    command=lambda: ui.__setitem__("flat", flat_var.get())).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

    # ---------------- mount
    mt = ttk.LabelFrame(root, text="Camera mount (level the horizon, then Save)")
    mt.grid(row=3, column=0, sticky="we", **pad)
    mt.columnconfigure(1, weight=1)
    mscales = []
    for i, name in enumerate(("Yaw", "Pitch", "Roll")):
        s, _ = scale(mt, i, name, -180, 180, (lambda j: lambda: mount[j])(i), (lambda j: lambda v: mount.__setitem__(j, v))(i))
        mscales.append(s)
    pf = ttk.Frame(mt)
    pf.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=2)
    ttk.Label(pf, text="Presets:").pack(side="left")
    for k in sorted(presets):
        ttk.Button(pf, text=chr(k), width=2, command=(lambda kk: lambda: push(("preset", kk)))(k)).pack(side="left", padx=1)
    ttk.Button(pf, text="Reset", width=6, command=lambda: push("reset_mount")).pack(side="left", padx=6)

    # ---------------- actions
    ac = ttk.LabelFrame(root, text="Actions")
    ac.grid(row=4, column=0, sticky="we", **pad)
    ttk.Button(ac, text="Snapshot (full res)", command=lambda: push("snapshot")).grid(row=0, column=0, padx=4, pady=3)
    rec_btn = ttk.Button(ac, text="Start recording", command=lambda: push("record"))
    rec_btn.grid(row=0, column=1, padx=4, pady=3)
    ttk.Button(ac, text="Save frame PNG", command=lambda: push("save_frame")).grid(row=0, column=2, padx=4, pady=3)
    ttk.Button(ac, text="Save settings", command=lambda: push("save_settings")).grid(row=0, column=3, padx=4, pady=3)
    vcam_var = tk.BooleanVar(value=ui["vcam"])
    ttk.Checkbutton(ac, text="OBS virtual camera", variable=vcam_var,
                    command=lambda: ui.__setitem__("vcam", vcam_var.get())).grid(row=1, column=0, columnspan=2, sticky="w", padx=4)
    win_var = tk.BooleanVar(value=ui["window"])
    ttk.Checkbutton(ac, text="Large view window", variable=win_var,
                    command=lambda: ui.__setitem__("window", win_var.get())).grid(row=1, column=2, columnspan=2, sticky="w", padx=4)
    top_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(ac, text="Keep panel on top", variable=top_var,
                    command=lambda: root.attributes("-topmost", top_var.get())).grid(row=2, column=0, columnspan=2, sticky="w", padx=4)

    status = tk.StringVar(value="starting ...")
    ttk.Label(root, textvariable=status, relief="sunken", anchor="w").grid(row=5, column=0, sticky="we", padx=6, pady=(3, 6))

    # ---------------- periodic refresh
    def refresh_preview():
        if prev_var.get():
            out = ui.get("out")
            if out is not None:
                h, w = out.shape[:2]
                small = cv2.resize(out, (PREVIEW_W, max(1, int(h * PREVIEW_W / w))), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".png", small)
                if ok:
                    img = tk.PhotoImage(data=base64.b64encode(buf.tobytes()))
                    prev_lbl.configure(image=img, text="", width=small.shape[1], height=small.shape[0])
                    prev_lbl.image = img
        elif prev_lbl.cget("image"):
            prev_lbl.configure(image="", text="(preview off)", width=PREVIEW_W // 8, height=2)
            prev_lbl.image = None
        root.after(100, refresh_preview)

    def refresh_state():
        if not src.pending:  # camera values: follow what the camera really reports (also keyboard changes)
            cs = src.cam_settings
            if "evb" in cs and cs["evb"] in hua360.EV_ORDER:
                sync_scale(ev_scale, hua360.EV_ORDER.index(cs["evb"]))
                ev_lbl.configure(text="%s EV" % hua360.EV_NAMES[cs["evb"]])
            if cs.get("white_balance", 99) < len(hua360.WB_NAMES) and wb.get() != hua360.WB_NAMES[cs["white_balance"]]:
                wb.set(hua360.WB_NAMES[cs["white_balance"]])
            if cs.get("filter", 99) < len(hua360.FILTER_NAMES) and flt.get() != hua360.FILTER_NAMES[cs["filter"]]:
                flt.set(hua360.FILTER_NAMES[cs["filter"]])
            if ui["body"] is None and "logo" in cs:  # nothing chosen yet: follow the camera's own watermark setting
                ui["body"] = "logo" if cs["logo"] else "raw"
            res_var.set(src.res)
        body_var.set(ui["body"] or "raw")
        sync_scale(bs_size, ui["blur_deg"])
        sync_scale(bs_str, ui["blur_str"])
        sync_scale(fov_s, view["fov"])
        sync_scale(yaw_s, wrap180(view["yaw"]))
        sync_scale(pit_s, view["pitch"])
        for s, v in zip(mscales, mount):
            sync_scale(s, v)
        flat_var.set(ui["flat"])
        vcam_var.set(ui["vcam"])
        win_var.set(ui["window"])
        rec_btn.configure(text="Stop recording" if src.rec_file else "Start recording")
        msg = ""
        if src.snap_msg and time.time() - src.snap_msg_t < 8:
            msg = " | " + src.snap_msg
        elif src.rec_file:
            msg = " | REC %d s" % (time.time() - src.rec_t0)
        t = THERMAL.get(src.thermal, "temp ?") if src.thermal is not None else "temp ?"
        status.set("%s | %.0f fps | %.1f Mb/s | %s%s" % (src.status, ui.get("fps", 0), ui.get("mbps", 0), t, msg))
        if ui.get("quit"):
            root.destroy()
            return
        root.after(250, refresh_state)

    def on_close():
        ui["quit"] = True
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_state()
    refresh_preview()
    root.mainloop()
