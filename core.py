"""Dräger V600 VentOCR Engine

- 1呼吸 = VT の表示が更新されてから、次に更新されるまで
- OCR: VT（リーク補正）
- 波形: 気道内圧（Pressure）、フロー（Flow）、ボリューム（Volume）。基線から値までを塗りつぶした波形

波形の画面は左から右へ掃引し、左端に戻って上書きする（wrap around）。ただし戻る位置・掃引の速さは一定でない
（右端の手前で次の呼吸が始まると戻る、モード変更で全消去、モードで速さが変わる）ため、周期を仮定しない。
各フレームでペン先（消去帯の左端）を検出し（initial_ocr）、折り返しを展開した通しの位置に直す（post_ocr）。
呼吸の波形は、その呼吸の最後のフレームの画像から、開始時のペン先〜終了時のペン先の列を切り出す（extract_waves）。
"""
import json
import os
import re

import cv2
import numpy as np
import pandas as pd
from tqdm import tqdm

from . import Settings

ENGINE_VERSION = "0.3.0"
VENTOCR_TESTED = "0.5.5"

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_KEYS = list(Settings.ROIs)


# ============================================================
# EasyOCR初期化
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
# 共通
# ============================================================

def _normalize_frame(frame):
    """基準解像度（1920x1080）にそろえる。"""
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
    """True が続く区間の (開始, 終了) のリスト（終了を含む）。"""
    idx = np.flatnonzero(np.diff(np.r_[0, mask.astype(np.int8), 0]))
    return list(zip(idx[0::2], idx[1::2] - 1))


def _wave_mask(hsv):
    """波形の塗り（濃い青・薄い青）の画素。"""
    d = np.abs(hsv[..., 0].astype(np.int16) - Settings.WAVE_HUE)
    return (d <= Settings.WAVE_HUE_TOL) & (hsv[..., 1] >= Settings.WAVE_SAT_MIN) & (hsv[..., 2] >= Settings.WAVE_VAL_MIN)


def _load_axis_reference():
    path = os.path.join(ENGINE_DIR, Settings.AXIS_REFERENCE_IMAGE)
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE) if os.path.isfile(path) else None
    return None if img is None else img.astype(np.int16)


def _format_time(frame, fps):
    """フレーム番号からHH:MM:SS:CC形式のタイムスタンプを生成"""
    total_seconds = frame / fps
    s = int(total_seconds)
    centiseconds = int((total_seconds - s) * 100)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}:{centiseconds:02d}"


