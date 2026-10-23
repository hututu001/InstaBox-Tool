"""Offline regression: no account, real machine, network, or actual sleeps."""
import copy
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import external_controller as ec

MID = "m_example"
NOW = 1_800_000_000


def machine(state="running", run=NOW - 100, cooldown=None, **kw):
    return {"id": MID, "state": state, "runStartedAt": ec.iso(run) if run else None,
            "cooldownUntil": ec.iso(cooldown) if cooldown else None,
            "runtime": {"ready": True}, "operationId": None, "maxRunMinutes": 480, **kw}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.machine = machine()
        self.failures = {}
        self.operation_state = "queued"
        self.now = NOW
        self.operations = {}

    def request(self, method, path, body=None, key=None):
        self.calls.append((method, path, copy.deepcopy(body), key))
        errors = self.failures.get(method, [])
        if errors:
            raise errors.pop(0)
        if method == "GET" and "/operations/" not in path:
            return copy.deepcopy(self.machine)
        if method == "PUT":
            return {"id": "lease", "machineId": MID, "expiresAt": ec.iso(self.now + 3600)}
        if method == "POST":
            op = {"id": "op_" + str(len(self.operations) + 1), "machineId": MID,
                  "type": path.rsplit("/", 1)[1], "state": self.operation_state}
            self.operations[op["id"]] = op
            return copy.deepcopy(op)
        if method == "GET":
            result = copy.deepcopy(self.operations[path.rsplit("/", 1)[1]])
            result["state"] = self.operation_state
            return result
        raise AssertionError((method, path))

    def mutations(self, method="POST"):
        return [call for call in self.calls if call[0] == method]


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.now = NOW
        self.state, self.logs, self.snapshots = {}, [], []
        self.controller = ec.Controller({"machine_ids": [MID]}, self.api, self.state,
                                        self.save, self.logs.append, lambda: self.now)

    def save(self):
        self.snapshots.append(copy.deepcopy(self.state))

    @property
    def target(self):
        return self.state["machines"][MID]

    def tick(self, advance=0):
        self.now += advance
        self.api.now = self.now
        return self.controller.tick()

    def start_cooldown(self, until=NOW + 600):
        self.api.machine = machine("suspended", cooldown=until)
        self.tick()

    def test_ten_minute_renewal_one_hour_ttl(self):
        self.tick()
        self.tick(60)
        self.assertEqual(len(self.api.mutations("PUT")), 1)
        self.tick(540)
        self.assertEqual(len(self.api.mutations("PUT")), 2)
        self.assertEqual(self.api.mutations("PUT")[0][2], {"ttlSeconds": 3600})

    def test_failed_renewal_retries_next_minute_not_next_ten_minutes(self):
        self.api.failures["PUT"] = [ec.APIError(503, "unavailable")]
        self.assertEqual(self.tick(), 1)
        self.assertNotIn("last_success", self.target)
        self.assertEqual(self.tick(60), 0)
        self.assertEqual(len(self.api.mutations("PUT")), 2)

    def test_failed_get_never_mutates_and_retries(self):
        self.api.failures["GET"] = [ec.APIError()]
        self.tick()
        self.assertFalse(self.api.mutations())
        self.tick(60)
        self.assertTrue(self.api.mutations("PUT"))

    def test_seven_hour_cycle_and_fresh_lease(self):
        self.api.machine = machine(run=NOW - 25200)
        self.tick()
        post = self.api.mutations()[0]
        self.assertTrue(post[1].endswith("/suspend"))
        self.assertIsNone(post[2])
        self.assertTrue(post[3].startswith("ib-cloud-"))
        self.assertTrue(any(x["machines"][MID].get("cycle", {}).get("pending", {}).get("key") == post[3]
                            for x in self.snapshots))
        self.api.machine["state"] = "suspended"
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 1)
        self.tick(20)
        self.assertEqual(len(self.api.mutations()), 1)
        self.tick(40)
        self.assertTrue(self.api.mutations()[-1][1].endswith("/resume"))
        self.api.machine = machine(run=self.now + 30)
        self.tick(60)
        self.assertNotIn("cycle", self.target)
        self.assertEqual(self.target["run_started_at"], self.now - 30)
        self.assertEqual(self.target["last_success"], self.now)
        self.assertEqual(len(self.api.mutations("PUT")), 2)

    def test_cooldown_wait_uses_deadline_not_a_new_ten_minutes_each_tick(self):
        self.start_cooldown()
        self.tick(300)
        self.assertFalse(self.api.mutations())
        self.tick(300)
        self.assertTrue(self.api.mutations()[-1][1].endswith("/resume"))

    def test_late_observation_waits_only_remaining_cooldown(self):
        self.now += 420
        self.start_cooldown()
        self.tick(180)
        self.assertEqual(len(self.api.mutations()), 1)

    def test_cooldown_resume_keeps_retrying_after_multiple_failures(self):
        self.start_cooldown()
        self.api.failures["POST"] = [ec.APIError(503, "unavailable") for _ in range(4)]
        for advance in [600, 60, 60, 60, 60]:
            self.tick(advance)
        posts = self.api.mutations()
        self.assertEqual(len(posts), 5)
        self.assertEqual(len({call[3] for call in posts}), 1)
        self.assertIn("operation_id", self.target["cycle"]["pending"])

    def test_cooldown_409_extends_wait_then_retries_with_fresh_key(self):
        self.start_cooldown()
        self.api.failures["POST"] = [ec.APIError(409, "machine_cooldown", NOW + 720)]
        self.tick(600)
        first_key = self.api.mutations()[0][3]
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 1)
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 2)
        self.assertNotEqual(first_key, self.api.mutations()[-1][3])

    def test_cooldown_409_without_header_rechecks_next_minute(self):
        self.start_cooldown()
        self.api.failures["POST"] = [ec.APIError(409, "machine_cooldown")]
        self.tick(600)
        self.tick(30)
        self.assertEqual(len(self.api.mutations()), 1)
        self.tick(30)
        self.assertEqual(len(self.api.mutations()), 2)

    def test_cooldown_probe_uses_updated_official_deadline(self):
        self.start_cooldown()
        self.api.failures['POST'] = [ec.APIError(409, 'machine_cooldown')]
        self.tick(600)
        self.api.machine['cooldownUntil'] = ec.iso(NOW + 720)
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 1)
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 2)

    def test_unmanaged_pause_automatically_resumes_without_lease_history(self):
        self.api.machine = machine('suspended')
        self.tick()
        self.assertEqual(self.target['cycle']['reason'], 'ensure_online')
        self.assertTrue(self.api.mutations()[0][1].endswith('/resume'))

    def test_unexpected_pause_recovers_and_renews_with_new_run_clock(self):
        self.api.machine = machine('suspended')
        self.api.failures['POST'] = [ec.APIError(503)] * 3
        for advance in [0, 60, 60, 60]:
            self.tick(advance)
        self.assertEqual(len({call[3] for call in self.api.mutations()}), 1)
        self.api.machine = machine(run=self.now + 5)
        self.tick(60)
        self.assertNotIn('cycle', self.target)
        self.assertEqual(self.target['last_success'], self.now)
        self.assertEqual(self.target['run_started_at'], self.now - 55)

    def test_unexpected_pause_honors_cooldown_rejection(self):
        self.api.machine = machine('suspended')
        self.api.failures['POST'] = [ec.APIError(409, 'machine_cooldown', NOW + 600)]
        self.tick()
        self.tick(300)
        self.assertEqual(len(self.api.mutations()), 1)
        self.tick(300)
        self.assertEqual(len(self.api.mutations()), 2)

    def test_maintenance_prevents_mutations_and_can_be_disabled(self):
        self.controller.config['maintenance'] = True
        self.api.machine = machine('suspended', cooldown=NOW + 60)
        ec.run_ticks(self.controller, sleep=lambda _: self.fail('must not fast poll'))
        self.assertFalse(self.api.mutations())
        self.assertFalse(self.api.mutations('PUT'))
        self.assertTrue(ec.health(self.state, self.now)['ok'])
        self.controller.config['maintenance'] = False
        self.tick(60)
        self.assertTrue(self.api.mutations()[0][1].endswith('/resume'))

    def test_maintenance_preserves_pending_intent_without_advancing_it(self):
        self.api.machine = machine(run=NOW - 25200)
        self.tick()
        key = self.target['cycle']['pending']['key']
        self.controller.config['maintenance'] = True
        self.tick(1200)
        self.assertEqual(len(self.api.mutations()), 1)
        self.assertEqual(self.target['cycle']['pending']['key'], key)
        self.assertTrue(ec.health(self.state, self.now)['ok'])

    def test_readiness_wait_preserves_lease_without_repeating_renewal(self):
        self.api.machine = machine(runtime={'ready': False})
        self.tick()
        self.tick(60)
        self.assertEqual(len(self.api.mutations('PUT')), 1)
        self.tick(540)
        self.assertEqual(len(self.api.mutations('PUT')), 2)
        self.assertEqual(self.target['phase'], 'runtime_wait')

    def test_cooldown_does_not_hide_api_failures(self):
        self.start_cooldown(until=NOW + 3600)
        self.api.failures['GET'] = [ec.APIError(503)] * 3
        for _ in range(3):
            self.tick(60)
        self.assertIn({'machine_id': MID, 'issue': 'repeated_api_failure'},
                      ec.health(self.state, self.now)['issues'])

    def test_fast_polling_finishes_cycle_and_returns_to_timer(self):
        self.api.machine = machine(run=NOW - 25200)
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            self.now += seconds
            self.api.now = self.now
            cycle = self.target.get('cycle', {})
            if cycle.get('stage') == 'suspend':
                self.api.machine['state'] = 'suspended'
            elif cycle.get('stage') == 'resume':
                self.api.machine = machine(run=self.now)
        result = ec.run_ticks(self.controller, monotonic=lambda: self.now, sleep=sleep)
        self.assertEqual(result, 0)
        self.assertEqual(sleeps, [10] * 5)
        self.assertEqual(self.target['phase'], 'running')
        self.assertNotIn('cycle', self.target)

    def test_fast_polling_never_sleeps_through_cooldown_or_failure(self):
        self.api.machine = machine('suspended', cooldown=NOW + 600)
        with mock.patch.object(ec.time, 'sleep') as sleeper:
            ec.run_ticks(self.controller, sleep=sleeper)
            sleeper.assert_not_called()

    def test_cooldown_success_restarts_seven_hours_and_renews_immediately(self):
        self.start_cooldown()
        self.tick(600)
        self.api.machine = machine(run=self.now + 5, cooldown=NOW + 600)
        self.tick(60)
        self.assertNotIn("cycle", self.target)
        self.assertEqual(self.target["phase"], "running")
        self.assertEqual(self.target["last_success"], self.now)
        self.tick(600)
        self.assertEqual(len(self.api.mutations()), 1)

    def test_expired_cooldown_from_previous_run_is_ignored(self):
        self.api.machine = machine(run=NOW - 100, cooldown=NOW - 1000)
        self.tick()
        self.assertNotIn("cycle", self.target)
        self.assertFalse(self.api.mutations())

    def test_old_cooldown_does_not_block_recovery_of_later_pause(self):
        self.api.machine = machine(run=NOW - 100, cooldown=NOW - 1000)
        self.tick()
        self.api.machine["state"] = "suspended"
        self.tick(60)
        self.assertEqual(self.target["phase"], "resume_requested")
        self.assertTrue(self.api.mutations()[-1][1].endswith('/resume'))

    def test_expired_cooldown_for_current_run_recovers_without_another_wait(self):
        self.api.machine = machine("suspended", run=NOW - 30000, cooldown=NOW - 60)
        self.tick()
        self.assertEqual(len(self.api.mutations()), 1)

    def test_restart_preserves_cooldown_deadline(self):
        self.start_cooldown()
        restored = json.loads(json.dumps(self.state))
        self.controller = ec.Controller({"machine_ids": [MID]}, self.api, restored,
                                        lambda: None, self.logs.append, lambda: self.now)
        self.tick(600)
        self.assertEqual(len(self.api.mutations()), 1)

    def test_lost_suspend_response_reuses_key_after_restart(self):
        self.api.machine = machine(run=NOW - 25200)
        self.api.failures["POST"] = [ec.APIError()]
        self.tick()
        first = self.api.mutations()[0][3]
        self.controller = ec.Controller({"machine_ids": [MID]}, self.api, self.state,
                                        self.save, self.logs.append, lambda: self.now)
        self.tick(60)
        self.assertEqual(first, self.api.mutations()[-1][3])

    def test_existing_suspend_intent_survives_a_changed_rotation_threshold(self):
        self.api.machine = machine(run=NOW - 700)
        self.controller.config["rotate_after_seconds"] = 600
        self.api.failures["POST"] = [ec.APIError()]
        self.tick()
        first = self.api.mutations()[0][3]
        self.controller.config["rotate_after_seconds"] = 25200
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 2)
        self.assertEqual(first, self.api.mutations()[-1][3])

    def test_lost_suspend_response_confirmed_by_state_without_duplicate(self):
        self.api.machine = machine(run=NOW - 25200)
        self.api.failures["POST"] = [ec.APIError()]
        self.tick()
        self.api.machine["state"] = "suspended"
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 1)
        self.assertEqual(self.target["phase"], "suspend_hold")

    def test_first_conflict_retries_with_fresh_key(self):
        self.start_cooldown()
        self.api.failures["POST"] = [ec.APIError(409, "operation_conflict")]
        self.tick(600)
        self.tick(60)
        posts = self.api.mutations()
        self.assertNotEqual(posts[0][3], posts[1][3])

    def test_conflict_after_unknown_response_keeps_original_key(self):
        self.start_cooldown()
        self.api.failures["POST"] = [ec.APIError(), ec.APIError(409, "operation_conflict")]
        self.tick(600)
        self.tick(60)
        self.tick(60)
        self.assertEqual(len({call[3] for call in self.api.mutations()}), 1)

    def test_first_cooldown_observation_remembers_prior_run_for_reset_check(self):
        self.start_cooldown()
        self.tick(600)
        self.api.machine["state"] = "running"
        self.tick(60)
        self.assertEqual(self.target["phase"], "run_reset_unconfirmed")

    def test_new_lease_failure_after_resume_keeps_retrying(self):
        self.start_cooldown()
        self.tick(600)
        self.api.machine = machine(run=self.now + 5)
        self.api.failures["PUT"] = [ec.APIError(503, "unavailable")]
        self.tick(60)
        self.tick(60)
        self.assertEqual(self.target["last_success"], self.now)
        self.assertEqual(self.target["phase"], "running")

    def test_failed_operation_is_retried_with_new_idempotency_key(self):
        self.start_cooldown()
        self.api.operation_state = "failed"
        self.tick(600)
        self.api.operation_state = "queued"
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 2)
        self.assertNotEqual(self.api.mutations()[0][3], self.api.mutations()[1][3])

    def test_accepted_operation_is_polled_not_posted_again(self):
        self.start_cooldown()
        self.tick(600)
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 1)
        self.assertTrue(self.api.calls[-1][1].startswith("/v1/operations/"))

    def test_stale_succeeded_operation_reconciles_then_retries(self):
        self.start_cooldown()
        self.api.operation_state = "succeeded"
        self.tick(600)
        self.tick(120)
        self.tick(60)
        self.assertEqual(len(self.api.mutations()), 2)

    def test_running_operation_is_not_overlapped(self):
        self.api.machine = machine(run=NOW - 25200, operationId="op_external")
        self.tick()
        self.assertFalse(self.api.mutations())

    def test_stopped_machine_cancels_cycle_and_is_never_started(self):
        self.start_cooldown()
        self.api.machine["state"] = "stopped"
        self.tick(600)
        self.assertNotIn("cycle", self.target)
        self.assertFalse(self.api.mutations())

    def test_max_run_zero_is_not_unlimited(self):
        self.api.machine = machine("suspended", cooldown=NOW + 600, maxRunMinutes=0)
        self.tick()
        self.tick(600)
        self.assertFalse(self.api.mutations())
        self.assertEqual(self.target["phase"], "max_run_disabled")

    def test_resume_without_clock_reset_does_not_fake_success_or_suspend_again(self):
        self.api.machine = machine(run=NOW - 25200)
        self.tick()
        self.api.machine["state"] = "suspended"
        self.tick(60)
        self.tick(60)
        self.api.machine["state"] = "running"
        self.tick(60)
        self.assertEqual(self.target["phase"], "run_reset_unconfirmed")
        self.assertIn("cycle", self.target)
        self.assertEqual(len(self.api.mutations()), 2)

    def test_runtime_not_ready_does_not_complete_cycle(self):
        self.start_cooldown()
        self.tick(600)
        self.api.machine = machine(run=self.now, runtime={"ready": False})
        self.tick(60)
        self.assertIn("cycle", self.target)
        self.assertEqual(self.target["phase"], "runtime_wait")

    def test_unknown_run_start_keeps_lease_but_never_guesses_seven_hours(self):
        self.api.machine = machine(run=None)
        self.tick()
        self.assertTrue(self.api.mutations("PUT"))
        self.assertFalse(self.api.mutations())

    def test_renewal_failure_does_not_block_due_rotation(self):
        self.api.machine = machine(run=NOW - 25200)
        self.api.failures["PUT"] = [ec.APIError(503, "unavailable")]
        self.tick()
        self.assertTrue(self.api.mutations()[0][1].endswith("/suspend"))

    def test_rate_limit_honors_retry_after(self):
        self.api.failures["GET"] = [ec.APIError(429, "rate_limited", NOW + 300)]
        self.tick()
        self.tick(60)
        self.assertEqual(len(self.api.calls), 1)
        self.tick(240)
        self.assertTrue(self.api.mutations("PUT"))

    def test_auth_failure_keeps_retrying_at_slow_interval(self):
        self.api.failures["GET"] = [ec.APIError(401) for _ in range(3)]
        for advance in [0, 600, 600, 600]:
            self.tick(advance)
        self.assertEqual(len([x for x in self.api.calls if x[0] == "GET"]), 4)

    def test_save_failure_prevents_lifecycle_mutation(self):
        self.api.machine = machine(run=NOW - 25200)
        def fail():
            raise OSError("disk full")
        self.controller.save = fail
        with self.assertRaises(OSError):
            self.tick()
        self.assertFalse(self.api.mutations())

    def test_logs_have_correlation_and_never_include_api_key(self):
        self.start_cooldown()
        self.tick(600)
        self.assertTrue(self.logs[0]["cycle_id"])
        self.assertNotIn("X-API-Key", json.dumps(self.logs))

    def test_bad_config_is_rejected(self):
        for config in [{"machine_ids": []}, {"machine_ids": [MID, MID]},
                       {"machine_ids": [MID], "ttl_seconds": 60},
                       {"machine_ids": [MID], "lease_id": "../../x"}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                ec.validate_config(config)


class WireAndStorageTests(unittest.TestCase):
    def test_service_start_limit_allows_minute_timer_and_retry_headroom(self):
        unit = (Path(__file__).resolve().parents[1] / 'examples' / 'instabox-external-controller.service').read_text()
        settings = dict(line.split('=', 1) for line in unit.splitlines() if '=' in line)
        self.assertGreaterEqual(int(settings['StartLimitBurst']), 10)

    def test_missing_state_is_unhealthy(self):
        self.assertFalse(ec.health({}, NOW)["ok"])

    def test_health_suppresses_expected_cooldown_lease_age(self):
        state = {"last_tick_at": ec.iso(NOW), "machines": {MID: {
            "phase": "cooldown_wait", "last_success": NOW - 4000,
            "cycle": {"created_at": NOW - 300, "cooldown_until": NOW + 300}}}}
        self.assertTrue(ec.health(state, NOW)["ok"])
        self.assertFalse(ec.health(state, NOW + 1000)["ok"])

    def test_health_detects_repeated_failure_and_stale_lease(self):
        state = {"last_tick_at": ec.iso(NOW), "machines": {MID: {
            "phase": "running", "last_success": NOW - 1300, "consecutive_errors": 3}}}
        issues = {x["issue"] for x in ec.health(state, NOW)["issues"]}
        self.assertEqual(issues, {"repeated_api_failure", "lease_renewal_stale"})

    def test_suspend_omits_body_and_has_idempotency_header(self):
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 202
        response.read.return_value = b'{"id":"op_test"}'
        api = ec.API("test-placeholder", opener)
        api.request("POST", "/v1/machines/m_example/suspend", key="ib-cloud-example")
        request = opener.open.call_args.args[0]
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Idempotency-key"), "ib-cloud-example")

    def test_http_error_retains_retry_after_but_not_message(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("url", 409, "private", {"Retry-After": "120"},
            io.BytesIO(b'{"error":{"code":"machine_cooldown","message":"SECRET"}}'))
        with self.assertRaises(ec.APIError) as caught:
            ec.API("test-placeholder", opener, lambda: NOW).request("POST", "/v1/machines/m_example/resume")
        self.assertEqual(caught.exception.retry_at, NOW + 120)
        self.assertEqual(caught.exception.code, "machine_cooldown")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_retry_after_accepts_http_date(self):
        value = dt.datetime.fromtimestamp(NOW + 120, dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertEqual(ec.retry_deadline(value, NOW), NOW + 120)

    def test_atomic_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "state.json"
            ec.atomic_json(path, {"schema": 1, "machines": {}})
            self.assertEqual(json.loads(path.read_text()), {"schema": 1, "machines": {}})
            self.assertEqual(len(list(path.parent.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
