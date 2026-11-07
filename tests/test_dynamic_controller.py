"""Deadline-window regressions using fake time and fake API responses only."""
import copy
import io
import unittest
import urllib.error
from unittest import mock

from test_external_controller import FakeAPI, MID, NOW, ec, machine


class DynamicControllerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.now = NOW
        self.state, self.logs, self.snapshots = {}, [], []
        self.controller = ec.Controller(
            {"machine_ids": [MID]}, self.api, self.state,
            lambda: self.snapshots.append(copy.deepcopy(self.state)),
            self.logs.append, lambda: self.now)

    @property
    def target(self):
        return self.state["machines"][MID]

    def tick(self, advance=0):
        self.now += advance
        self.api.now = self.now
        return self.controller.tick()

    def running_for(self, minutes, cap=120):
        self.api.machine = machine(run=self.now - minutes * 60, maxRunMinutes=cap)

    def test_110_minutes_preserves_the_official_extension_opportunity(self):
        self.running_for(110)
        self.tick()
        self.assertFalse(self.api.mutations())
        self.assertEqual(self.target["observed_max_run_minutes"], 120)
        self.assertEqual(self.target["runtime_deadline_at"], NOW + 600)
        self.assertEqual(self.target["rotation_due_at"], NOW + 480)

    def test_official_extension_at_115_minutes_moves_the_rotation_deadline(self):
        self.running_for(114)
        self.tick()
        old_due = self.target["rotation_due_at"]
        self.api.machine["maxRunMinutes"] = 240
        self.tick(60)
        self.tick(180)  # The old 118-minute boundary must no longer trigger.
        self.assertFalse(self.api.mutations())
        self.assertEqual(self.target["rotation_due_at"], old_due + 7200)
        changes = [row for row in self.logs if row["event"] == "runtime_limit_changed"]
        self.assertTrue(changes)

    def test_unextended_120_minute_run_rotates_at_118_minutes(self):
        self.running_for(117)
        self.tick()
        self.assertFalse(self.api.mutations())
        self.tick(60)
        self.assertTrue(self.api.mutations()[0][1].endswith("/suspend"))
        self.assertEqual(self.target["cycle"]["reason"], "runtime_limit")
        self.assertEqual(self.target["cycle"]["stage"], "suspend")

    def test_final_confirmation_observes_a_late_extension_before_suspend(self):
        self.running_for(118)
        request = self.api.request
        reads = []

        def extend_on_second_read(method, path, body=None, key=None):
            if method == "GET" and "/operations/" not in path:
                reads.append(path)
                if len(reads) == 2:
                    self.api.machine["maxRunMinutes"] = 240
            return request(method, path, body, key)

        self.api.request = extend_on_second_read
        self.tick()
        self.assertEqual(len(reads), 2)
        self.assertFalse(self.api.mutations())
        self.assertNotIn("cycle", self.target)
        self.assertEqual(self.target["observed_max_run_minutes"], 240)

    def test_definitely_rejected_suspend_is_cancelled_after_official_extension(self):
        self.running_for(118)
        self.api.failures["POST"] = [ec.APIError(409, "operation_conflict")]
        self.tick()
        self.assertEqual(len(self.api.mutations()), 1)
        self.assertEqual(self.target["cycle"]["reason"], "runtime_limit")
        self.assertNotIn("pending", self.target["cycle"])
        self.api.machine["maxRunMinutes"] = 240
        self.tick(15)
        self.assertEqual(len(self.api.mutations()), 1)
        self.assertNotIn("cycle", self.target)
        self.assertEqual(self.target["phase"], "running")
        self.assertTrue(any(row["event"] == "cycle_rescheduled" for row in self.logs))

    def test_unlimited_platform_cap_retains_the_seven_hour_schedule(self):
        self.running_for(419, cap=-1)
        self.tick()
        self.assertFalse(self.api.mutations())
        self.tick(60)
        self.assertTrue(self.api.mutations()[0][1].endswith("/suspend"))
        self.assertEqual(self.target["cycle"]["reason"], "scheduled_rotation")

    def test_runtime_limit_cycle_still_holds_for_at_least_30_seconds(self):
        self.running_for(118)
        self.tick()
        self.api.machine["state"] = "suspended"
        self.tick(10)
        self.tick(20)
        self.assertEqual(len(self.api.mutations()), 1)
        self.tick(10)
        self.assertTrue(self.api.mutations()[-1][1].endswith("/resume"))

    def test_deadline_window_retries_transient_failures_every_15_seconds(self):
        self.running_for(114)
        self.tick()
        self.api.failures["GET"] = [ec.APIError(503, "internal")] * 3
        self.tick(60)  # First failure occurs at the official evaluation boundary.
        self.assertEqual(self.target["retry_at"], self.now + 15)
        count = len(self.api.calls)
        self.tick(14)
        self.assertEqual(len(self.api.calls), count)
        self.tick(1)
        self.assertEqual(self.target["consecutive_errors"], 2)
        self.assertEqual(self.target["retry_at"], self.now + 15)
        self.tick(15)
        self.assertEqual(self.target["consecutive_errors"], 3)
        self.api.machine["maxRunMinutes"] = 240
        self.tick(15)
        self.assertEqual(self.target["consecutive_errors"], 0)
        self.assertFalse(self.api.mutations())

    def test_retry_interval_starts_after_the_failed_request_finishes(self):
        self.running_for(114)
        self.tick()
        request = self.api.request

        def slow_failure(method, path, body=None, key=None):
            if method == "GET":
                self.now += 15
                raise ec.APIError(0, "transport_or_response_error")
            return request(method, path, body, key)

        self.api.request = slow_failure
        self.tick(60)
        self.assertEqual(self.target["retry_at"], NOW + 60 + 15 + 15)

    def test_long_official_retry_after_is_not_shortened_by_deadline_urgency(self):
        self.running_for(114)
        self.tick()
        official_retry = self.now + 600
        self.api.failures["GET"] = [ec.APIError(429, "rate_limited", official_retry)]
        self.tick(60)
        self.assertEqual(self.target["retry_at"], official_retry)
        count = len(self.api.calls)
        self.tick(300)
        self.assertEqual(len(self.api.calls), count)
        self.assertFalse(self.api.mutations())

    def test_platform_cooldown_reached_during_outage_wins_over_rotation(self):
        self.running_for(114)
        self.tick()
        self.api.failures["GET"] = [ec.APIError(503)]
        self.tick(60)
        self.api.machine["state"] = "suspended"
        self.api.machine["cooldownUntil"] = ec.iso(self.now + 3600)
        self.tick(15)
        self.assertEqual(self.target["phase"], "cooldown_wait")
        self.assertFalse(self.api.mutations())

    def test_fast_window_loop_survives_a_transient_failure_and_observes_extension(self):
        self.running_for(114)
        self.tick()
        self.api.failures["GET"] = [ec.APIError(503)]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.now += seconds
            self.api.now = self.now
            if self.now >= NOW + 20:
                self.api.machine["maxRunMinutes"] = 240

        result = ec.run_ticks(self.controller, budget=60,
                              monotonic=lambda: self.now, sleep=sleep)
        self.assertEqual(result, 0)
        self.assertEqual(sleeps, [10, 10])
        self.assertEqual(self.target["observed_max_run_minutes"], 240)
        self.assertFalse(self.api.mutations())

    def test_fast_window_loop_is_bounded_if_all_requests_keep_failing(self):
        self.running_for(114)
        self.tick()
        self.api.failures["GET"] = [ec.APIError(503)] * 10
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.now += seconds

        self.assertEqual(ec.run_ticks(self.controller, budget=40,
                                     monotonic=lambda: self.now, sleep=sleep), 1)
        self.assertLessEqual(sum(sleeps), 40)
        self.assertFalse(self.api.mutations())

    def test_failed_operation_preserves_body_only_wait_and_cause(self):
        self.api.machine = machine("suspended")
        request = self.api.request

        def capacity_failure(method, path, body=None, key=None):
            result = request(method, path, body, key)
            if method == "POST":
                result.update(state="failed", error={"code": "no_capacity",
                              "retryable": True, "retryAfterSeconds": 300})
            return result

        self.api.request = capacity_failure
        self.tick()
        self.assertEqual(self.target["error"]["code"], "no_capacity")
        self.assertTrue(self.target["error"]["retryable"])
        self.assertEqual(self.target["retry_at"], NOW + 300)
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 1)

    def test_failed_cooldown_operation_becomes_wait_without_reposting(self):
        self.api.machine = machine("suspended")
        request = self.api.request

        def cooldown_failure(method, path, body=None, key=None):
            result = request(method, path, body, key)
            if method == "POST":
                result.update(state="failed", error={"code": "machine_cooldown",
                              "retryable": True, "retryAfterSeconds": 3600,
                              "reason": "abuse_proxy"})
            return result

        self.api.request = cooldown_failure
        self.tick()
        self.assertEqual(self.target["cycle"]["stage"], "hold")
        self.assertEqual(self.target["cycle"]["cooldown_until"], NOW + 3600)
        self.tick(600)
        self.assertEqual(len(self.api.mutations()), 1)


