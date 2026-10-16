ENGINE_SETTINGS_SCHEMA = {
    "initial_ocr": {
        "skip_keys": {
            "type": "multiselect",
            "choices": ["VT"],
            "default": [],
            "label": "ROI Keys to be skipped"
        },
        "use_gpu": {
            "type": "bool",
            "default": True,
            "label": "Enable GPU Processing"
        },
        "diff_thresh": {
            "type": "float",
            "default": 3.0,
            "label": "Re-OCR when ROI changes more than (mean gray level)"
        }
    },
    "post_ocr": {
        "boundary_shift_sec": {
            "type": "float",
            "default": -1.0,
            "label": "Shift of breath boundary from VT update (sec)"
        },
        "stable_frames": {
            "type": "int",
            "default": 3,
            "label": "Frames a new VT must persist to count as update"
        },
        "min_breath_sec": {
            "type": "float",
            "default": 1.0,
            "label": "Minimum breath length (sec)"
        },
        "abnormal_screen_diff": {
            "type": "float",
            "default": 8.0,
            "label": "Abnormal frame threshold (axis label difference)"
        },
        "trim_to_breath": {
            "type": "bool",
            "default": True,
            "label": "Align waveform boundaries to inspiration onset"
        },
        "wave_split": {
            "type": "bool",
            "default": True,
            "label": "Split breaths at inspiration onset after full exhalation (missed VT update)"
        },
        "split_flow": {
            "type": "float",
            "default": 5.0,
            "label": "Inspiration onset: flow above (L/min)"
        },
        "split_lead_sec": {
            "type": "float",
            "default": 0.4,
            "label": "Split position before inspiration onset (sec)"
        },
        "split_insp_sec": {
            "type": "float",
            "default": 0.15,
            "label": "Inspiration onset: flow must stay above for (sec)"
        },
        "split_min_insp_ml": {
            "type": "float",
            "default": 50.0,
            "label": "Ignore inspirations smaller than (mL)"
        },
        "exhaled_ml": {
            "type": "float",
            "default": 30.0,
            "label": "Fully exhaled if volume before onset <= (mL)"
        },
        "exhaled_frac": {
            "type": "float",
            "default": 0.1,
            "label": "... or <= this fraction of preceding peak volume (else RT?)"
        }
    }
}
