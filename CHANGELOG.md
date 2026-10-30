# CHANGELOG

## 0.2.4 (2026-10-30)
Addresses the user's breath comments on the 0.2.3 analysis of the 2026-10-27 recording. Breath boundaries, VT and waveforms are unchanged.
- New Breaths.csv column `Trigger` (A: patient-triggered / C: machine / blank: unknown), declared in outputs.breath_values. The V600 draws a
  lung icon below the airway pressure band, centred on the inspiratory onset, at each patient-triggered breath; A = an icon in the first
  20 columns of the breath (comment BN 281). Against the MEDIBUS log: SPN-CPAP/PS 28 of 28 A, mandatory breaths 351 C, 5 blank
- New Alert `Popup`: a popup was over the waveforms during the breath and its waveform was read from the screen after it closed
  (comments BN 4, 5, 86, 87; 0.2.3 read them without saying so)
- New Alert `Clipped`: a waveform goes beyond the axis line or under the label / "Leak compensation" banner at the top of its band, so values
  beyond it are not on the screen (comment BN 291: VT 551 mL with the 500 mL Volume axis; the engine read 508 or, under the label, 433).
  Only a solid fill from the baseline counts (a run of at least 3 px covering 90% of the rows); below the baseline only where the axis goes
  below it (Flow, Paw), so a lone antialiased pixel under the Volume baseline is not a clip
- New Alert `Short`: shorter than 0.5 s
- No `RT?` Alert any more (comment BN 304: asynchrony is tagged by the user). Biphasic breaths are still not split
- A VT update with no inspiratory onset of its own whose boundary would be less than 0.5 s after the previous one now starts 0.5 s before
  the update (two inspirations without a pause in flow). 0.2.3 put it on the previous breath's onset and left a 1-point breath (comment BN 301)
- The "VT mL" label check counts light text too, so popups in Night Mode do not hide VT updates (VT_Label was 0 in Night Mode; comment BN 314)
- None of the new Alerts is excluded from accuracy (the user decides)
- Check against the MEDIBUS log (0.2.3 → 0.2.4): matched 387/387, VT and waveforms identical, Duration off by more than 0.5 s 3 → 1.
  Clipped: 11 true (Volume above the axis or under the label, a Flow peak under the label). Three Night Mode breaths were flagged by a lone
  pixel under the Volume baseline (fixed; re-run on frame windows: the false flag is gone and the true ones remain — two of the three did
  not reproduce in the window runs, so their absence is still to be confirmed on a full run)
- Check (V600 sample, 4K): breaths and waveforms identical to 0.2.3; new Alerts Clipped 7 (traces under a label or the banner), Popup 4, Short 1
- The comments "読めてない" on breaths at the left edge of the screen (BN 21, 29, 49 …) were the app's chart: a saved horizontal zoom in
  "screen" mode hid the start of those breaths. The data was complete; reported to the app

## 0.2.3 (2026-10-30)
Addresses the user's breath comments on the 2026-10-27 recording (0.2.1 analysis, BN 3, 5, 6, 10).
- Popups over the waveforms (e.g. the settings panel) no longer hide VT display updates while the "VT mL" label is visible
  (new Frames.csv columns `VT_Label` = overlap of its text with `vt_label_reference.png`, 0.64 in the 1080p recording and 0.985 in the
  sample, 0.12 or less elsewhere; `VT_Visible`). VT values come from those frames too. Up to 0.2.2 every breath under a popup merged into one
  breath without a waveform (BN 3-4: 25 s, 5 breaths)
- A breath whose image has a popup is read from the first later image without one (up to 30 s later); columns the pen has redrawn by
  then are missing. Such breaths no longer get the `Abnormal` Alert (kept for breaths with no readable image)
- A breath after a breath without a waveform is read from 1.75 s earlier, and its start is cut at the last inspiratory onset before
  its VT update (BN 5 started 0.3 s into its inspiration)
- `Frame`, `Time`, `Duration`, `Start_File_Name`, `End_File_Name` and `End_Frame` of breaths with a waveform follow the inspiratory onset (`Wave_T0`)
  instead of `boundary_shift_sec` before the VT update (option `frames_from_wave`, default ON). The VT update lags the onset by 0.2-1.4 s,
  so Duration was 4.0 / 5.3 s for 5 s breaths and the app's capture of a breath started in the previous one (BN 6, 10). The first breath and
  breaths without a waveform keep the frames from the update; VT / VT_displayed unchanged.
  The early return to the left edge (BN 6 comment) was already handled: the waveforms of those breaths match the ventilator log
