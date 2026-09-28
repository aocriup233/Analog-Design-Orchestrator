---
name: waveform-toolkit
description: Analyze and plot generic analog waveforms from CSV or bridge result JSON using windowing, overlay, sampling, reference comparison, eye diagrams, and FFT.
---

# Waveform toolkit

Use `agents/analysis/scripts/waveform_tool.py CONFIG.json`. The reusable implementation is `src/analog_agent/waveforms.py`; example requests are in `agents/analysis/config/`. Supported operations are `windows`, `overlay`, `sample`, `reference`, `eye`, and `fft`.

Record the source path, signal names, units, time origin, UI or sample rate, and plot configuration with the result. Eye plots fold by a configured UI; they do not claim BER or eye-height compliance. FFT resamples nonuniform timestamps to a uniform grid and returns the single-sided amplitude spectrum and peak, not a circuit-specific SNDR/ENOB metric. Check sampling density and aliasing before drawing circuit conclusions. Put specialized formulas in a project metric plugin and test them against a known waveform.
