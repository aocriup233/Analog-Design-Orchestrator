from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from analog_agent.waveforms import compare_reference, fft_spectrum, load_trace, run_waveform_config, sample_at, segments


class WaveformTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.x = np.arange(1000) / 10000.0
        self.y = np.sin(2 * np.pi * 1000 * self.x)
        with (self.root / "wave.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time", "out"])
            writer.writerows(zip(self.x, self.y))

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, operation, options, output="plot.png"):
        config = {"source": {"path": "wave.csv", "x": "time", "y": "out"},
                  "operation": operation, "options": options, "output": output}
        path = self.root / "request.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return run_waveform_config(path)

    def test_sample_reference_and_fft(self):
        self.assertAlmostEqual(sample_at(self.x, self.y, [0.00025])[0]["y"],
                               np.sin(2 * np.pi * 1000 * 0.0002), places=2)
        comparison = compare_reference(self.x, self.y, value=0, tolerance=0.5)
        self.assertGreater(comparison["outside_tolerance_samples"], 0)
        _, _, detail = fft_spectrum(self.x, self.y)
        self.assertAlmostEqual(detail["peak_hz"], 1000, delta=10)
        self.assertAlmostEqual(detail["peak_amplitude"], 1.0, delta=0.02)
        irregular = self.x + 1e-6 * np.sin(2 * np.pi * np.arange(len(self.x)) / 23)
        _, _, irregular_detail = fft_spectrum(irregular, np.sin(2 * np.pi * 1000 * irregular))
        self.assertTrue(irregular_detail["resampled"])
        self.assertAlmostEqual(irregular_detail["peak_hz"], 1000, delta=10)

    def test_plot_operations(self):
        requests = [
            ("windows", {"start": 0, "width": 0.002, "count": 3}, "windows.png"),
            ("overlay", {"start": 0, "width": 0.002, "count": 3}, "overlay.png"),
            ("eye", {"origin": 0, "ui": 0.001, "count": 3}, "eye.png"),
            ("reference", {"value": 0, "tolerance": 0.5}, "reference.png"),
            ("fft", {"window": "hann"}, "fft.png"),
        ]
        for operation, options, output in requests:
            with self.subTest(operation=operation):
                result = self._run(operation, options, output)
                self.assertTrue(Path(result["plot"]).is_file())
        result = self._run("sample", {"points": [0, 0.001]}, output="")
        self.assertEqual(len(result["points"]), 2)

    def test_multi_trace_overlay_and_phase(self):
        config = {"sources": [
            {"path": "wave.csv", "x": "time", "y": "out", "label": "first"},
            {"path": "wave.csv", "x": "time", "y": "out", "label": "second"},
        ], "operation": "overlay", "output": "multi.png"}
        path = self.root / "multi.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        result = run_waveform_config(path)
        self.assertEqual(result["traces"], 2)
        self.assertTrue((self.root / "multi.png").is_file())
        (self.root / "ac.json").write_text(json.dumps({"data": {
            "ac_freq": [1e6, 2e6],
            "ac_VOUT": [{"real": 0, "imag": 1}, {"real": -1, "imag": 0}],
        }}), encoding="utf-8")
        _, phase = load_trace({"path": "ac.json", "format": "simulation_json",
                               "x": "ac_freq", "y": "ac_VOUT",
                               "representation": "phase_deg"}, self.root)
        self.assertEqual(list(phase), [90.0, 180.0])


if __name__ == "__main__":
    unittest.main()