def detect_pen(frame):
    """ペン先の x（画面座標）。見つからなければ NaN。

    気道内圧の帯は常に塗られているので、塗りの無い列の区間からペン先を探す（ペン先 = 区間の左端の1つ手前）。
      1. 消去帯: 長さが SWEEP_GAP_LEN の範囲で、右側に（1周前の）塗りがある区間
      2. 無ければ、右端まで続く区間（最初の1周・画面の消去の直後・ペン先が右端に近く消去帯が端で切れたとき）
    右端の手前で左端に戻ったときは、右側の消去された部分も右端まで続く区間になるが、1 の消去帯が優先される。
    ペン先が右端の近くにあると、消去帯が右端で折り返して左端側にもはみ出す。左端から始まる区間と右端まで続く区間の
    長さの合計が消去帯の幅なら、ペン先は右端側にある。
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
    """縦軸の最大値 → (最小値, 最大値, 基線のy, px/単位)。"""
    spec = Settings.WAVES[name]
    vmax = float(max_value) if max_value and np.isfinite(max_value) and max_value > 0 else float(spec["default_max"])
    vmin = {"fixed": float(spec.get("min_value", 0)), "neg_max": -vmax, "zero": 0.0}[spec["min_rule"]]
    k = (spec["bottom"] - spec["top"]) / (vmax - vmin)
    return vmin, vmax, spec["top"] + vmax * k, k


def popup_pixels(frame):
    """ポップアップ（設定画面など）の枠の色の画素数。"""
    x, y, w, h = Settings.POPUP_AREA
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    (h0, h1), (s0, s1), (v0, v1) = Settings.POPUP_HSV
    m = (hsv[..., 0] >= h0) & (hsv[..., 0] <= h1) & (hsv[..., 1] >= s0) & (hsv[..., 1] <= s1) & (hsv[..., 2] >= v0) & (hsv[..., 2] <= v1)
    return int(m.sum())


# ============================================================
# OCR結果パース
# ============================================================

def _parse_ocr(result):
    """EasyOCR の結果 → (値の文字列, 生の文字列, 信頼度の文字列)。VT は整数（mL）。"""
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
    各フレームで VT と縦軸の最大値を読み取り、ペン先の位置・ポップアップの有無を記録する。

    VT は、読み取り位置の画像が前回OCRしたときから変わったフレームだけOCRし、それ以外は前の値を引き継ぐ。

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

            # 縦軸の最大値（数字の画像が変わったときだけOCR）
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
                row["Screen_Diff"] = round(float(np.abs(_crop(gray, Settings.AXIS_LABEL_ROI).astype(np.int16) - axis_ref).mean()), 2)
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
# 掃引の追跡
# ============================================================

def track_sweep(sweep_raw, fps):
    """各フレームのペン先を、折り返しを展開した通しの位置（描いた列の累計）に直す。

    Returns:
        dict(
            pen:   各フレームのペン先（画面のx。欠けは前後から補間）,
            u:     通しの位置（描いた列の数の累計。折り返しをまたいでも連続）,
            wrap:  このフレームの直前で左端に戻ったか（0/1）,
            speed: 各フレームの掃引の速さ（px/フレーム、近傍の中央値）,
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

    # 欠けを埋める: 折り返しの区間ごとに、前後の検出値から直線で補間（端は最寄りの値）
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

    # 速さ: 折り返しでない隣どうしの進み（0 <= d < 30）の近傍平均
    # （収録のフレーム間隔の揺らぎで 4px と 6px が交互に出るので、中央値ではなく平均）
    d = np.diff(pen)
    step = np.where((d >= 0) & (d < 30), d, np.nan)
    s_ser = pd.Series(np.r_[step, np.nan]).rolling(101, center=True, min_periods=5).mean().to_numpy()
    speed = np.where(np.isfinite(s_ser), s_ser, np.nanmean(step) if np.isfinite(step).any() else default_speed)

    # 通しの位置: 普段はペン先の進みをそのまま足す。左端に戻ったフレームはその時点の速さだけ進めたとみなす
    wrap = np.r_[0, (d < -Settings.SWEEP_WRAP_JUMP).astype(int)]
    adv = np.r_[0, np.where(wrap[1:] == 1, speed[1:], np.maximum(d, 0))]
    u = (pen[0] - Settings.SWEEP_X_LEFT) + np.cumsum(adv)
    return {"pen": pen, "u": u, "wrap": wrap, "speed": speed}


# ============================================================
# post_ocr
# ============================================================

SCALE_VOTE_FRAMES = 5


def _mode(series):
    s = series.dropna()
    s = s[s.astype(str) != ""]
    return s.mode().iloc[0] if len(s) else np.nan


def post_ocr(df, Analyze_Id, user_ID, settings={}):
    """
    呼吸の区切り（VT の表示の更新）を求め、呼吸番号・ペン先の位置を付ける。

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
    # 縦軸の最大値: 読めなかったフレームと、stable フレーム未満しか続かない値（スケールを切り替えている最中の読み違い）は
    # 前の値を引き継ぐ
    for name in Settings.WAVES:
        col = f"Scale_{name}"
        if col in df:
            v = pd.to_numeric(df[col], errors="coerce")
            run_len = v.groupby((v != v.shift()).cumsum()).transform("size")
            df[col] = v.where((v > 0) & (run_len >= stable)).ffill().bfill()

    # --- 呼吸の区切り: VT の表示の更新（新しい値が stable フレーム以上続いたもの） ---
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
            if j - i >= stable or j == n:   # 1〜2フレームだけの読み違いは無視
                if not updates or i - updates[-1] >= min_gap:
                    updates.append(i)
                current = v
            i = j
            continue
        i += 1

    # 区切りの位置を boundary_shift_sec だけずらす（VT の表示の更新は吸気の始まりより約0.6秒遅れる）
    bounds = sorted({min(max(u + shift, 0), n - 1) for u in updates})
    df["BreathMark"] = 0
    df.loc[bounds, "BreathMark"] = 1
    df["BreathNumber"] = df["BreathMark"].cumsum()
    if not bounds or bounds[0] > 0:
        df["BreathNumber"] += 1
    df["Display_Update"] = 0
    df.loc[updates, "Display_Update"] = 1
    df["Time"] = df["Frame"].apply(lambda f: _format_time(f, fps))

    # --- ペン先の追跡 ---
    sw = track_sweep(df["Sweep_Raw"].to_numpy(float) if "Sweep_Raw" in df else np.full(n, np.nan), fps)
    df["Sweep_X"] = np.round(sw["pen"], 1)
    df["Sweep_U"] = np.round(sw["u"], 2)
    df["Sweep_Wrap"] = sw["wrap"]
    df["Sweep_Speed"] = np.round(sw["speed"] * fps, 2)  # px/秒
    print(f"[post_ocr] sweep detected {np.isfinite(df['Sweep_Raw']).mean() * 100:.1f}% of frames, "
          f"{int(sw['wrap'].sum())} wraps, speed {np.nanmedian(sw['speed']) * fps:.1f} px/s")

    # --- 呼吸ごとの表 ---
    rows = []
    last_scale = {}   # 直前の呼吸の縦軸の最大値（読めなかった呼吸はこれを引き継ぐ）
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
        # 画面の VT は直前の呼吸の測定値: この呼吸の値 = 次の呼吸の間に表示される値
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
        # 縦軸のスケール = 波形を切り出す最後のフレームのもの。1フレームの誤読（100→25 など）で呼吸全体が
        # 狂わないよう、最後の SCALE_VOTE_FRAMES フレーム（正常なもの）の多数決にする。読めなければ直前の呼吸の値
        tail = normal.tail(SCALE_VOTE_FRAMES) if len(normal) else g.tail(SCALE_VOTE_FRAMES)
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

    # VT が前の呼吸と同じ値だと区切れず、2呼吸が1つになる（extract_waves で波形から補って区切り、付け直す）
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
    """画面の列 xs ごとに、各波形の値（物理量）を求める。

    値 = 基線から最も遠い塗りの端（基線より上の塗りが多ければ上端、下が多ければ下端）。
    気道内圧の塗りが無い列は波形が無い（消去済み）とみなし、全波形 NaN。
    気道内圧の塗りがあってフロー・ボリュームの塗りが無い列は 0。
    scale_max: {波形名: 縦軸の最大値}。無ければ既定のスケール。
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
    """1呼吸の波形の中にある、2つ目以降の吸気の立ち上がりを調べる。

    吸気の立ち上がり = フローが split_flow を超えて split_insp_sec 以上続く区間の始まり（フローが 1 L/min 以下だった最後の点）。
    立ち上がりの直前でボリュームが基線まで戻っていれば（吐き切っていれば）別の呼吸 → 区切る。
    戻り切る前に次の吸気が来ていれば Reverse Triggering などの二峰性の1呼吸 → 区切らない（RT?）。
      吐き切った = 直前 0.2秒のボリュームの中央値 <= max(exhaled_ml, exhaled_frac × 直前の区切りからの最大ボリューム)
    新しい吸気のボリュームが split_min_insp_ml 未満の立ち上がりは無視する。

    Returns:
        (区切る行番号のリスト（立ち上がりの split_lead_sec 前）, RT? の立ち上がりの行番号のリスト)
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
            continue  # この呼吸自身の吸気、または端に近すぎる
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
    """長さが前後の呼吸の中央値の1.7倍以上の呼吸に Merged? を付ける（付け直す）。"""
    alerts = df_breaths["Alert"].fillna("").astype(str).map(lambda a: [x for x in a.split(",") if x and x != "Merged?"])
    if len(df_breaths) >= 5:
        med = df_breaths["Duration"].rolling(9, center=True, min_periods=3).median()
        for i in np.flatnonzero((df_breaths["Duration"] >= 1.7 * med).to_numpy()):
            alerts.iat[i] = alerts.iat[i] + ["Merged?"]
    df_breaths["Alert"] = alerts.map(lambda a: ",".join(dict.fromkeys(a)))
    return df_breaths


# ============================================================
# 呼吸の区切りを吸気の始まりへ合わせる（NAVA v1.3 の切り詰めと同じ考え方）
# ============================================================
# 1呼吸 = 吸気の始まり 〜 次の吸気の始まり。
# 波形が時間の上で隙間なくつながっている呼吸どうしは、区切りを「近くの吸気の始まりの TRIM_LEAD_SEC 前」へ動かし、
# 行を前後の呼吸の間で移す（データは消えない）。つながりの端（前後の呼吸に波形が無い）は、
# 吸気の始まりより前（前の呼吸の呼気の終わり）と、呼気を伴わない最後の吸気（次の呼吸の吸気の途中）を削る。
ONSET_FLOW = 4.0        # Flow がこれ（L/min）を超えたら吸気の流れ
ONSET_RUN_SEC = 0.06    # 吸気の流れがこれ以上続くこと
ONSET_PEAK = 12.0       # その吸気がこれ（L/min）以上に届くこと
QUIET_SEC = 0.06        # 吸気の前に、吸気でない区間がこれ以上あること
ONSET_FRAC = 0.2        # 吸気の最大値のこの割合を超えた点から、立ち上がりの足元までさかのぼる
EXP_FLOW = 4.0          # 呼気の流れ（L/min, 0 よりこれ以上下）
BACKTRACK_TOL = 0.3     # 足元までさかのぼるとき、これ（L/min）以上下がり続ける間はさかのぼる
TRIM_LEAD_SEC = 0.04    # 吸気の始まりのこの時間前から残す
CONTIG_TOL_SEC = 0.15   # 前の呼吸の最後の点の次の時刻と、次の呼吸の最初の点の時刻の差がこれ（秒）以内ならつながっている（時刻の推定の誤差を見込む）


def _inspiration_onsets(flow, rate):
    """flow: Flow（L/min, NaN 可）, rate: 点/秒。吸気の始まり（立ち上がりの足元）の行番号の一覧。"""
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
    """items: 時間順の dict のリスト {"wave": DataFrame（Flow 列, 行=点）または None, "t0": 秒, "rate": 点/秒}。
    各 item に "wave"（切り直した波形）, "t0", "rate", "trim_head", "trim_tail"（元の範囲から削った/渡した点の数。負は前後から受け取った点）を書き込む。"""
    # つながっている呼吸のまとまりに分ける
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
        # 先頭: 空の行と、最初の呼吸の吸気の始まりより前
        lo = int(np.argmax(valid)) if valid.any() else 0
        first = on[on < (bounds[0] + bounds[1]) // 2]   # 最初の呼吸の前半にある吸気の始まり
        new[0] = max(lo, int(first[0]) - lead) if len(first) else lo
        # 呼吸の間の区切り: 前後の呼吸の真ん中より内側にある吸気の始まりのうち、元の区切りに最も近いもの
        for j in range(1, len(g)):
            a = (bounds[j - 1] + bounds[j]) // 2
            b = (bounds[j] + bounds[j + 1]) // 2
            cand = on[(on > a) & (on < b)]
            if len(cand):
                o = int(cand[np.argmin(np.abs(cand - bounds[j]))])
                new[j] = o - lead
        # 末尾: 空の行と、呼気を伴わない最後の吸気（次の呼吸の吸気の途中）
        hi = len(cat) - int(np.argmax(valid[::-1])) if valid.any() else len(cat)
        new[-1] = hi
        last_on = on[(on > new[-2] + lead) & (on < hi)]
        if len(last_on) and not (np.nan_to_num(flow[last_on[-1]:hi]) < -EXP_FLOW).any():
            new[-1] = max(new[-2] + 1, int(last_on[-1]) - lead)
        # 順序を保つ（区切りが追い越さない）
        for j in range(1, len(new)):
            new[j] = max(new[j], new[j - 1] + 1)
        for j, i in enumerate(g):
            a, b = int(new[j]), int(new[j + 1])
            items[i]["trim_head"] = a - int(bounds[j])
            items[i]["trim_tail"] = int(bounds[j + 1]) - b
            items[i]["wave"] = cat.iloc[a:b].reset_index(drop=True)
            items[i]["t0"] = float(times[a])
            # 前後の呼吸から受け取った点は元の呼吸の速さで時刻が決まっているので、速さは両端の点の時刻から求め直す
            if b - a >= 2 and times[b - 1] > times[a]:
                items[i]["rate"] = float((b - a - 1) / (times[b - 1] - times[a]))
    return items


def extract_waves(df_frames, df_breaths, frames_root, output_dir, user_ID,
                  settings={}, overwrite=False, archive_dir=None):
    """
    呼吸ごとの波形CSV（列: X, Pressure, Flow, Volume）を output_dir/<BreathNumber>.csv に書き出す。

    呼吸の最後のフレームの画像から、呼吸の開始時のペン先の次の列〜終了時のペン先の列を切り出す。
    途中で左端に戻った呼吸は、戻る前の部分（右側）と戻った後の部分（左側）をつなぐ。2回以上戻った呼吸は最後の1周分だけ。
    各点の時刻 = Wave_T0 + 行番号 / Wave_Rate（秒、動画の先頭から）。

    VT が前の呼吸と同じ値だと表示の更新が見えず、2呼吸が1つになる。wave_split が有効なら、波形の吸気の立ち上がりで
    補って区切る（find_breath_splits）。区切った呼吸は Boundary = "Waveform"。呼吸番号は時間順に振り直し、
    df_frames の BreathNumber / BreathMark / Alert も書き換える。

    0.3.0: trim_to_breath（既定 ON）なら、波形の区切りを吸気の始まりへ合わせる（_snap_to_breaths）。
    呼吸の Frame / End_Frame（VT の表示の更新による区切り）は変えず、波形と Wave_T0 / Wave_Rate だけを変える。
    動かした点の数は Wave_Trim_Head / Wave_Trim_Tail（負は前後の呼吸から受け取った点）。

    Returns:
        (CSV を書き出した呼吸の数, df_breaths)
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

    pieces = []  # (行の dict, 波形の DataFrame または None)
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

        # 範囲: (前のフレームのペン先, 最後のフレームのペン先]。途中の折り返しは最後のものだけ使う
        p0 = s - 1 if s > 0 else s
        wraps = [i for i in range(p0 + 1, e + 1) if wrap[i]]
        if len(wraps) >= 2:
            alerts.append("Long")
            p0 = wraps[-2]  # 最後から2番目の折り返しの直後から（それより前は上書き済み）
            wraps = wraps[-1:]
        first_x = int(np.floor(pen[p0])) + 1
        x_end = int(np.floor(pen[e]))
        if wraps:
            # 戻った後: 左端〜終了時のペン先。戻る前: 開始時のペン先の次の列から、残りの列数（通しの位置の差から求める）
            n_after = max(x_end - Settings.SWEEP_X_LEFT + 1, 0)
            n_before = max(int(round(u[e] - u[p0])) - n_after, 0)
            xs = np.r_[np.arange(first_x, first_x + n_before), np.arange(Settings.SWEEP_X_LEFT, x_end + 1)]
        else:
            xs = np.arange(first_x, x_end + 1)
        if len(xs) == 0:
            row["Alert"] = alerts
            pieces.append((row, None))
            continue

        # 描画領域の外になった列（折り返し位置の推定誤差）は、時刻がずれないよう行を残して NaN にする
        inside = (xs >= Settings.SWEEP_X_LEFT) & (xs < Settings.SWEEP_X_RIGHT)
        scale_max = {name: b.get(f"{spec['scale_prefix']}_max") for name, spec in Settings.WAVES.items()}
        vals = extract_column_values(_normalize_frame(frame), np.clip(xs, Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT - 1), scale_max)
        wave = pd.DataFrame({"X": np.arange(len(xs)) + int(round(u[p0])) + 1})
        for name in Settings.WAVES:
            wave[name] = np.round(np.where(inside, vals[name], np.nan), 2)
        wave["_sx"] = xs   # 画面の列（区切りを動かした後の Sweep_Start / End 用。書き出さない）

        # 時刻: ペン先の通しの位置 u と時刻の対応から内挿（掃引の速さが変わっても合う）
        t_first = float(np.interp(u[e] - len(xs) + 1, u, times))
        t_last = times[e]
        rate = (len(xs) - 1) / (t_last - t_first) if t_last > t_first else float(df_frames["Sweep_Speed"].iloc[e])

        # 波形の吸気の立ち上がりで補って区切る
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
                piece_alerts.append("Partial")   # モード変更などで画面が消去され、波形の一部が残っていない
            if any(a <= o < z for o in rts):
                piece_alerts.append("RT?")       # 吐き切る前に次の吸気が来た二峰性の呼吸（Reverse Triggering など）
            r["Alert"] = piece_alerts
            pieces.append((r, wave.iloc[a:z].reset_index(drop=True)))

    # 波形の区切りを吸気の始まりへ合わせる（呼吸どうしの波形はつながっているので、点を前後の呼吸の間で移す）
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

    # 区切った片の終わり = 次の片の開始の1つ前のフレーム（最後の片は元の呼吸の終わり）
    for i in range(len(pieces) - 1):
        r, nxt = pieces[i][0], pieces[i + 1][0]
        if nxt["Boundary"] == "Waveform":
            r["End_Frame"] = int(nxt["Frame"]) - 1
            r["End_File_Name"] = frame_file(r["End_Frame"])

    # 呼吸番号を時間順に振り直し、CSV を書き出す
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
        # 波形を調べた呼吸は、吐き切った後の吸気の立ち上がりで区切り済み。残った長い呼吸は吸気が1回だけ（後に無呼吸）なので
        # Merged? を外す（波形が無い呼吸だけ残す）
        has_wave = np.array([w is not None for _, w in pieces])
        df_out.loc[has_wave, "Alert"] = df_out.loc[has_wave, "Alert"].map(
            lambda a: ",".join(x for x in a.split(",") if x and x != "Merged?"))

    # df_frames（呼び出し元のもの）の呼吸番号も合わせる
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
