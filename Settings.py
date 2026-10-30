# V600 Engine Settings
# All coordinates are screen coordinates at the base resolution (1920x1080). Input frames (recorded at 3840x2160) are downscaled to this resolution before use.
# Reference image: layout.jpg (from resource/V600_Sample.mp4, which was cut from V600_VentOCR_Test.mp4)

BASE_RESOLUTION = (1920, 1080)  # (width, height)

# ------------------------------------------------------------
# Numeric value read positions (x, y, w, h)
# ------------------------------------------------------------
ROIs = {
    "VT": (1500, 180, 155, 105),   # VT mL (leak-compensated). The small 1000 / 250 on the right are alarm upper/lower limits, so they are excluded
}

# Screen check (frames that are not the waveform screen are marked Abnormal): the column of waveform vertical-axis tick numbers
AXIS_LABEL_ROI = (0, 130, 62, 670)        # (x, y, w, h)
AXIS_REFERENCE_IMAGE = "axis_reference.png"

# 0.2.3: the "VT mL" label next to the VT value. A popup over the waveforms (Abnormal) leaves it visible, so VT display updates
# are still used there. Visible = overlap (IoU) of its dark text pixels with the reference's at least VT_LABEL_MIN_IOU
# (normal and popup frames 0.64 in both the 4K sample and the 2026-10-27 1080p recording; other parts of the screen 0.12 or less)
VT_LABEL_ROI = (1395, 155, 95, 35)        # (x, y, w, h)
VT_LABEL_REFERENCE_IMAGE = "vt_label_reference.png"
VT_LABEL_MIN_IOU = 0.4

# 0.2.4: patient trigger. The V600 draws a lung icon (about 22 px wide, centred on the inspiratory onset) just below the airway
# pressure band at each patient-triggered breath. Icon column = at least TRIGGER_ICON_MIN_PX pixels in TRIGGER_ICON_ROWS that
# differ from the strip's background by more than 60 (either way, for Night Mode)
TRIGGER_ICON_ROWS = (305, 330)
TRIGGER_ICON_MIN_PX = 3
TRIGGER_ICON_SEARCH_PX = 20   # columns from the breath's first point (2 points before the onset) where the icon is looked for

# ------------------------------------------------------------
# Waveform drawing area and sweep
# ------------------------------------------------------------
# The sweep draws rightward from the left edge X_LEFT. The point at which it returns to the left edge is not fixed:
#   - when it reaches the right edge (X_RIGHT)
#   - when the next breath starts before the right edge (x≈1100 or beyond) (the remaining right side is erased)
#   - when the ventilation mode is changed (the whole screen is cleared)
# The sweep speed also depends on the mode (52 px/s, 64 px/s, etc.). So no period is assumed; the pen tip is detected and tracked in every frame.
SWEEP_X_LEFT = 62
SWEEP_X_RIGHT = 1362          # this column is not drawn
SWEEP_PX_PER_SEC = 52.0       # default sweep speed (used only when it cannot be detected)
# Pen tip detection: the airway pressure band is always filled (0 to PEEP), so the erase band (~41px) ahead of the pen tip appears as a blank.
# The pen tip is one column before the left end of a run of unfilled columns (core.detect_pen).
SWEEP_GAP_ROWS = (170, 302)   # part of the airway pressure band excluding the label text (y<170)
SWEEP_GAP_LEN = (30, 55)      # accepted range for the erase band width (measured 41px)
SWEEP_WRAP_JUMP = 100         # if the pen tip moves left by at least this much, treat it as a wrap (or a clear)

# ------------------------------------------------------------
# Waveforms (CSV column name -> on-screen band and vertical axis)
# ------------------------------------------------------------
# Waveforms are drawn filled from the baseline (0) to the value. Value of each column = the edge of the fill farthest from the baseline.
# Colors are dark blue (mandatory breaths) and light blue (spontaneous breaths). Both have hue ≈ 104 (OpenCV HSV).
#
# The vertical-axis scale can be changed on screen (e.g. flow ±100 -> ±50, volume 1000 -> 500), and changing it redraws past waveforms too.
# So the vertical-axis max number (max_roi) is OCR'd, and each breath is converted using "the scale of the frame the waveform was cut from".
#   max is at y=top, min is at y=bottom. min is determined by min_rule (fixed: min_value, neg_max: -max, zero: 0)
# top / bottom were calibrated against known on-screen values (2026-10-11, PC-AC segment of V600_Sample.mp4, default scale):
#   Pressure: fill bottom edge y302 = 0, PEEP 5.0 -> y275.3, inspiratory pressure 20.0 -> y195.5 (5.33 px/cmH2O) -> 30 = y142, -5 = y328.7
#   Volume:   fill bottom edge y793 = 0, VT 538 mL <-> fill height 99-102 px (0.186 px/mL) -> 1000 = y607
#   Flow:     baseline y468 = 0. Value at which the integral of inspiratory flow matches the volume increase (1.0 px/(L/min)) -> 100 = y368
# Verified that extracted values match the redrawn screen in segments with a changed scale (flow ±50 / volume 500) as well.
WAVES = {
    "Pressure": {"band": (142, 330), "top": 142, "bottom": 328.7, "min_rule": "fixed", "min_value": -5,
                 "default_max": 30, "max_roi": (10, 142, 50, 22),
                 "unit": "cmH2O", "scale_prefix": "P"},
    "Flow":     {"band": (368, 570), "top": 368, "bottom": 568, "min_rule": "neg_max",
                 "default_max": 100, "max_roi": (10, 374, 50, 22),
                 "unit": "L/min", "scale_prefix": "F"},
    "Volume":   {"band": (598, 795), "top": 607, "bottom": 793, "min_rule": "zero",
                 "default_max": 1000, "max_roi": (10, 605, 50, 22),
                 "unit": "mL", "scale_prefix": "V"},
}
SCALE_OCR_ZOOM = 3    # vertical-axis numbers are small (14px tall), so upscale before OCR

# Areas within the bands that are not read (x0, y0, x1, y1): label text and the "Leak compensation is active." banner
WAVE_EXCLUDE = {
    "Pressure": [(0, 130, 310, 170)],
    "Flow":     [(0, 360, 250, 400), (735, 355, 1365, 386)],
    "Volume":   [(0, 595, 280, 632)],
}
WAVE_HUE = 104
WAVE_HUE_TOL = 10
WAVE_SAT_MIN = 40
WAVE_VAL_MIN = 60
WAVE_GAP_FILL = 6     # gaps up to this width (px) are filled by linear interpolation

# Frames where a popup (grayish-blue frame, e.g. a settings screen) appears over the waveforms are marked Abnormal
POPUP_AREA = (62, 130, 1318, 680)          # (x, y, w, h)
POPUP_HSV = ((100, 112), (60, 120), (110, 160))  # hue / saturation / value ranges
POPUP_MIN_PIXELS = 15000                   # normal frames still have 3000-4000 such pixels at fill edges. With a popup, around 50,000
