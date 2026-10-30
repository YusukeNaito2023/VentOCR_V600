# V600 VentOCR Engine

## Supported Devices
- Dräger Savina 300 / V600 (screen with VT, MV, respiratory rate, and leak % on the right, and three waveforms — airway pressure / flow / volume — on the left)
- Videos recorded at 3840x2160 or 1920x1080 (downscaled to 1920x1080 before reading)

## Description
- **1 breath** = from one update of the VT display until the next update
  - The display update lags the start of inspiration by 0.2-1.4 s depending on the mode, so the boundary is first placed `boundary_shift_sec` (default -1.0 s) before the update.
    The waveform boundary is then moved to the last inspiratory onset 0.15-1.6 s before the update (0.2.2), or, if there is none, to the onset nearest the boundary
  - A change counts as an update only when the new value lasts at least `stable_frames` (default 3) frames (misreads of 1-2 frames are ignored)
    and comes at least `min_update_sec` (default 0.5 s; 0.2.2, was `min_breath_sec` 1.0 s) after the previous update
- **Waveform-based splits** (`wave_split`, enabled by default): if VT has the same value as the previous breath, the display update is not visible and two breaths merge into one.
  If a breath's waveform contains a second inspiratory onset (flow > `split_flow` 5 L/min) and the volume has returned
  to baseline (fully exhaled) just before it, it is split off as a separate breath (`split_lead_sec` 0.4 s before the onset).
  - Fully exhaled = volume over the preceding 0.2 s ≤ max(`exhaled_ml` 30 mL, `exhaled_frac` 0.1 × preceding max volume)
  - If the next inspiration arrives before full exhalation (a single biphasic breath, e.g. Reverse Triggering), it is not split and Alert gets `RT?`
  - Onsets whose new inspiratory volume is below `split_min_insp_ml` (50 mL) are ignored, and so are onsets less than `min_breath_sec` (1.0 s)
    after the previous split or before the end of the breath (0.2.2: the end limit was half of it; an onset just before the end belongs to the next breath)
  - Split breaths have `Boundary` = `Waveform` in Breaths.csv (breaths split by a VT update have `VT`, and the first breath has `Start`).
    The display did not change inside the merged breath, so every piece but the last has `VT` = the value displayed during the breath (`VT_displayed`),
    and the last piece has the value displayed after it (0.2.2; up to 0.2.1 every piece had the value displayed after it)
  - A breath longer than one sweep is read from one image per sweep (the last frame before each return to the left edge), so it can be split too (0.2.2)
  - Breath numbers are reassigned in time order (BreathNumber in Frames.csv is updated to match)
- **OCR**: VT (leak-compensated). The on-screen VT is the value of the "previous breath", so `VT` in Breaths.csv is the value displayed during the next breath,
  and `VT_displayed` is the value that was displayed during that breath
- **Waveforms**: Pressure, Flow, Volume. Columns of BreathCSVs/<n>.csv are X, Pressure, Flow, Volume
  - Waveforms are drawn filled from the baseline to the value (mandatory breaths in dark blue, spontaneous breaths in light blue). Value of each column = the edge of the fill farthest from the baseline
  - The vertical-axis scale (flow ±100/±50, volume 1000/500, etc.) can be changed on screen, and past waveforms are redrawn too.
    The vertical-axis max number is OCR'd, and each breath is converted using the scale of the frame its waveform was cut from (P_/F_/V_min and max in Breaths.csv)
- **Sweep**: drawn left to right, then returns to the left edge and overwrites (wrap around). The return position and speed are not constant, so the pen tip is tracked in every frame
  - It returns to the left edge (x=62) when it reaches the right edge (x=1362), or when the next breath starts at x≈1100 or beyond (the remaining right side is erased)
  - Changing the ventilation mode clears the whole screen
  - The speed depends on the mode (52 px/s in the sample, 64 px/s in the PC-BIPAP segment)
  - Pen tip = in the airway pressure band, one column before the left end of the unfilled columns (erase band, about 41px wide)
  - Time of each point = `Wave_T0` + row index / `Wave_Rate` (Breaths.csv)

## Expected Layout
See `layout.jpg` (red boxes: OCR, green boxes: waveform bands, red lines: baselines, gray boxes: areas not read, blue box: vertical-axis ticks used for the screen check).

## Alert (Breaths.csv)
- `RT?`: biphasic breath where the next inspiration came before full exhalation (e.g. Reverse Triggering). Not split
- `Merged?`: a breath at least 1.7x the median duration of the surrounding breaths that could not be split by waveform because it has no waveform
  (not set on breaths whose waveform was examined, since those are already split; remaining long breaths have a single inspiration followed by apnea)
- `Abnormal`: a popup such as a settings screen was shown over the waveforms (no waveform is output)
- `Partial`: the screen was cleared (e.g. mode change) and 30% or more of the waveform is missing
- `Long`: a breath longer than one sweep whose earlier sweeps could not be read (only the sweeps after the unreadable image are used). Up to 0.2.1 every breath longer than one sweep
- `Fragment`: the first and last breaths of the recording

## Limitations
- When VT has the same value as the previous breath, the display update is not visible. Breaths are split using the waveform instead (8 places in the sample; this happens more often when VT is stable, as in PC-AC)
- The airway pressure vertical-axis minimum is assumed to be fixed at -5 (the maximum is read). Maximum values other than 30 are unverified
- With PEEP 0 (no airway pressure fill), the pen tip cannot be detected

## Author
- VentOCR Developer Team
