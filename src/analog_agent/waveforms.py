"""Circuit-agnostic waveform operations for CSV and bridge result JSON.

Numerical/plotting dependencies are optional so netlisting and simulation can
run in the lean bridge environment. Install the ``analysis`` extra to use this.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


def _np():
    import numpy as np
    return np


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _real_value(value: Any, representation: str) -> float:
    if isinstance(value, dict) and {"real", "imag"} <= value.keys():
        value = complex(value["real"], value["imag"])
    elif isinstance(value, str):
        value = complex(value.replace("i", "j")) if "j" in value or "i" in value else float(value)
    if representation == "real":
        return float(value.real if isinstance(value, complex) else value)
    if representation == "imag":
        return float(value.imag if isinstance(value, complex) else 0.0)
    if representation == "magnitude":
        return abs(value)
    if representation == "db":
        return 20.0 * math.log10(max(abs(value), 1e-30))
    if representation == "phase_deg":
        z = complex(value)
        return math.degrees(math.atan2(z.imag, z.real))
    raise ValueError(f"Unknown representation: {representation}")


def load_trace(source: dict[str, Any], root: Path):
    """Return sorted x/y NumPy arrays from CSV or simulation_result.json."""
    np = _np()
    path = (root / source["path"]).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    x_key, y_key = source["x"], source["y"]
    if source.get("format", "csv") == "csv":
        with path.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        x = [float(row[x_key]) for row in rows]
        y = [row[y_key] for row in rows]
    elif source["format"] == "simulation_json":
        data = json.loads(path.read_text(encoding="utf-8"))["data"]
        x, y = data[x_key], data[y_key]
    else:
        raise ValueError("source.format must be csv or simulation_json")
    x = np.asarray(x, dtype=float)
    y = np.asarray([_real_value(value, source.get("representation", "real")) for value in y], dtype=float)
    if len(x) != len(y) or len(x) < 2 or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("Trace needs at least two finite x/y samples")
    if np.any(np.diff(x) <= 0):
        raise ValueError("x samples must be strictly increasing")
    return x, y


def sample_at(x, y, points: list[float]) -> list[dict[str, float]]:
    np = _np()
    values = np.asarray(points, dtype=float)
    if np.any(values < x[0]) or np.any(values > x[-1]):
        raise ValueError("Sample point outside waveform range")
    interpolated = np.interp(values, x, y)
    return [{"x": float(a), "y": float(b)} for a, b in zip(values, interpolated)]


def segments(x, y, *, start: float, width: float, count: int, step: float):
    np = _np()
    if width <= 0 or step <= 0 or count < 1:
        raise ValueError("width, step, and count must be positive")
    result = []
    for index in range(count):
        origin = start + index * step
        mask = (x >= origin) & (x <= origin + width)
        if np.count_nonzero(mask) < 2:
            raise ValueError(f"Window {index} contains fewer than two samples")
        result.append((x[mask] - origin, y[mask], origin))
    return result


def compare_reference(x, y, *, value: float | None = None, reference=None,
                      tolerance: float | None = None) -> dict[str, float | int]:
    np = _np()
    if (value is None) == (reference is None):
        raise ValueError("Supply exactly one constant value or reference trace")
    reference_values = np.full_like(y, value, dtype=float) if reference is None else np.asarray(reference, dtype=float)
    if len(reference_values) != len(y):
        raise ValueError("Reference and signal lengths differ")
    error = y - reference_values
    result: dict[str, float | int] = {
        "max_abs_error": float(np.max(np.abs(error))),
        "rms_error": float(np.sqrt(np.mean(error * error))),
        "mean_error": float(np.mean(error)),
    }
    if tolerance is not None:
        if tolerance < 0:
            raise ValueError("tolerance must be nonnegative")
        result["outside_tolerance_samples"] = int(np.count_nonzero(np.abs(error) > tolerance))
    return result


def fft_spectrum(x, y, *, window: str = "hann", remove_mean: bool = True):
    """Single-sided amplitude spectrum, scaled for the chosen window."""
    np = _np()
    if len(x) < 4:
        raise ValueError("FFT requires at least four samples")
    interval = float((x[-1] - x[0]) / (len(x) - 1))
    if interval <= 0:
        raise ValueError("Invalid time axis")
    uniform_x = x[0] + interval * np.arange(len(x))
    resampled = bool(np.max(np.abs(x - uniform_x)) > interval * 1e-4)
    if resampled:
        y = np.interp(uniform_x, x, y)
    signal = y - np.mean(y) if remove_mean else np.asarray(y, dtype=float)
    if window == "hann":
        weights = np.hanning(len(signal))
    elif window == "rectangular":
        weights = np.ones(len(signal))
    else:
        raise ValueError("window must be hann or rectangular")
    spectrum = np.fft.rfft(signal * weights)
    amplitude = 2 * np.abs(spectrum) / np.sum(weights)
    amplitude[0] /= 2
    if len(signal) % 2 == 0:
        amplitude[-1] /= 2
    frequency = np.fft.rfftfreq(len(signal), interval)
    peak_index = int(np.argmax(amplitude[1:]) + 1)
    return frequency, amplitude, {
        "sample_rate_hz": 1 / interval,
        "frequency_resolution_hz": float(frequency[1]),
        "peak_hz": float(frequency[peak_index]),
        "peak_amplitude": float(amplitude[peak_index]),
        "resampled": resampled,
    }


def run_waveform_config(path: Path) -> dict[str, Any]:
    """Run a plotting/calculation request from one JSON config file."""
    np, plt = _np(), _plt()
    path = path.resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent
    operation = config["operation"]
    options = config.get("options", {})
    output = (root / config["output"]).resolve() if config.get("output") else None
    if operation == "overlay" and config.get("sources"):
        fig, axis = plt.subplots(figsize=(8, 4.5))
        count = 0
        for source in config["sources"]:
            trace_x, trace_y = load_trace(source, root)
            axis.plot(trace_x, trace_y, label=source.get("label", source["y"]))
            count += 1
        axis.set_xlabel(config.get("x_label", "X"))
        axis.set_ylabel(config.get("y_label", "Y"))
        axis.grid(True)
        axis.legend()
        if output is None:
            raise ValueError("Multi-trace overlay needs output path")
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fig.savefig(output, dpi=int(config.get("dpi", 150)))
        plt.close(fig)
        return {"operation": operation, "traces": count, "plot": str(output)}
    x, y = load_trace(config["source"], root)
    result: dict[str, Any] = {"operation": operation, "samples": len(x)}
    fig = None
    if operation == "sample":
        result["points"] = sample_at(x, y, options["points"])
    elif operation in {"windows", "overlay", "eye"}:
        if operation == "eye":
            ui = float(options["ui"])
            windowed = segments(x, y, start=float(options.get("origin", x[0])),
                                width=2 * ui, count=int(options["count"]), step=ui)
        else:
            width = float(options["width"])
            windowed = segments(x, y, start=float(options.get("start", x[0])),
                                width=width, count=int(options["count"]),
                                step=float(options.get("step", width)))
        if operation == "windows":
            fig, axes = plt.subplots(len(windowed), 1, figsize=(8, 2.3 * len(windowed)), squeeze=False)
            for axis, (wx, wy, origin) in zip(axes[:, 0], windowed):
                axis.plot(wx, wy)
                axis.set_title(f"Start = {origin:.6g}")
                axis.grid(True)
                axis.set_ylabel(config["source"]["y"])
            axes[-1, 0].set_xlabel("Relative time (s)")
        else:
            fig, axis = plt.subplots(figsize=(8, 4.5))
            for index, (wx, wy, _) in enumerate(windowed):
                axis.plot(wx, wy, alpha=0.35 if operation == "eye" else 0.75,
                          label=None if operation == "eye" else f"Window {index}")
            axis.set_xlabel("Time within window (s)")
            axis.set_ylabel(config["source"]["y"])
            axis.grid(True)
            if operation == "overlay":
                axis.legend()
        result["windows"] = len(windowed)
    elif operation == "reference":
        reference = None
        if "source" in options:
            rx, reference = load_trace(options["source"], root)
            reference = np.interp(x, rx, reference)
        value = float(options["value"]) if "value" in options else None
        result["comparison"] = compare_reference(x, y, value=value, reference=reference,
                                                   tolerance=options.get("tolerance"))
        fig, axis = plt.subplots(figsize=(8, 4.5))
        axis.plot(x, y, label="Signal")
        axis.plot(x, np.full_like(y, value) if reference is None else reference, label="Reference")
        axis.set_xlabel(config["source"]["x"])
        axis.grid(True)
        axis.legend()
    elif operation == "fft":
        frequency, amplitude, details = fft_spectrum(x, y,
            window=options.get("window", "hann"), remove_mean=options.get("remove_mean", True))
        result["spectrum"] = details
        if config.get("spectrum_csv"):
            spectrum_path = (root / config["spectrum_csv"]).resolve()
            spectrum_path.parent.mkdir(parents=True, exist_ok=True)
            with spectrum_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["frequency_hz", "amplitude", "amplitude_db"])
                writer.writerows((float(f), float(a), float(20 * np.log10(max(a, 1e-30))))
                                 for f, a in zip(frequency, amplitude))
            result["spectrum_csv"] = str(spectrum_path)
        fig, axis = plt.subplots(figsize=(8, 4.5))
        axis.plot(frequency, 20 * np.log10(np.maximum(amplitude, 1e-30)))
        axis.set_xlabel("Frequency (Hz)")
        axis.set_ylabel("Amplitude (dB re 1 unit)")
        axis.grid(True)
    else:
        raise ValueError(f"Unsupported operation: {operation}")
    if fig is not None:
        if output is None:
            raise ValueError("Plot operation needs output path")
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.tight_layout()
        fig.savefig(output, dpi=int(config.get("dpi", 150)))
        plt.close(fig)
        result["plot"] = str(output)
    return result
