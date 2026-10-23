#!/usr/bin/env python3
"""Opt-in external lifecycle/lease controller; Python standard library only.

Run once per minute on an independent Linux host. No guest exec or public-port
probe is used: those can accidentally wake an idle machine during diagnosis.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import http.client
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid

API_ROOT = "https://api.instabox.sh"
ID = re.compile(r"m_[A-Za-z0-9_-]{1,128}\Z")
OP_ID = re.compile(r"op_[A-Za-z0-9_-]{1,128}\Z")


def timestamp(value):
    if not isinstance(value, str):
        return 0
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (ValueError, OverflowError):
        return 0


def iso(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def retry_deadline(value, now):
    try:
        seconds = float(value)
        return now + max(0, seconds) if math.isfinite(seconds) else 0
    except (TypeError, ValueError):
        try:
            return email.utils.parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0


class APIError(Exception):
    def __init__(self, status=0, code="", retry_at=0):
        self.status = status
        # Never log raw responses/messages or arbitrary server strings.
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,60}", code) else "unknown"
        self.retry_at = retry_at
        super().__init__(f"HTTP {status} {self.code}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, key, opener=None, clock=time.time):
        if not key or not all(33 <= ord(c) <= 126 for c in key):
            raise ValueError("Invalid API key file")
        self.key, self.clock = key, clock
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, key=None):
        headers = {"X-API-Key": self.key, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(API_ROOT + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                expected = 202 if method == "POST" else 200
                if response.status != expected:
                    raise APIError(response.status, "unexpected_status")
                raw = response.read(2_097_153)
                if len(raw) > 2_097_152:
                    raise ValueError()
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read(65536))
                code = payload.get("error", {}).get("code", "unknown")
            except (ValueError, AttributeError, OSError):
                code = "unknown"
            raise APIError(error.code, code, retry_deadline(error.headers.get("Retry-After"), self.clock())) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                http.client.HTTPException, RecursionError):
            raise APIError(0, "transport_or_response_error") from None


def validate_config(config):
    ids = config.get("machine_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 20:
        raise ValueError("machine_ids must contain 1-20 unique machine IDs")
    if any(not isinstance(mid, str) or not ID.fullmatch(mid) for mid in ids):
        raise ValueError("Invalid machine ID")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate machine ID")
    result = dict(config)
    if type(result.setdefault("maintenance", False)) is not bool:
        raise ValueError("maintenance must be a boolean")
    for name, default, low, high in [
        ("ttl_seconds", 3600, 30, 3600),
        ("renew_interval_seconds", 600, 30, 1200),
        ("rotate_after_seconds", 25200, 600, 86400),
        ("suspend_hold_seconds", 30, 30, 300),
        ("retry_seconds", 60, 30, 300),
    ]:
        value = result.setdefault(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid {name}")
    if result["ttl_seconds"] < result["renew_interval_seconds"] * 3:
        raise ValueError("TTL must cover at least three normal renewal intervals")
    lease = result.setdefault("lease_id", "external-controller")
    if not isinstance(lease, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", lease):
        raise ValueError("Invalid lease_id")
    return result


class Controller:
    def __init__(self, config, api, state, save, event, clock=time.time):
        self.config = validate_config(config)
        self.api, self.state, self.save, self.event, self.clock = api, state, save, event, clock
        self.state.setdefault("schema", 1)
        self.state.setdefault("machines", {})

    def log(self, mid, s, event, **fields):
        self.event({"at": iso(self.clock()), "machine_id": mid,
                    "cycle_id": s.get("cycle", {}).get("id"), "event": event, **fields})

    def phase(self, mid, s, name):
        if s.get("phase") != name:
            self.log(mid, s, "phase_changed", previous=s.get("phase"), phase=name)
            s["phase_since"] = self.clock()
        s["phase"] = name

    def tick(self):
        failed = False
        for mid in self.config["machine_ids"]:
            s = self.state["machines"].setdefault(mid, {})
            now = self.clock()
            if now < s.get("retry_at", 0):
                continue
            try:
                self.check(mid, s, now)
                s.pop("retry_at", None)
                s.pop("error", None)
                s["consecutive_errors"] = 0
            except APIError as error:
                failed = True
                s["consecutive_errors"] = s.get("consecutive_errors", 0) + 1
                delay = 600 if error.status in {400, 401, 402, 403, 404} else self.config["retry_seconds"]
                s["retry_at"] = max(now + delay, error.retry_at)
                s["error"] = {"at": iso(now), "status": error.status, "code": error.code}
                self.log(mid, s, "retry_scheduled", status=error.status, code=error.code,
                         attempt=s["consecutive_errors"], retry_at=iso(s["retry_at"]))
            # A failed durable save must stop the process, not allow a mutation.
            self.save()
        self.state["last_tick_at"] = iso(self.clock())
        self.save()
        return 1 if failed else 0

    def new_cycle(self, mid, s, reason, now, cooldown=0):
        s["cycle"] = {"id": uuid.uuid4().hex, "reason": reason,
                      "created_at": now, "prior_run": s.get("run_started_at", 0) or timestamp(s.get("observed_run_started_at")),
                      "stage": "suspend" if reason == "seven_hours" else "hold",
                      "cooldown_until": cooldown}
        self.save()
        self.log(mid, s, "cycle_started", reason=reason,
                 prior_run=s.get("run_started_at"), cooldown_until=cooldown or None)
        return s["cycle"]

    def renew(self, mid, s, now, force=False):
        if not force and now - s.get("last_success", 0) < self.config["renew_interval_seconds"]:
            if timestamp(s.get("expires_at")) > now + self.config["retry_seconds"] * 2:
                return
        lease = self.api.request("PUT", f"/v1/machines/{mid}/activity-leases/{self.config['lease_id']}",
                                 {"ttlSeconds": self.config["ttl_seconds"]})
        if timestamp(lease.get("expiresAt")) <= now:
            raise APIError(0, "invalid_lease_expiry")
        s.update(last_success=now, expires_at=lease["expiresAt"],
                 lease_run=timestamp(s.get("observed_run_started_at")) or s.get("run_started_at"))
        self.save()
        self.log(mid, s, "lease_renewed", expires_at=lease["expiresAt"])

    def operation(self, mid, s, cycle, action, now):
        pending = cycle.get("pending")
        if not pending:
            pending = cycle["pending"] = {"action": action, "key": "ib-cloud-" + uuid.uuid4().hex,
                                          "created_at": now}
            # Persist intent AND idempotency key before issuing a mutation.
            self.save()
        if pending["action"] != action:
            raise APIError(409, "pending_action_conflict")
        if pending.get("operation_id"):
            result = self.api.request("GET", "/v1/operations/" + pending["operation_id"])
        else:
            if now - pending["created_at"] >= 7 * 86400:
                raise APIError(409, "idempotency_window_expired")
            try:
                # Both suspend and resume accept an omitted body. Suspend MUST
                # NOT send {}. Both require the persisted Idempotency-Key.
                result = self.api.request("POST", f"/v1/machines/{mid}/{action}", key=pending["key"])
            except APIError as error:
                if error.status == 0 or error.status >= 500 or error.status == 408:
                    pending["uncertain"] = True
                if (400 <= error.status < 500 and error.status != 408
                        and not pending.get("uncertain") and error.code != "idempotency_conflict"):
                    cycle.pop("pending", None)  # definite rejection; next attempt gets a fresh key
                if error.code == "machine_cooldown":
                    cycle.pop("pending", None)
                    cycle.update(reason="cooldown", stage="hold",
                                 cooldown_until=max(cycle.get("cooldown_until", 0),
                                                    error.retry_at))
                    cycle["cooldown_probe_at"] = error.retry_at or now + self.config["retry_seconds"]
                    self.log(mid, s, "cooldown_reported", until=iso(cycle["cooldown_until"]))
                raise
        if (not OP_ID.fullmatch(str(result.get("id", ""))) or result.get("machineId") != mid
                or result.get("type") != action
                or result.get("state") not in {"queued", "running", "succeeded", "failed"}):
            raise APIError(0, "invalid_operation_response")
        if pending.get("operation_id") and pending["operation_id"] != result["id"]:
            raise APIError(0, "operation_id_mismatch")
        pending["operation_id"] = result["id"]
        self.save()
        if result["state"] == "failed":
            cycle.pop("pending", None)
            raise APIError(503, "operation_failed")
        if result["state"] == "succeeded":
            pending.setdefault("succeeded_at", now)
            if now - pending["succeeded_at"] >= 120:
                cycle.pop("pending", None)
                raise APIError(503, "operation_state_mismatch")
        self.log(mid, s, "operation_observed", action=action, operation_id=result["id"], state=result["state"])
        # Even succeeded is not enough: next tick must confirm machine state.
        if now - pending["created_at"] >= 600:
            self.log(mid, s, "operation_stalled", action=action, age_seconds=now - pending["created_at"])
            s["attention"] = "operation_stalled"

    def check(self, mid, s, now):
        machine = self.api.request("GET", f"/v1/machines/{mid}")
        if machine.get("id") != mid or not isinstance(machine.get("state"), str):
            raise APIError(0, "invalid_machine_response")
        state = machine["state"]
        run = timestamp(machine.get("runStartedAt"))
        cooldown = timestamp(machine.get("cooldownUntil"))
        s.update(last_checked_at=iso(now), machine_state=state,
                 observed_run_started_at=machine.get("runStartedAt"),
                 observed_cooldown_until=machine.get("cooldownUntil"))
        if self.config["maintenance"]:
            self.phase(mid, s, "maintenance")
            s.pop("attention", None)
            return
        cycle = s.get("cycle")
        if cycle and cycle.get("cooldown_probe_at") and cooldown:
            cycle["cooldown_until"] = cooldown
        # A past deadline retained on a later run is history, not a fresh pause.
        reference_run = run or s.get("run_started_at", 0)
        fresh_cooldown = cooldown > s.get("handled_cooldown_until", 0) and (
            cooldown > now or (reference_run > 0 and cooldown > reference_run))
        if fresh_cooldown and (state != "running" or cooldown > now):
            if not cycle:
                cycle = self.new_cycle(mid, s, "cooldown", now, cooldown)
            elif cooldown > cycle.get("cooldown_until", 0):
                # Do not replace pending operations or reset retries every poll.
                cycle.update(reason="cooldown", cooldown_until=cooldown)
                self.log(mid, s, "cooldown_updated", until=iso(cooldown))

        if machine.get("maxRunMinutes") == 0:
            self.phase(mid, s, "max_run_disabled")
            s["attention"] = "max_run_disabled"
            return
        if cycle and now < cycle.get("cooldown_until", 0):
            self.phase(mid, s, "cooldown_wait")
            return
        if machine.get("operationId") or state in {"suspending", "resuming", "starting"}:
            self.phase(mid, s, "operation_wait")
            if cycle and cycle.get("pending", {}).get("operation_id"):
                self.operation(mid, s, cycle, cycle["pending"]["action"], now)
            return
        if state in {"stopped", "stopping", "deleted", "deleting", "archived"}:
            self.phase(mid, s, "manual_or_terminal_state")
            s["attention"] = "manual_or_terminal_state"
            # Do not later resurrect a stopped machine based on an old intent.
            if cycle:
                self.log(mid, s, "cycle_cancelled", state=state)
                s.pop("cycle", None)
            return
        if state == "running":
            runtime = machine.get("runtime")
            if not isinstance(runtime, dict):
                raise APIError(0, "invalid_machine_response")
            if not runtime.get("ready"):
                self.phase(mid, s, "runtime_wait")
                # Preserve activity while readiness converges, except while
                # deliberately suspending or waiting for a lifecycle mutation.
                if not cycle or cycle.get("stage") != "suspend":
                    self.renew(mid, s, now, force=s.get("lease_run") != run)
                return
            if not run or run > now + 60:
                self.renew(mid, s, now)
                s["attention"] = "run_start_unconfirmed"
                self.phase(mid, s, "run_start_unconfirmed")
                return
            if cycle and (cycle["stage"] != "suspend" or run > cycle.get("prior_run", run)):
                if cycle.get("prior_run") and run <= cycle["prior_run"]:
                    self.renew(mid, s, now, force=s.get("lease_run") != run)
                    self.phase(mid, s, "run_reset_unconfirmed")
                    s["attention"] = "run_reset_unconfirmed"
                    return
                self.log(mid, s, "cycle_completed", run_started_at=iso(run),
                         reason=cycle["reason"], duration_seconds=now - cycle["created_at"])
                s["handled_cooldown_until"] = max(s.get("handled_cooldown_until", 0),
                                                   cycle.get("cooldown_until", 0))
                s.pop("cycle", None)
                cycle = None
                # A lifecycle operation invalidates all previous leases.
                s["last_success"] = 0
            s["run_started_at"] = run
            s.pop("attention", None)
            threshold = self.config["rotate_after_seconds"]
            cap = machine.get("maxRunMinutes", -1)
            if isinstance(cap, int) and cap > 0:
                threshold = min(threshold, max(60, cap * 60 - 600))
            if now - run >= threshold or (cycle and cycle["stage"] == "suspend"):
                cycle = cycle or self.new_cycle(mid, s, "seven_hours", now)
                self.phase(mid, s, "suspend_requested")
                try:
                    self.renew(mid, s, now, force=s.get("lease_run") != run)
                except APIError as error:
                    # Lease endpoint failure must not prevent a due rotation.
                    self.log(mid, s, "lease_failed_before_rotation", status=error.status, code=error.code)
                self.operation(mid, s, cycle, "suspend", now)
            else:
                self.renew(mid, s, now, force=s.get("lease_run") != run)
                self.phase(mid, s, "running")
            return
        if state == "suspended":
            if not cycle:
                cycle = self.new_cycle(mid, s, "ensure_online", now)
            if cycle["stage"] == "suspend":
                cycle.update(stage="hold", suspended_at=now)
                cycle.pop("pending", None)
                self.save()
                self.log(mid, s, "suspend_confirmed")
            if cycle["reason"] == "seven_hours" and now < cycle.get("suspended_at", now) + self.config["suspend_hold_seconds"]:
                self.phase(mid, s, "suspend_hold")
                return
            cycle["stage"] = "resume"
            self.phase(mid, s, "resume_requested")
            self.operation(mid, s, cycle, "resume", now)
            return
        self.phase(mid, s, "unexpected_state")
        s["attention"] = "unexpected_state"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".controller-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def logger(log_file):
    path = Path(log_file)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = logging.getLogger("instabox.external")
    result.setLevel(logging.INFO)
    handler = TimedRotatingFileHandler(path, when="midnight", backupCount=30, encoding="utf-8", utc=True)
    handler.setFormatter(logging.Formatter("%(message)s"))
    result.addHandler(handler)
    result.addHandler(logging.StreamHandler())
    return lambda event: result.info(json.dumps(event, ensure_ascii=False, allow_nan=False))


def health(state, now):
    """Local-only watchdog result; suitable for an existing alert transport."""
    issues = []
    if now - timestamp(state.get("last_tick_at")) > 180:
        issues.append({"issue": "controller_stale"})
    for mid, s in state.get("machines", {}).items():
        names = set()
        if s.get("attention"):
            names.add(s["attention"])
        if s.get("phase") == "unmanaged_pause":
            names.add("unmanaged_pause")
        if s.get("consecutive_errors", 0) >= 3:
            names.add("repeated_api_failure")
        if s.get("phase") == "maintenance":
            issues.extend({"machine_id": mid, "issue": name} for name in sorted(names))
            continue
        cycle = s.get("cycle", {})
        expected_cooldown = cycle and now < cycle.get("cooldown_until", 0)
        if not expected_cooldown:
            if s.get("phase") == "running" and now - s.get("last_success", 0) > 1200:
                names.add("lease_renewal_stale")
            if cycle:
                deadline = max(cycle.get("created_at", now) + 600,
                               cycle.get("cooldown_until", 0) + 300)
                if now > deadline:
                    names.add("cycle_stalled")
            if s.get("phase") in {"runtime_wait", "operation_wait"} and now - s.get("phase_since", now) > 300:
                names.add("machine_transition_stalled")
        issues.extend({"machine_id": mid, "issue": name} for name in sorted(names))
    return {"ok": not issues, "checked_at": iso(now), "issues": issues}


def run_ticks(controller, budget=180, monotonic=time.monotonic, sleep=time.sleep):
    """Brief fast polling only during our active lifecycle; timer handles idle time."""
    end = monotonic() + budget
    while True:
        result = controller.tick()
        now = controller.clock()
        active = any(s.get("cycle") and
                     s.get("phase") not in {"maintenance", "max_run_disabled", "run_reset_unconfirmed"} and
                     now >= s.get("retry_at", 0) and
                     now >= s["cycle"].get("cooldown_until", 0)
                     for mid, s in controller.state["machines"].items()
                     if mid in controller.config["machine_ids"])
        if result or not active or monotonic() + 10 >= end:
            return result
        sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--status", action="store_true", help="Read local state only; no network calls")
    parser.add_argument("--health", action="store_true", help="Local watchdog: JSON report, nonzero when unhealthy")
    args = parser.parse_args()
    os.umask(0o077)
    state_path = Path(args.state)
    if args.status or args.health:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if args.health:
                report = health(state, time.time())
                print(json.dumps(report))
                return 0 if report["ok"] else 1
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, TypeError, AttributeError):
            print(json.dumps({"ok": False, "issues": [{"issue": "local_state_unreadable"}]}))
            return 1
    import fcntl  # Linux runtime; unit tests can import the state machine elsewhere.
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(str(state_path) + ".lock", "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        emit = logger(args.log_file)
        try:
            config = json.loads(Path(args.config).read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if not isinstance(state, dict) or state.get("schema", 1) != 1 or not isinstance(state.get("machines", {}), dict):
                raise ValueError("Invalid state file; preserve it for inspection")
            key = Path(args.key_file).read_text(encoding="utf-8").strip()
            return run_ticks(Controller(config, API(key), state,
                                        lambda: atomic_json(state_path, state), emit))
        except (OSError, ValueError, TypeError, KeyError):
            emit({"at": iso(time.time()), "event": "controller_local_error",
                  "detail": "Check local configuration, state and file permissions; raw values omitted"})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