- Check against the MEDIBUS log (0.2.2 → 0.2.3): matched breaths 383 → 387 of 387 (the 6 breaths under the 2 popups now have waveforms,
  r 0.983-0.999, VT exact); breath start (Frame) vs the log's onset median -0.32 → -0.005 s, 95% within 0.62 → 0.11 s; Duration off by
  more than 0.5 s 58 → 3 of 385; VT on clean breaths 96.5% → 96.6% exact, LoA ±2.7 mL; waveforms unchanged
- Check (V600 sample, 4K): the 3 breaths under the popup (91-106 s) became 4 breaths with waveforms; other breaths unchanged except Frame / Duration.
  Analysis time in the same conditions: extract_waves 15.8 s (0.2.2: 14.4 s; 2 more images opened); full 32-min video 325 s (0.2.1: 320 s)

## 0.2.2 (2026-10-30)
Fixes the three weaknesses found by comparing 0.2.1 with the ventilator's MEDIBUS log (2026-10-27 recording, 32 min, 387 breaths).
- Breaths longer than one sweep are read from one image per sweep (the last frame before each return to the left edge); up to 0.2.1 only the last sweep was read (Alert Long). Breaths merged because VT showed the same value several times can now be split before the last sweep. `Long` is now set only when an earlier image cannot be read
- Split pieces: every piece but the last has `VT` = the value displayed during the breath (the display did not change inside it); only the last piece has the value displayed after it. Up to 0.2.1 every piece had the value displayed after it
- Waveform splits need `min_breath_sec` before the end of the breath too (was half). An onset just before the end is the next breath's; splitting there made a 0.7 s breath before the next VT boundary
- An onset right after a gap (the right edge is erased when the sweep returns early, often just as a breath starts) can split: the volume before it is taken from the last points within 1 s
- Display updates may be 0.5 s apart (new option `min_update_sec`; was `min_breath_sec` 1.0 s). Fast spontaneous breathing had updates 0.7 s apart; the second was dropped and the VT of the following breath shifted
- A waveform boundary from a VT update moves to the last inspiratory onset 0.15-1.6 s before the update (the lag is 0.2-1.4 s by mode in the log), if that onset is after the previous boundary; otherwise to the onset nearest the boundary as before. With ~1 s breaths the boundary 1.0 s before the update was nearer the previous breath's onset
- Check against the MEDIBUS log (1080p30, 10 fps; 0.2.1 → 0.2.2): matched breaths 378 → 383 of 387; missed outside flagged breaths 5 → 0, extra outside flagged breaths 4 → 0 (the 4 missed are under 2 settings popups, flagged Abnormal). VT on clean breaths: bias -0.06 → 0.05 mL, 95% LoA ±8.2 → ±2.8 mL, exact 79.2% → 96.5%, within 5 mL 98.6% → 99.2%. Waveforms unchanged (r 0.998 / 0.991 / 0.998)
- Check (V600 sample, 4K, 10 fps): 3 more display updates (VT values shown 0.6-0.9 s), 92 breaths (90), one Long breath read whole; VT of split pieces as above. Analysis time: extract_waves 22.8 s vs 28.2 / 24.2 s for 0.2.1 in the same conditions (one more image opened)

## 0.2.1 (2026-10-30)
Fixes recordings that are brighter than the sample being analysed as a single Abnormal breath.
- The screen check (axis label column vs axis_reference.png) shifts the column's background level to the reference's before comparing. In the 2026-10-27 1080p recording (V600 data logger test) the background is 255 instead of 232, which alone gave Screen_Diff ≈ 24 (threshold 8): every frame was Abnormal, no VT update was used, and 32 min became 1 breath. With the shift it is ≈ 2.4; popups over the waveforms (e.g. the settings panel) are still Abnormal
- Check (VentOCR 0.8.0, V600 sample, 4K, 10 fps): Frames.csv (except Screen_Diff, which moves by at most 1.8), Breaths.csv (except File_Name) and waveforms identical to 0.2.0; same 84 Abnormal frames
- Check (2026-10-27 recording, 1080p30, 3 min from 270 s, 10 fps): 35 breaths, the same as the 35 inspiration starts in the MEDIBUS log over that window

## 0.2.0 (2026-10-25)
Supports engine_api 3 (VentOCR no longer saves every frame image; it creates only the images the engine opens. ENGINE_API.md §5.1).
- Images are read only when extracting waveforms: the last frame of each breath, one per breath, in time order (as before). Processing that looks at every frame happens only inside initial_ocr
- Frame counts are derived from the frame rate instead of being fixed: the window that smooths the sweep speed (101 frames at 10 fps), its minimum count (5), the maximum advance per frame (30 px; widened below 10 fps), and the frames used for the scale majority vote (5). At 10 fps the values are the same as 0.1.2
- outputs: added alerts (the meaning of each Alert value and whether it is excluded from the accuracy evaluation)
- The engine no longer uses Japanese: manifest labels, README, code comments and docstrings and .gitignore are in English. Entries for earlier versions below remain in Japanese
- Check (VentOCR e0e306b, V600 sample, 4K, 385 s): 0.1.2 (all frames saved) and 0.2.0 (only opened images created; 86 images) give identical Frames.csv, Breaths.csv (except File_Name) and waveforms for 91 breaths. At 20 fps: 93 breaths (94 at 10 fps), scale values unchanged

