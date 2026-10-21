"""Contract-oracle checks only; these do not claim D122 production readiness."""
import unittest

from tests.sim.d122_timing_oracle import Request, TimingOracle, transition_cases


class D122TimingContractTests(unittest.TestCase):
    def test_nonzero_ticks_and_durations_cover_all_registered_events(self):
        for landing, duration in ((26, 8), (107, 11)):
            with self.subTest(L=landing, duration=duration):
                cases = {case["event"]: case for case in transition_cases(landing, duration)}
                self.assertEqual(len(cases), 12)
                self.assertEqual(cases["never"]["events"][0], (landing, "fallback_armed", 25))
                self.assertEqual(cases["on_time"]["rows"][0]["status"], "window_applied")
                self.assertEqual(cases["on_time"]["events"][-1],
                                 (landing + 1, "window_applied", 26))
                self.assertEqual(cases["late"]["rows"][0]["movement"], "sneak")
                self.assertIn((landing + 2, "window_missed", 26), cases["late"]["events"])
                self.assertNotIn("jump", [row["movement"] for row in cases["late"]["rows"]])
                for event in ("never", "default_exhaustion", "wall_expired"):
                    rows = cases[event]["rows"]
                    self.assertEqual([row["movement"] for row in rows[:duration]], ["sneak"] * duration)
                    self.assertEqual(rows[duration]["status"], "default_exhausted")
                    self.assertTrue(all(row["movement"] == "neutral" for row in rows[duration:]))
                early = cases["early"]
                self.assertEqual([row["movement"] for row in early["rows"][:3]], ["sneak", "sneak", "jump"])
                self.assertFalse(early["rows"][0]["response_ready"])
                self.assertTrue(early["rows"][2]["response_ready"])
                self.assertEqual(early["admissions"], 1)
                self.assertEqual(early["activation_checks"], 3)
                for event in ("cancel", "reset", "episode", "permission"):
                    self.assertTrue(all(row["movement"] == "neutral" for row in cases[event]["rows"]))
                self.assertEqual(cases["ordinary"]["rows"][0]["movement"], "walk")
                self.assertTrue(all(row["movement"] == "neutral" for row in cases["ordinary"]["rows"][1:]))
                higher = cases["higher_sequence"]
                self.assertEqual([row["movement"] for row in higher["rows"][:2]], ["sneak", "jump"])
                self.assertEqual(higher["admissions"], 2)

    def test_pending_activation_does_not_reconsume_sequence_or_observation(self):
        model = TimingOracle(landing_tick=71, duration=9)
        request = Request(26, window=(74, 74))
        self.assertEqual(model.admit(request), "window_pending")
        model.observation = 40
        for _ in range(4):
            movement, status = model.sample()
        self.assertEqual((movement, status), ("jump", "window_applied"))
        self.assertEqual(model.admissions, 1)
        self.assertEqual(model.last_sequence, 26)
        self.assertEqual(model.admit(request), "rejected_identity")

    def test_missed_response_waits_until_default_finishes(self):
        model = TimingOracle(landing_tick=90, duration=12)
        model.sample()
        model.sample()
        self.assertEqual(model.admit(Request(26, window=(91, 91))), "window_pending")
        while model.tick < 102:
            self.assertEqual(model.sample()[0], "sneak")
            self.assertFalse(model.response_ready)
        self.assertEqual(model.sample(), ("neutral", "default_exhausted"))
        self.assertTrue(model.response_ready)

    def test_default_wall_clock_starts_at_activation_not_parent_admission(self):
        model = TimingOracle(landing_tick=200, duration=8, default_wall_budget=50)
        self.assertEqual(model.sample(now=100), ("parent", "leased"))
        self.assertEqual(model.sample(now=1_000), ("sneak", "default_active"))
        self.assertEqual(model.sample(now=1_049), ("sneak", "default_active"))
        self.assertEqual(model.sample(now=1_050), ("sneak", "default_active"))
        self.assertEqual(model.default_wall_started, 1_000)
        for _ in range(5):
            self.assertEqual(model.sample(now=2_000), ("sneak", "default_active"))
        self.assertEqual(model.sample(now=2_001), ("neutral", "default_exhausted"))


if __name__ == "__main__":
    unittest.main()
