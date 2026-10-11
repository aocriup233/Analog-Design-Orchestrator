import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent.campaign import run_until_review, propose, _lock
from analog_agent.analysis_agent import extract


class CampaignSecurityTest(unittest.TestCase):
    def test_idle_loop_sleeps_even_without_scheduler_job(self):
        report = {"status": "ACTIVE", "policy": {"poll_interval_s": 0}, "cycles": [
            {"points": [{"state": "STAGED", "issue": None}]}]}
        ended = {**report, "status": "REVIEW"}
        with patch("analog_agent.campaign.step", side_effect=[report, report, ended]), \
                patch("analog_agent.campaign.time.sleep") as sleep:
            self.assertEqual(run_until_review(Path(".")), ended)
            self.assertGreaterEqual(sleep.call_args.args[0], 1)

    def test_propose_holds_campaign_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            def proposer(path):
                with self.assertRaises((RuntimeError, PermissionError)):
                    with _lock(path):
                        pass
                return {"review_required": True}
            with patch("analog_agent.campaign._propose_locked", side_effect=proposer):
                self.assertTrue(propose(campaign)["review_required"])

    def test_ac_bounds_and_log_frequency_interpolation(self):
        data = {"ac_freq": [10, 1000], "gain": [1, 100]}
        definition = {"kind": "ac_db_at", "signal": "gain", "at_hz": 100}
        self.assertEqual(extract(data, definition), 0)
        self.assertAlmostEqual(extract(data, {**definition, "interpolate": True}), 20)
        for target in (1, 1001, float("nan")):
            with self.assertRaises(ValueError):
                extract(data, {**definition, "at_hz": target})
        self.assertEqual(extract(data, {**definition, "at_hz": 1000, "interpolate": True}), 40)
