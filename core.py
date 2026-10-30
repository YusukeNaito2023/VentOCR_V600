"""Dräger V600 VentOCR Engine

- 1 breath = from one update of the VT display until the next update
- OCR: VT (leak-compensated)
- Waveforms: airway pressure (Pressure), flow (Flow), volume (Volume). Each is drawn filled from the baseline to the value

The waveform screen sweeps left to right, then returns to the left edge and overwrites (wrap around). However, the return
position and sweep speed are not constant (it returns early if the next breath starts before the right edge, a mode change
clears everything, and the speed depends on the mode), so no period is assumed.
The pen tip (left edge of the erase band) is detected in each frame (initial_ocr) and converted to a continuous position
with the wraps unrolled (post_ocr).
Each breath's waveform is cut from the image of that breath's last frame, taking the columns from the pen tip at its start
to the pen tip at its end (extract_waves).
"""
import json
import os
import re

import cv2
import numpy as np
import pandas as pd
from tqdm import tqdm

from . import Settings

ENGINE_VERSION = "0.2.1"
VENTOCR_TESTED = "0.5.5"

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_KEYS = list(Settings.ROIs)


# ============================================================
# EasyOCR initialization
# ============================================================

_EASYOCR_LOCKFILE = "easyocr_init.lock"


def _get_easyocr_model_dir():
    model_dir = os.path.join(".", "_runtime_models", "easyocr")
    os.makedirs(model_dir, exist_ok=True)
    return model_dir


def _easyocr_model_ready(model_dir):
    from pathlib import Path
    p = Path(model_dir)
    if not p.exists():
        return False
    patterns = ["craft*.pth", "dbnet*.pth", "latin*.pth", "english_g2*.pth", "standard*.pth"]
    return any(list(p.glob(pat)) for pat in patterns)


def _acquire_easyocr_file_lock(model_dir, timeout=300, poll=0.5):
    import time
    lock_path = os.path.join(model_dir, _EASYOCR_LOCKFILE)
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return fd, lock_path
        except FileExistsError:
            if (time.time() - start) > timeout:
                raise TimeoutError(f"Timeout waiting for EasyOCR lock: {lock_path}")
            time.sleep(poll)


def _release_easyocr_file_lock(fd, lock_path):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


def _ensure_easyocr_reader_ready(gpu_enabled):
    from easyocr import Reader
    model_dir = _get_easyocr_model_dir()
    if _easyocr_model_ready(model_dir):
        return Reader(["en"], gpu=gpu_enabled, model_storage_directory=model_dir, download_enabled=True)

    fd, lock_path = _acquire_easyocr_file_lock(model_dir)
    try:
        if not _easyocr_model_ready(model_dir):
            Reader(["en"], gpu=gpu_enabled, model_storage_directory=model_dir, download_enabled=True)
    finally:
        _release_easyocr_file_lock(fd, lock_path)

    return Reader(["en"], gpu=gpu_enabled, model_storage_directory=model_dir, download_enabled=True)


# ============================================================
# Common
# ============================================================

def _normalize_frame(frame):
    """Resize to the base resolution (1920x1080)."""
    w, h = Settings.BASE_RESOLUTION
    if frame.shape[1] != w or frame.shape[0] != h:
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
    return frame


def _to_gray(frame):
    return np.dot(frame[..., :3], [0.114, 0.587, 0.299]).astype(np.uint8)


def _crop(img, roi):
    x, y, w, h = roi
    return img[y:y + h, x:x + w]


def _runs(mask):
    """List of (start, end) for runs of True (end inclusive)."""
    idx = np.flatnonzero(np.diff(np.r_[0, mask.astype(np.int8), 0]))
    return list(zip(idx[0::2], idx[1::2] - 1))


def _wave_mask(hsv):
    """Pixels of the waveform fill (dark blue / light blue)."""
    d = np.abs(hsv[..., 0].astype(np.int16) - Settings.WAVE_HUE)
    return (d <= Settings.WAVE_HUE_TOL) & (hsv[..., 1] >= Settings.WAVE_SAT_MIN) & (hsv[..., 2] >= Settings.WAVE_VAL_MIN)


def _load_axis_reference():
    path = os.path.join(ENGINE_DIR, Settings.AXIS_REFERENCE_IMAGE)
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE) if os.path.isfile(path) else None
    return None if img is None else img.astype(np.int16)


def screen_diff(gray, axis_ref):
    """Mean absolute difference between the axis label column and the reference image.

    0.2.1: the column's background level is shifted to the reference's before comparing (median of each = the background).
    Recordings differ in brightness (background 232 in V600_Sample.mp4, 255 in the 2026-10-27 1080p recording), which
    alone gave a difference of 24 and marked every frame Abnormal. The tick numbers' pattern still has to match.
    """
    roi = _crop(gray, Settings.AXIS_LABEL_ROI).astype(np.int16)
    roi = np.clip(roi + int(np.median(axis_ref) - np.median(roi)), 0, 255)
    return round(float(np.abs(roi - axis_ref).mean()), 2)


def _format_time(frame, fps):
    """Build an HH:MM:SS:CC timestamp from a frame number"""
    total_seconds = frame / fps
    s = int(total_seconds)
    centiseconds = int((total_seconds - s) * 100)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}:{centiseconds:02d}"