class DynamicWireTests(unittest.TestCase):
    def test_http_and_body_wait_use_the_later_valid_deadline(self):
        for header, body_seconds, expected in [("60", 180, 180), ("240", 30, 240)]:
            with self.subTest(header=header):
                error = ec.APIError.from_payload(429, {"code": "rate_limited",
                    "retryAfterSeconds": body_seconds, "retryable": True}, NOW, header)
                self.assertEqual(error.retry_at, NOW + expected)
                self.assertTrue(error.retryable)

    def test_api_reads_body_wait_without_logging_private_message(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "url", 409, "private", {}, io.BytesIO(
                b'{"error":{"code":"machine_cooldown","retryable":true,'
                b'"reason":"abuse_proxy","retryAfterSeconds":7200,"message":"SECRET"}}'))
        with self.assertRaises(ec.APIError) as caught:
            ec.API("placeholder", opener, lambda: NOW).request("POST", "/resume")
        self.assertEqual(caught.exception.retry_at, NOW + 7200)
        self.assertEqual(caught.exception.reason, "abuse_proxy")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_margin_stays_after_the_five_minute_official_evaluation(self):
        for margin in [89, 241, True]:
            with self.subTest(margin=margin), self.assertRaises(ValueError):
                ec.validate_config({"machine_ids": [MID], "runtime_margin_seconds": margin})


if __name__ == "__main__":
    unittest.main()