## 0.1.2 (2026-10-21)
解析の結果は 0.1.1 と同じ。チャートの1段目（outputs.waves の Pressure）の表示名を「Paw」に（列の名前は Pressure のまま）。

## 0.1.1 (2026-10-21)
解析の結果は 0.1.0 と同じ。manifest.json に出力の宣言 `outputs` を足した（VentOCR 0.6.2 から。ENGINE_API.md §8.1）。
- waves: Pressure（cmH2O、縦軸は P_min / P_max）・Flow（L/min、F_min / F_max）・Volume（mL、V_min / V_max）。色・単位・縦軸は VentOCR 0.6.1 までの見え方と同じ
- breath_values: VT・VT_displayed（今までの breath_summary・labels・単位と同じ）
- `ventocr_min` を 0.6.2 に（それより古いアプリは宣言を読めない）

## 0.1.0 (2026-10-19)
旧 v0.3.0 と中身は同じ。版の付け方を 0.X.Y に改めた（古い版のタグは削除）。

## 0.3.0 (2026-10-11)
### 波形の区切りを吸気の始まりへ合わせる
- 1呼吸 = 吸気の始まり 〜 次の吸気の始まり（NAVA v1.3 の「呼吸の範囲への切り詰め」と同じ考え方）。
  Flow の吸気の立ち上がり（4 L/min 超が 0.06 秒以上続き、12 L/min 以上に届き、その前に吸気でない区間が 0.06 秒以上）を見つけ、
  立ち上がりの足元（最大値の 20% を超えた点から下がり続ける所までさかのぼる）の 0.04 秒前を区切りにする（`_snap_to_breaths`）
- この機種は呼吸どうしの波形が時間の上で隙間なくつながっているので、NAVA のように削るのではなく、
  区切りを「元の区切りに最も近い吸気の始まり」へ動かし、点を前後の呼吸の間で移す（データは消えない）。
  前後の呼吸に波形が無い端だけ、吸気の始まりより前と、呼気を伴わない最後の吸気（次の呼吸の吸気の途中）を削る
- 呼吸の Frame / End_Frame（表示の更新による区切り）と OCR の値は変えない。変わるのは波形 CSV と Wave_T0 / Wave_Rate。
  動かした点の数は Breaths.csv の Wave_Trim_Head / Wave_Trim_Tail（正 = 削った / 渡した、負 = 受け取った）
- 区切り直した呼吸の Wave_Rate は、両端の点の時刻から求め直す（受け取った点は元の呼吸の速さで時刻が決まっているため）
- 設定 `post_ocr.trim_to_breath`（既定 ON）で元の動作に戻せる
- 波形で補って区切った呼吸（wave_split）の区切りも、同じく吸気の始まりへ合わせる
- 確認（resource/V600_Sample.mp4）: 91 呼吸。区切りは中央値 0.38 秒（−0.40〜0.75 秒）動いた。隣の呼吸との 0.05〜0.5 秒の隙間 1 → 0 か所、重なりなし（0.5 秒超の隙間 2 か所は異常フレームで波形が無い区間）

## 0.2.0 (2026-10-11)
- 作り直し（端末版）。サンプル: resource/V600_Sample.mp4（V600_VentOCR_Test.mp4 から切り出した 385秒、4K）
- 基準解像度を 1920x1080 に（4K は縮小して読む）
- 呼吸の区切り = VT の表示の更新（-1.0秒）。旧版は VT の値が変わったフレームをそのまま区切りにしていた
- 波形の抽出を追加（Pressure / Flow / Volume）。wrap around する掃引をフレームごとに追跡
- 縦軸の最大値を OCR し、スケールの変更に対応
- 設定画面などのポップアップが出ているフレームを Abnormal に
- OCR は VT のみ（旧版の MV・RR・Leak は削除）
- VT が同じ値で区切れなかった呼吸を、波形（吐き切った後の吸気の立ち上がり）で補って区切る。
  吐き切る前の立ち上がりは区切らず RT?（Reverse Triggering などの二峰性）

## 0.1.0
- 試作（サーバー版）。VT・MV・RR・Leak の OCR のみ