def detect_pen(frame):
    """x of the pen tip (screen coordinates), or NaN if not found.

    The airway pressure band is always filled, so the pen tip is found from runs of unfilled columns
    (pen tip = one column before the left end of the run).
      1. Erase band: a run whose length is within SWEEP_GAP_LEN and that has fill (from the previous sweep) to its right
      2. Otherwise, a run extending to the right edge (first sweep, right after the screen is cleared, or when the pen tip
         is near the right edge and the erase band is cut off at the edge)
    When the sweep returns to the left edge before reaching the right edge, the erased part on the right also becomes a run
    extending to the right edge, but the erase band of case 1 takes priority.
    When the pen tip is near the right edge, the erase band wraps at the right edge and spills over onto the left edge.
    If the combined length of the run starting at the left edge and the run extending to the right edge equals the
    erase band width, the pen tip is on the right-edge side.
    """
    y0, y1 = Settings.SWEEP_GAP_ROWS
    x_lo, x_hi = Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT
    hsv = cv2.cvtColor(frame[y0:y1, x_lo:x_hi], cv2.COLOR_BGR2HSV)
    filled = _wave_mask(hsv).sum(0) >= 2
    last = len(filled) - 1
    lo, hi = Settings.SWEEP_GAP_LEN
    runs = _runs(~filled)
    tail = next((a for a, b in runs if b == last), None)
    head_len = runs[0][1] + 1 if runs and runs[0][0] == 0 else 0
    if tail is not None and head_len and lo <= head_len + (last - tail + 1) <= hi:
        return float(x_lo + tail - 1)
    for a, b in runs:
        if b < last and lo <= b - a + 1 <= hi:
            return float(x_lo + a - 1)
    return float(x_lo + tail - 1) if tail is not None else np.nan


def wave_scale(name, max_value):
    """Vertical-axis max value -> (min value, max value, baseline y, px/unit)."""
    spec = Settings.WAVES[name]
    vmax = float(max_value) if max_value and np.isfinite(max_value) and max_value > 0 else float(spec["default_max"])
    vmin = {"fixed": float(spec.get("min_value", 0)), "neg_max": -vmax, "zero": 0.0}[spec["min_rule"]]
    k = (spec["bottom"] - spec["top"]) / (vmax - vmin)
    return vmin, vmax, spec["top"] + vmax * k, k


def popup_pixels(frame):
    """Number of pixels with the frame color of a popup (settings screen, etc.)."""
    x, y, w, h = Settings.POPUP_AREA
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    (h0, h1), (s0, s1), (v0, v1) = Settings.POPUP_HSV
    m = (hsv[..., 0] >= h0) & (hsv[..., 0] <= h1) & (hsv[..., 1] >= s0) & (hsv[..., 1] <= s1) & (hsv[..., 2] >= v0) & (hsv[..., 2] <= v1)
    return int(m.sum())


# ============================================================
# OCR result parsing
# ============================================================

def _parse_ocr(result):
    """EasyOCR result -> (value string, raw string, confidence string). VT is an integer (mL)."""
    texts = [t for _, t, _ in result]
    confs = [s for _, _, s in result]
    raw = ", ".join(texts)
    conf = ", ".join(f"{s:.2f}" for s in confs)
    digits = re.sub(r"[^0-9]", "", "".join(texts))
    return digits, raw, conf


# ============================================================
# initial_ocr
# ============================================================

def initial_ocr(frames, total_frames, subfolder, settings={},
                progress_callback=None, frames_dir=None):
    """
    Read VT and the vertical-axis max values in each frame, and record the pen tip position and whether a popup is shown.

    VT is OCR'd only in frames where the ROI image has changed since the last OCR; otherwise the previous value is carried over.

    Returns:
        pd.DataFrame (columns: Frame, BreathMark, VT, VT_raw, VT_conf, OCR_Done,
                      Scale_Pressure, Scale_Flow, Scale_Volume, Sweep_Raw, Popup_Px, Screen_Diff)
    """
    opts = settings.get("initial_ocr", {})
    skip_keys = opts.get("skip_keys", [])
    gpu_enabled = opts.get("use_gpu", True)
    diff_thresh = float(opts.get("diff_thresh", 3.0))

    reader = _ensure_easyocr_reader_ready(gpu_enabled)
    print(f"GPU:{gpu_enabled}")

    axis_ref = _load_axis_reference()
    last_roi = {k: None for k in OCR_KEYS}
    last_val = {k: ("", "", "0.00") for k in OCR_KEYS}
    last_scale_roi = {name: None for name in Settings.WAVES}
    last_scale = {name: "" for name in Settings.WAVES}
    data = []

    with tqdm(total=total_frames, desc="OCR Processing") as pbar:
        for frame_count, color_frame in enumerate(frames):
            frame = _normalize_frame(color_frame)
            gray = _to_gray(frame)
            row = {"Frame": frame_count, "BreathMark": 0, "OCR_Done": 0}

            for key in OCR_KEYS:
                if key not in skip_keys:
                    roi = _crop(gray, Settings.ROIs[key]).astype(np.int16)
                    prev = last_roi[key]
                    if prev is None or np.abs(roi - prev).mean() > diff_thresh:
                        try:
                            result = reader.readtext(roi.astype(np.uint8), allowlist="0123456789")
                            last_val[key] = _parse_ocr(result)
                        except Exception:
                            last_val[key] = ("", "", "0.00")
                        last_roi[key] = roi
                        row["OCR_Done"] = 1
                value, raw, conf = last_val[key]
                row[key], row[f"{key}_raw"], row[f"{key}_conf"] = value, raw, conf

            # Vertical-axis max values (OCR only when the digit image changes)
            for name, spec in Settings.WAVES.items():
                roi = _crop(gray, spec["max_roi"]).astype(np.int16)
                prev = last_scale_roi[name]
                if prev is None or np.abs(roi - prev).mean() > diff_thresh:
                    z = Settings.SCALE_OCR_ZOOM
                    big = cv2.resize(roi.astype(np.uint8), None, fx=z, fy=z, interpolation=cv2.INTER_CUBIC)
                    try:
                        last_scale[name] = _parse_ocr(reader.readtext(big, allowlist="0123456789"))[0]
                    except Exception:
                        last_scale[name] = ""
                    last_scale_roi[name] = roi
                row[f"Scale_{name}"] = last_scale[name]

            row["Sweep_Raw"] = detect_pen(frame)
            row["Popup_Px"] = popup_pixels(frame)
            if axis_ref is not None:
                row["Screen_Diff"] = screen_diff(gray, axis_ref)
            else:
                row["Screen_Diff"] = np.nan

            data.append(row)
            pbar.update(1)
            if progress_callback is not None and (frame_count % 100 == 0 or frame_count == total_frames - 1):
                progress_callback(current=frame_count, total=total_frames, message="OCR Processing")

    df = pd.DataFrame(data)
    print(f"[initial_ocr] {len(df)} frames processed")
    return df


# ============================================================
# Sweep tracking
# ============================================================

def track_sweep(sweep_raw, fps):
    """Convert each frame's pen tip into a continuous position with wraps unrolled (cumulative drawn columns).

    Returns:
        dict(
            pen:   pen tip per frame (screen x; gaps interpolated from neighbors),
            u:     continuous position (cumulative number of drawn columns; continuous across wraps),
            wrap:  whether the sweep returned to the left edge just before this frame (0/1),
            speed: sweep speed per frame (px/frame, rolling mean over neighboring frames),
        )
    """
    pen = np.asarray(sweep_raw, float).copy()
    n = len(pen)
    if n == 0:
        return {"pen": pen, "u": pen, "wrap": np.zeros(0, int), "speed": pen}
    valid = np.isfinite(pen)
    default_speed = Settings.SWEEP_PX_PER_SEC / fps
    if not valid.any():
        u = np.arange(n) * default_speed
        return {"pen": np.full(n, np.nan), "u": u, "wrap": np.zeros(n, int), "speed": np.full(n, default_speed)}

    # Fill gaps: within each wrap segment, linearly interpolate from neighboring detections (ends take the nearest value)
    vi = np.flatnonzero(valid)
    seg_id = np.zeros(n, int)
    jumps = vi[1:][np.diff(pen[vi]) < -Settings.SWEEP_WRAP_JUMP]
    for j in jumps:
        seg_id[j:] += 1
    for s in np.unique(seg_id):
        idx = np.flatnonzero(seg_id == s)
        ok = idx[valid[idx]]
        if len(ok):
            pen[idx] = np.interp(idx, ok, pen[ok])
    pen = np.where(np.isfinite(pen), pen, np.nanmedian(pen))

    # Speed: neighborhood mean of the advance between adjacent frames that are not wraps (0 <= d < 30)
    # (jitter in the recording's frame interval makes 4px and 6px alternate, so use the mean rather than the median)
    # 0.2.0: the window width (frames) and the max advance per frame are derived from fps (at 10 fps: 101 / 5 frames and 30px, same as 0.1.x)
    d = np.diff(pen)
    max_step = 30.0 * max(1.0, 10.0 / fps)
    step = np.where((d >= 0) & (d < max_step), d, np.nan)
    win = int(round(10.1 * fps)) | 1
    s_ser = pd.Series(np.r_[step, np.nan]).rolling(win, center=True, min_periods=max(2, round(0.5 * fps))).mean().to_numpy()
    speed = np.where(np.isfinite(s_ser), s_ser, np.nanmean(step) if np.isfinite(step).any() else default_speed)

    # Continuous position: normally add the pen tip's advance as is. A frame that returned to the left edge is treated as advancing by the current speed
    wrap = np.r_[0, (d < -Settings.SWEEP_WRAP_JUMP).astype(int)]
    adv = np.r_[0, np.where(wrap[1:] == 1, speed[1:], np.maximum(d, 0))]
    u = (pen[0] - Settings.SWEEP_X_LEFT) + np.cumsum(adv)
    return {"pen": pen, "u": u, "wrap": wrap, "speed": speed}


# ============================================================
# post_ocr
# ============================================================

SCALE_VOTE_SEC = 0.5   # 0.2.0: duration at the end of a breath used for the scale majority vote (5 frames at 10 fps, same as 0.1.x)


def _mode(series):
    s = series.dropna()
    s = s[s.astype(str) != ""]
    return s.mode().iloc[0] if len(s) else np.nan


def post_ocr(df, Analyze_Id, user_ID, settings={}):
    """
    Find breath boundaries (VT display updates) and assign breath numbers and pen tip positions.

    Returns:
        (df_frames, df_breaths)
    """
    opts = settings.get("post_ocr", {})
    fps = float(opts.get("fps", 10))
    triggers = [k for k in opts.get("breath_trigger", ["VT"]) if k in df.columns]
    min_gap = max(1, int(round(float(opts.get("min_breath_sec", 1.0)) * fps)))
    stable = max(1, int(opts.get("stable_frames", 3)))
    shift = int(round(float(opts.get("boundary_shift_sec", -1.0)) * fps))
    abnormal_thresh = float(opts.get("abnormal_screen_diff", 8.0))

    df = df.sort_values("Frame").reset_index(drop=True).copy()
    n = len(df)
    for col in OCR_KEYS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    screen = pd.to_numeric(df["Screen_Diff"], errors="coerce") if "Screen_Diff" in df else pd.Series(np.nan, index=df.index)
    popup = pd.to_numeric(df["Popup_Px"], errors="coerce") if "Popup_Px" in df else pd.Series(0, index=df.index)
    df["Abnormal"] = ((screen > abnormal_thresh) | (popup > Settings.POPUP_MIN_PIXELS)).astype(int)
    # Vertical-axis max: frames that could not be read, and values lasting fewer than `stable` frames (misreads while the scale
    # is being switched), carry over the previous value
    for name in Settings.WAVES:
        col = f"Scale_{name}"
        if col in df:
            v = pd.to_numeric(df[col], errors="coerce")
            run_len = v.groupby((v != v.shift()).cumsum()).transform("size")
            df[col] = v.where((v > 0) & (run_len >= stable)).ffill().bfill()

    # --- Breath boundaries: VT display updates (a new value lasting at least `stable` frames) ---
    state = df[triggers].astype(str).where(df[triggers].notna(), "").agg("|".join, axis=1) if triggers else pd.Series("", index=df.index)
    state = state.where(df["Abnormal"] == 0)
    state = state.where(state.str.strip("|") != "")
    state = state.ffill()
    vals = state.to_numpy(object)
    updates, current = [], None
    i = 0
    while i < n:
        v = vals[i]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            i += 1
            continue
        if current is None:
            current = v
        elif v != current:
            j = i
            while j < n and vals[j] == v:
                j += 1
            if j - i >= stable or j == n:   # ignore misreads lasting only 1-2 frames
                if not updates or i - updates[-1] >= min_gap:
                    updates.append(i)
                current = v
            i = j
            continue
        i += 1

    # Shift boundaries by boundary_shift_sec (the VT display update lags the start of inspiration by about 0.6 s)
    bounds = sorted({min(max(u + shift, 0), n - 1) for u in updates})
    df["BreathMark"] = 0
    df.loc[bounds, "BreathMark"] = 1
    df["BreathNumber"] = df["BreathMark"].cumsum()
    if not bounds or bounds[0] > 0:
        df["BreathNumber"] += 1
    df["Display_Update"] = 0
    df.loc[updates, "Display_Update"] = 1
    df["Time"] = df["Frame"].apply(lambda f: _format_time(f, fps))

    # --- Pen tip tracking ---
    sw = track_sweep(df["Sweep_Raw"].to_numpy(float) if "Sweep_Raw" in df else np.full(n, np.nan), fps)
    df["Sweep_X"] = np.round(sw["pen"], 1)
    df["Sweep_U"] = np.round(sw["u"], 2)
    df["Sweep_Wrap"] = sw["wrap"]
    df["Sweep_Speed"] = np.round(sw["speed"] * fps, 2)  # px/s
    print(f"[post_ocr] sweep detected {np.isfinite(df['Sweep_Raw']).mean() * 100:.1f}% of frames, "
          f"{int(sw['wrap'].sum())} wraps, speed {np.nanmedian(sw['speed']) * fps:.1f} px/s")

    # --- Per-breath table ---
    rows = []
    last_scale = {}   # vertical-axis max of the previous breath (breaths that could not be read carry this over)
    groups = list(df.groupby("BreathNumber", sort=True))
    for k, (no, g) in enumerate(groups):
        s, e = int(g.index[0]), int(g.index[-1])
        normal = g[g["Abnormal"] == 0]
        nxt = groups[k + 1][1] if k + 1 < len(groups) else None
        nxt_normal = nxt[nxt["Abnormal"] == 0] if nxt is not None else None
        row = {
            "BreathNumber": int(no),
            "Frame": int(df.at[s, "Frame"]),
            "End_Frame": int(df.at[e, "Frame"]),
            "Time": df.at[s, "Time"],
            "Duration": round((e - s + 1) / fps, 2),
        }
        # The on-screen VT is the measurement of the previous breath: this breath's value = the value displayed during the next breath
        for key in OCR_KEYS:
            row[key] = _mode(nxt_normal[key]) if nxt_normal is not None and key in df else np.nan
            row[f"{key}_displayed"] = _mode(normal[key]) if key in df else np.nan
        alerts = []
        if k == 0 and (not bounds or bounds[0] > 0):
            alerts.append("Fragment")
        if nxt is None:
            alerts.append("Fragment")
        if g["Abnormal"].any():
            alerts.append("Abnormal")
        row["Alert"] = ",".join(dict.fromkeys(alerts))
        row["Abnormal"] = int(g["Abnormal"].any())
        # Vertical-axis scale = that of the last frame, from which the waveform is cut. So that a single-frame misread (e.g. 100->25)
        # does not corrupt the whole breath, take a majority vote over the (normal) frames in the last SCALE_VOTE_SEC seconds. If unreadable, use the previous breath's value
        n_vote = max(1, round(SCALE_VOTE_SEC * fps))
        tail = normal.tail(n_vote) if len(normal) else g.tail(n_vote)
        for name, spec in Settings.WAVES.items():
            col = f"Scale_{name}"
            vmax = pd.to_numeric(tail[col], errors="coerce") if col in df else pd.Series(dtype=float)
            vmax = _mode(vmax[vmax > 0]) if len(vmax) else np.nan
            if not (np.isfinite(vmax) and vmax > 0):
                vmax = last_scale.get(name, np.nan)
            last_scale[name] = vmax
            vmin, vmax, _, _ = wave_scale(name, vmax)
            row[f"{spec['scale_prefix']}_min"], row[f"{spec['scale_prefix']}_max"] = vmin, vmax
        rows.append(row)
    df_breaths = pd.DataFrame(rows)

    # If VT has the same value as the previous breath, no boundary is detected and two breaths merge into one (extract_waves splits them using the waveform and re-marks)
    df_breaths = _mark_merged(df_breaths)
    df["Alert"] = df["BreathNumber"].map(df_breaths.set_index("BreathNumber")["Alert"]).fillna("")

    print(f"[post_ocr] {len(df)} frames, {len(df_breaths)} breaths ({len(updates)} display updates)")
    return df, df_breaths


# ============================================================
# extract_waves
# ============================================================

def _fill_small_gaps(v, max_gap):
    v = v.copy()
    nan = ~np.isfinite(v)
    for a, b in _runs(nan):
        if a > 0 and b < len(v) - 1 and b - a + 1 <= max_gap:
            v[a:b + 1] = np.interp(np.arange(a, b + 1), [a - 1, b + 1], [v[a - 1], v[b + 1]])
    return v


def extract_column_values(frame, xs, scale_max=None):
    """For each screen column in xs, compute the value (physical quantity) of each waveform.

    Value = the edge of the fill farthest from the baseline (top edge if more fill is above the baseline, bottom edge if more is below).
    Columns with no airway pressure fill are treated as having no waveform (erased): NaN for all waveforms.
    Columns with airway pressure fill but no flow/volume fill are 0.
    scale_max: {waveform name: vertical-axis max}. If absent, the default scale is used.
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = _wave_mask(hsv)
    xs = np.asarray(xs, int)
    out = {}
    present = None
    for name, spec in Settings.WAVES.items():
        y0, y1 = spec["band"]
        m = mask[y0:y1].copy()
        for (ex0, ey0, ex1, ey1) in Settings.WAVE_EXCLUDE.get(name, []):
            m[max(ey0 - y0, 0):max(ey1 - y0, 0), ex0:ex1] = False
        sub = m[:, xs]
        _, _, zero_y, k = wave_scale(name, (scale_max or {}).get(name))
        zero = int(round(zero_y)) - y0
        vals = np.zeros(len(xs))
        for c in np.flatnonzero(sub.any(0)):
            ys = np.flatnonzero(sub[:, c])
            up, dn = ys[ys < zero], ys[ys > zero]
            if len(up) and len(up) >= len(dn):
                vals[c] = zero - up.min()
            elif len(dn):
                vals[c] = -(dn.max() - zero)
        if name == "Pressure":
            gy0, gy1 = Settings.SWEEP_GAP_ROWS
            present = mask[gy0:gy1][:, xs].sum(0) >= 2
        out[name] = vals / k
    for name in out:
        v = np.where(present, out[name], np.nan)
        out[name] = _fill_small_gaps(v, Settings.WAVE_GAP_FILL)
    return out


def find_breath_splits(wave, rate, opts):
    """Look for the second and later inspiratory onsets within one breath's waveform.

    Inspiratory onset = start of a run where flow exceeds split_flow for at least split_insp_sec (the last point where flow was <= 1 L/min).
    If the volume has returned to baseline (fully exhaled) just before the onset, it is a separate breath -> split.
    If the next inspiration arrives before the volume fully returns, it is a single biphasic breath such as Reverse Triggering -> do not split (RT?).
      Fully exhaled = median volume over the preceding 0.2 s <= max(exhaled_ml, exhaled_frac x max volume since the previous split)
    Onsets whose new inspiratory volume is below split_min_insp_ml are ignored.

    Returns:
        (list of row indices to split at (split_lead_sec before the onset), list of row indices of RT? onsets)
    """
    flow = np.nan_to_num(wave["Flow"].to_numpy(float), nan=0.0)
    vol = wave["Volume"].to_numpy(float)
    n = len(flow)
    thr = float(opts.get("split_flow", 5.0))
    lead = int(round(float(opts.get("split_lead_sec", 0.4)) * rate))
    min_len = int(round(float(opts.get("min_breath_sec", 1.0)) * rate))
    insp_len = max(1, int(round(float(opts.get("split_insp_sec", 0.15)) * rate)))
    pre = max(1, int(round(0.2 * rate)))
    exhaled_ml = float(opts.get("exhaled_ml", 30.0))
    exhaled_frac = float(opts.get("exhaled_frac", 0.1))
    min_insp_ml = float(opts.get("split_min_insp_ml", 50.0))

    splits, rts = [], []
    last = 0
    for a, b in _runs(flow >= thr):
        if b - a + 1 < insp_len:
            continue
        o = a
        while o > 0 and flow[o - 1] > 1.0:
            o -= 1
        cut = o - lead
        if cut - last < min_len or n - cut < min_len // 2:
            continue  # this breath's own inspiration, or too close to the edge
        before = vol[max(o - pre, 0):o]
        if not np.isfinite(before).any() or not np.isfinite(vol[last:o]).any():
            continue
        v_before = float(np.nanmedian(before))
        peak = float(np.nanmax(vol[last:o]))
        rise = float(np.nanmax(vol[o:min(o + int(1.5 * rate), n)])) - v_before if np.isfinite(vol[o:]).any() else 0.0
        if rise < min_insp_ml:
            continue
        if v_before <= max(exhaled_ml, exhaled_frac * peak):
            splits.append(cut)
            last = cut
        else:
            rts.append(o)
    return splits, rts


def _mark_merged(df_breaths):
    """Mark Merged? on breaths at least 1.7x the median duration of the surrounding breaths (re-marks from scratch)."""
    alerts = df_breaths["Alert"].fillna("").astype(str).map(lambda a: [x for x in a.split(",") if x and x != "Merged?"])
    if len(df_breaths) >= 5:
        med = df_breaths["Duration"].rolling(9, center=True, min_periods=3).median()
        for i in np.flatnonzero((df_breaths["Duration"] >= 1.7 * med).to_numpy()):
            alerts.iat[i] = alerts.iat[i] + ["Merged?"]
    df_breaths["Alert"] = alerts.map(lambda a: ",".join(dict.fromkeys(a)))
    return df_breaths


# ============================================================
# Align breath boundaries to inspiratory onsets (same idea as the trimming in NAVA v1.3)
# ============================================================
# 1 breath = inspiratory onset to the next inspiratory onset.
# For breaths whose waveforms are contiguous in time, move the boundary to "TRIM_LEAD_SEC before the nearby inspiratory onset"
# and transfer rows between adjacent breaths (no data is lost). At the ends of a contiguous run (no waveform in the
# neighboring breath), trim the part before the inspiratory onset (end of the previous breath's expiration) and a final
# inspiration with no following expiration (partway into the next breath's inspiration).
ONSET_FLOW = 4.0        # flow above this (L/min) counts as inspiratory flow
ONSET_RUN_SEC = 0.06    # inspiratory flow must last at least this long
ONSET_PEAK = 12.0       # that inspiration must reach at least this (L/min)
QUIET_SEC = 0.06        # there must be at least this much non-inspiratory time before the inspiration
ONSET_FRAC = 0.2        # from the point exceeding this fraction of the inspiratory peak, backtrack to the foot of the rise
EXP_FLOW = 4.0          # expiratory flow (L/min, at least this far below 0)
BACKTRACK_TOL = 0.3     # when backtracking to the foot, continue while the flow keeps dropping by at least this (L/min)
TRIM_LEAD_SEC = 0.04    # keep data from this long before the inspiratory onset
CONTIG_TOL_SEC = 0.15   # breaths are contiguous if the time after the previous breath's last point and the time of the next breath's first point differ by at most this (s) (allows for timing estimation error)


def _inspiration_onsets(flow, rate):
    """flow: Flow (L/min, NaN allowed), rate: points/s. List of row indices of inspiratory onsets (foot of the rise)."""
    raw = np.asarray(flow, dtype=float)
    g = pd.Series(raw).interpolate(limit_area="inside").to_numpy()
    n = len(g)
    fin = np.isfinite(g)
    ins = fin & (np.nan_to_num(g) > ONSET_FLOW)
    run_min = max(2, int(round(ONSET_RUN_SEC * rate)))
    quiet_min = max(2, int(round(QUIET_SEC * rate)))
    out = []
    i = 0
    while i < n:
        if not ins[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and ins[j + 1]:
            j += 1
        quiet, k = 0, i - 1
        while k >= 0 and fin[k] and not ins[k]:
            quiet += 1
            k -= 1
        peak = np.nanmax(g[i:j + 1])
        if j - i + 1 >= run_min and peak >= ONSET_PEAK and quiet >= quiet_min:
            c = i + int(np.argmax(g[i:j + 1] >= ONSET_FRAC * peak))
            while c > 0 and fin[c - 1] and g[c - 1] < g[c] - BACKTRACK_TOL:
                c -= 1
            k = c + int(np.argmax(np.isnan(raw[c:]))) if np.isnan(raw[c:i + 1]).any() else None
            before = raw[:k][np.isfinite(raw[:k])] if k is not None else None
            if before is None or len(before) == 0 or before[-1] >= -EXP_FLOW:
                out.append(c)
        i = j + 1
    return out


def _snap_to_breaths(items):
    """items: time-ordered list of dicts {"wave": DataFrame (Flow column, rows = points) or None, "t0": seconds, "rate": points/s}.
    Writes "wave" (the re-cut waveform), "t0", "rate", "trim_head", "trim_tail" (number of points trimmed/handed over from the original range; negative = points received from neighbors) into each item."""
    # Split into groups of contiguous breaths
    groups, cur = [], []
    for i, it in enumerate(items):
        it.setdefault("trim_head", 0)
        it.setdefault("trim_tail", 0)
        if it["wave"] is None or len(it["wave"]) == 0 or not np.isfinite(it["t0"]):
            if cur:
                groups.append(cur)
            cur = []
            continue
        if cur:
            p = items[cur[-1]]
            if abs(p["t0"] + len(p["wave"]) / p["rate"] - it["t0"]) > CONTIG_TOL_SEC:
                groups.append(cur)
                cur = []
        cur.append(i)
    if cur:
        groups.append(cur)

    for g in groups:
        waves = [items[i]["wave"] for i in g]
        cat = pd.concat(waves, ignore_index=True)
        times = np.concatenate([items[i]["t0"] + np.arange(len(items[i]["wave"])) / items[i]["rate"] for i in g])
        bounds = np.r_[0, np.cumsum([len(w) for w in waves])]
        rate = float(np.median([items[i]["rate"] for i in g]))
        lead = max(1, int(round(TRIM_LEAD_SEC * rate)))
        flow = cat["Flow"].to_numpy(float)
        on = np.array(_inspiration_onsets(flow, rate), int)
        valid = cat[[c for c in cat.columns if c != "X" and not c.startswith("_")]].notna().any(axis=1).to_numpy()
        new = bounds.copy()
        # Head: empty rows and anything before the first breath's inspiratory onset
        lo = int(np.argmax(valid)) if valid.any() else 0
        first = on[on < (bounds[0] + bounds[1]) // 2]   # inspiratory onsets in the first half of the first breath
        new[0] = max(lo, int(first[0]) - lead) if len(first) else lo
        # Boundaries between breaths: among inspiratory onsets between the midpoints of the adjacent breaths, the one closest to the original boundary
        for j in range(1, len(g)):
            a = (bounds[j - 1] + bounds[j]) // 2
            b = (bounds[j] + bounds[j + 1]) // 2
            cand = on[(on > a) & (on < b)]
            if len(cand):
                o = int(cand[np.argmin(np.abs(cand - bounds[j]))])
                new[j] = o - lead
        # Tail: empty rows and a final inspiration with no following expiration (partway into the next breath's inspiration)
        hi = len(cat) - int(np.argmax(valid[::-1])) if valid.any() else len(cat)
        new[-1] = hi
        last_on = on[(on > new[-2] + lead) & (on < hi)]
        if len(last_on) and not (np.nan_to_num(flow[last_on[-1]:hi]) < -EXP_FLOW).any():
            new[-1] = max(new[-2] + 1, int(last_on[-1]) - lead)
        # Keep order (boundaries must not overtake each other)
        for j in range(1, len(new)):
            new[j] = max(new[j], new[j - 1] + 1)
        for j, i in enumerate(g):
            a, b = int(new[j]), int(new[j + 1])
            items[i]["trim_head"] = a - int(bounds[j])
            items[i]["trim_tail"] = int(bounds[j + 1]) - b
            items[i]["wave"] = cat.iloc[a:b].reset_index(drop=True)
            items[i]["t0"] = float(times[a])
            # Points received from neighboring breaths have times based on their original breath's speed, so recompute the rate from the times of the end points
            if b - a >= 2 and times[b - 1] > times[a]:
                items[i]["rate"] = float((b - a - 1) / (times[b - 1] - times[a]))
    return items


def extract_waves(df_frames, df_breaths, frames_root, output_dir, user_ID,
                  settings={}, overwrite=False, archive_dir=None):
    """
    Write a waveform CSV per breath (columns: X, Pressure, Flow, Volume) to output_dir/<BreathNumber>.csv.

    From the image of the breath's last frame, cut the columns from the one after the pen tip at the breath's start to the pen tip at its end.
    For a breath that returned to the left edge midway, join the part before the return (right side) and the part after it (left side). For a breath that returned two or more times, only the last sweep is used.
    Time of each point = Wave_T0 + row index / Wave_Rate (seconds from the start of the video).

    If VT has the same value as the previous breath, the display update is not visible and two breaths merge into one. If wave_split
    is enabled, they are split using the inspiratory onsets in the waveform (find_breath_splits). Split breaths get Boundary = "Waveform".
    Breath numbers are reassigned in time order, and BreathNumber / BreathMark / Alert in df_frames are rewritten too.

    0.3.0: if trim_to_breath (default ON), align the waveform boundaries to inspiratory onsets (_snap_to_breaths).
    The breath's Frame / End_Frame (boundaries from VT display updates) are unchanged; only the waveform and Wave_T0 / Wave_Rate change.
    The number of moved points is in Wave_Trim_Head / Wave_Trim_Tail (negative = points received from neighboring breaths).

    Returns:
        (number of breaths whose CSV was written, df_breaths)
    """
    opts = settings.get("post_ocr", {})
    fps = float(opts.get("fps", 10))
    do_split = bool(opts.get("wave_split", True))
    os.makedirs(output_dir, exist_ok=True)

    frames_in = df_frames
    df_frames = df_frames.reset_index(drop=True)
    pen = df_frames["Sweep_X"].to_numpy(float)
    u = df_frames["Sweep_U"].to_numpy(float)
    wrap = df_frames["Sweep_Wrap"].to_numpy(int)
    frame_no = df_frames["Frame"].to_numpy(int)
    times = frame_no / fps
    pos_of_frame = {int(f): i for i, f in enumerate(frame_no)}

    def frame_file(f):
        return os.path.join(frames_root, f"frame_{int(f):08d}.jpg")

    pieces = []  # (row dict, waveform DataFrame or None)
    n_split = n_rt = 0
    last_k = len(df_breaths) - 1

    for k, (_, b) in enumerate(df_breaths.iterrows()):
        row = b.to_dict()
        row.update({"Boundary": "VT" if k > 0 else "Start", "Wave_T0": np.nan, "Wave_Rate": np.nan, "Wave_Points": np.nan,
                    "Wave_Missing": np.nan, "Sweep_Start": np.nan, "Sweep_End": np.nan, "Wrapped": np.nan,
                    "Start_File_Name": frame_file(b["Frame"]), "End_File_Name": frame_file(b["End_Frame"])})
        alerts = [a for a in str(row.get("Alert") or "").split(",") if a and a != "nan"]
        s = pos_of_frame.get(int(b["Frame"]))
        e = pos_of_frame.get(int(b["End_Frame"]))
        frame = cv2.imread(row["End_File_Name"]) if os.path.isfile(row["End_File_Name"]) else None
        if s is None or e is None or frame is None or int(b.get("Abnormal", 0)) == 1:
            row["Alert"] = alerts
            pieces.append((row, None))
            continue

        # Range: (pen tip of the previous frame, pen tip of the last frame]. Of the wraps in between, only the last is used
        p0 = s - 1 if s > 0 else s
        wraps = [i for i in range(p0 + 1, e + 1) if wrap[i]]
        if len(wraps) >= 2:
            alerts.append("Long")
            p0 = wraps[-2]  # start right after the second-to-last wrap (anything earlier has been overwritten)
            wraps = wraps[-1:]
        first_x = int(np.floor(pen[p0])) + 1
        x_end = int(np.floor(pen[e]))
        if wraps:
            # After the return: left edge to the pen tip at the end. Before the return: from the column after the pen tip at the start, the remaining number of columns (from the difference in continuous position)
            n_after = max(x_end - Settings.SWEEP_X_LEFT + 1, 0)
            n_before = max(int(round(u[e] - u[p0])) - n_after, 0)
            xs = np.r_[np.arange(first_x, first_x + n_before), np.arange(Settings.SWEEP_X_LEFT, x_end + 1)]
        else:
            xs = np.arange(first_x, x_end + 1)
        if len(xs) == 0:
            row["Alert"] = alerts
            pieces.append((row, None))
            continue

        # Columns outside the drawing area (error in the estimated wrap position) keep their rows as NaN so the timing does not shift
        inside = (xs >= Settings.SWEEP_X_LEFT) & (xs < Settings.SWEEP_X_RIGHT)
        scale_max = {name: b.get(f"{spec['scale_prefix']}_max") for name, spec in Settings.WAVES.items()}
        vals = extract_column_values(_normalize_frame(frame), np.clip(xs, Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT - 1), scale_max)
        wave = pd.DataFrame({"X": np.arange(len(xs)) + int(round(u[p0])) + 1})
        for name in Settings.WAVES:
            wave[name] = np.round(np.where(inside, vals[name], np.nan), 2)
        wave["_sx"] = xs   # screen column (for Sweep_Start / End after boundaries are moved; not written out)

        # Time: interpolate from the mapping between the pen tip's continuous position u and time (stays correct when the sweep speed changes)
        t_first = float(np.interp(u[e] - len(xs) + 1, u, times))
        t_last = times[e]
        rate = (len(xs) - 1) / (t_last - t_first) if t_last > t_first else float(df_frames["Sweep_Speed"].iloc[e])

        # Add splits at inspiratory onsets in the waveform
        cuts, rts = find_breath_splits(wave, rate, opts) if do_split else ([], [])
        n_split += len(cuts)
        n_rt += len(rts)
        edges = [0, *cuts, len(xs)]
        for i, (a, z) in enumerate(zip(edges[:-1], edges[1:])):
            r = dict(row)
            t0 = t_first + a / rate
            f_start = int(b["Frame"]) if i == 0 else int(np.clip(round(t0 * fps), b["Frame"], b["End_Frame"]))
            r["Frame"] = f_start
            r["Boundary"] = row["Boundary"] if i == 0 else "Waveform"
            r["Wave_T0"] = round(t0, 4)
            r["Wave_Rate"] = round(rate, 4)
            r["Wave_Points"] = z - a
            r["Wave_Missing"] = round(float(np.isnan(wave["Pressure"].to_numpy(float)[a:z]).mean()), 3)
            r["Sweep_Start"], r["Sweep_End"] = int(xs[a]), int(xs[z - 1])
            r["Wrapped"] = int(bool(np.any(np.diff(xs[a:z]) < 0)))
            r["Start_File_Name"] = frame_file(f_start)
            piece_alerts = [x for x in alerts if not (x == "Fragment" and ((k == 0 and i > 0) or (k == last_k and i < len(edges) - 2)))]
            if r["Wave_Missing"] > 0.3:
                piece_alerts.append("Partial")   # screen was cleared (e.g. mode change) and part of the waveform is missing
            if any(a <= o < z for o in rts):
                piece_alerts.append("RT?")       # biphasic breath where the next inspiration came before full exhalation (e.g. Reverse Triggering)
            r["Alert"] = piece_alerts
            pieces.append((r, wave.iloc[a:z].reset_index(drop=True)))

    # Align waveform boundaries to inspiratory onsets (breath waveforms are contiguous, so points are moved between adjacent breaths)
    if opts.get("trim_to_breath", True):
        items = [{"wave": w, "t0": float(r["Wave_T0"]) if w is not None else np.nan,
                  "rate": float(r["Wave_Rate"]) if w is not None else np.nan} for r, w in pieces]
        _snap_to_breaths(items)
        for (r, _), it in zip(pieces, items):
            w = it["wave"]
            if w is None:
                continue
            r["Wave_Trim_Head"], r["Wave_Trim_Tail"] = it.get("trim_head", 0), it.get("trim_tail", 0)
            if len(w) == 0:
                continue
            xs_w = w["_sx"].to_numpy(int)
            r["Wave_T0"] = round(it["t0"], 4)
            r["Wave_Rate"] = round(it["rate"], 4)
            r["Wave_Points"] = len(w)
            r["Wave_Missing"] = round(float(np.isnan(w["Pressure"].to_numpy(float)).mean()), 3)
            r["Sweep_Start"], r["Sweep_End"] = int(xs_w[0]), int(xs_w[-1])
            r["Wrapped"] = int(bool(np.any(np.diff(xs_w) < 0)))
        pieces = [(r, it["wave"]) for (r, _), it in zip(pieces, items)]

    # End of a split piece = the frame before the start of the next piece (the last piece keeps the original breath's end)
    for i in range(len(pieces) - 1):
        r, nxt = pieces[i][0], pieces[i + 1][0]
        if nxt["Boundary"] == "Waveform":
            r["End_Frame"] = int(nxt["Frame"]) - 1
            r["End_File_Name"] = frame_file(r["End_Frame"])

    # Reassign breath numbers in time order and write the CSVs
    rows, written = [], 0
    for no, (r, wave) in enumerate(pieces, start=1):
        r["BreathNumber"] = no
        r["Duration"] = round((int(r["End_Frame"]) - int(r["Frame"]) + 1) / fps, 2)
        r["Time"] = _format_time(int(r["Frame"]), fps)
        r["Alert"] = ",".join(dict.fromkeys(r["Alert"]))
        if wave is not None:
            path = os.path.join(output_dir, f"{no}.csv")
            if overwrite or not os.path.exists(path):
                wave.drop(columns=["_sx"], errors="ignore").to_csv(path, index=False)
            written += 1
        rows.append(r)
    df_out = _mark_merged(pd.DataFrame(rows))
    if do_split:
        # Breaths whose waveform was examined are already split at inspiratory onsets after full exhalation. Remaining long breaths have a single
        # inspiration (followed by apnea), so remove Merged? (keep it only on breaths without a waveform)
        has_wave = np.array([w is not None for _, w in pieces])
        df_out.loc[has_wave, "Alert"] = df_out.loc[has_wave, "Alert"].map(
            lambda a: ",".join(x for x in a.split(",") if x and x != "Merged?"))

    # Update the breath numbers in df_frames (the caller's object) to match
    bn = np.zeros(len(frame_no), int)
    mark = np.zeros(len(frame_no), int)
    alert = np.full(len(frame_no), "", object)
    for r in df_out.itertuples():
        a, z = pos_of_frame.get(int(r.Frame)), pos_of_frame.get(int(r.End_Frame))
        if a is None or z is None:
            continue
        bn[a:z + 1], mark[a], alert[a:z + 1] = r.BreathNumber, 1, r.Alert
    mark[0] = 0 if df_out["Boundary"].iloc[0] == "Start" else mark[0]
    frames_in["BreathNumber"], frames_in["BreathMark"], frames_in["Alert"] = bn, mark, alert

    if archive_dir is not None:
        os.makedirs(archive_dir, exist_ok=True)
        with open(os.path.join(archive_dir, "fields.txt"), "w", encoding="utf-8") as f:
            f.write("[waves]\n")
            for name in Settings.WAVES:
                f.write(f"{name}\n")
            f.write("\n[fields]\n")
            for name in ("VT", "VT_displayed", "Duration", "Boundary", "Sweep_Start", "Sweep_End", "Wrapped", "Alert"):
                f.write(f"{name}\n")
        meta = {
            "units": {name.lower(): spec["unit"] for name, spec in Settings.WAVES.items()},
            "symmetric": {name.lower(): spec["min_rule"] == "neg_max" for name, spec in Settings.WAVES.items()},
        }
        with open(os.path.join(archive_dir, "scale_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[extract_waves] {written} breaths written ({n_split} split by waveform, {n_rt} RT?)")
    return written, df_out
