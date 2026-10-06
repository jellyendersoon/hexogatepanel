"""Durable activation orchestration; no panel imports or credential persistence."""

import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def status(user):
    return getattr(user.get("status"), "value", user.get("status"))


def valid_active(user, now=None):
    now = now or datetime.now(UTC)
    return (
        status(user) == "active"
        and utc(user.get("expire")) is not None
        and utc(user["expire"]) > now
        and user.get("on_hold_expire_duration") is None
        and user.get("on_hold_timeout") is None
    )


def invariants(user):
    next_plan = json.dumps(user.get("next_plan"), sort_keys=True, default=str)
    return {
        "used_traffic": int(user.get("used_traffic") or 0),
        "data_limit": user.get("data_limit"),
        "lifetime_used_traffic": int(user.get("lifetime_used_traffic") or 0),
        "next_plan_sha256": hashlib.sha256(next_plan.encode()).hexdigest(),
        "next_plan_id": user.get("_next_plan_id"),
        "admin_id": user.get("_admin_id"),
    }


def hold_signature(user):
    return {
        "duration": user.get("on_hold_expire_duration"),
        "timeout": user.get("on_hold_timeout"),
        "edit_at": user.get("edit_at"),
        "data_limit": user.get("data_limit"),
        "next_plan_id": user.get("_next_plan_id"),
        "admin_id": user.get("_admin_id"),
    }


class ActivationError(Exception):
    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.db_path = self.directory / "intents.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS intents (
                user_id INTEGER PRIMARY KEY, username TEXT NOT NULL,
                phase TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, user_id):
        with self.connect() as db:
            row = db.execute("SELECT phase,payload FROM intents WHERE user_id=?", (user_id,)).fetchone()
        return {"phase": row[0], **json.loads(row[1])} if row else None

    def save(self, user_id, username, phase, payload):
        payload = {key: value for key, value in payload.items() if key != "phase"}
        with self.connect() as db:
            db.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "username=excluded.username,phase=excluded.phase,payload=excluded.payload,updated_at=excluded.updated_at",
                (user_id, username, phase, json.dumps(payload, default=str), datetime.now(UTC).isoformat()),
            )

    def pending(self, limit=25):
        with self.connect() as db:
            return db.execute(
                "SELECT username FROM intents WHERE phase IN ('prepared','pending_sync','pending_event','native_processing') "
                "ORDER BY updated_at LIMIT ?", (limit,),
            ).fetchall()

    def phase_counts(self):
        with self.connect() as db:
            return dict(db.execute("SELECT phase,COUNT(*) FROM intents GROUP BY phase").fetchall())

    def require_review(self, username, error):
        with self.connect() as db:
            row = db.execute("SELECT user_id,payload FROM intents WHERE username=?", (username,)).fetchone()
        if row:
            self.save(row[0], username, "operator_review", {**json.loads(row[1]), "error": str(error)})

    @contextmanager
    def user_lock(self, user_id):
        lock_path = self.directory / f"user-{int(user_id)}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class Activator:
    def __init__(self, backend, journal):
        self.backend = backend
        self.journal = journal

    @staticmethod
    def response(user, state, **details):
        return {
            **{key: value for key, value in user.items() if not key.startswith("_")},
            "_reserve_activation": {"state": state, **details},
        }

    async def activate(self, username, credentials, *, due_only=False):
        # Always authorize before consulting the journal or reporting lock state.
        initial = await self.backend.authorized_snapshot(username, credentials)
        user_id = int(initial["id"])
        with self.journal.user_lock(user_id) as acquired:
            if not acquired:
                return self.response(initial, "processing")
            async with self.backend.locked_user(username, credentials) as handle:
                user = await handle.snapshot()
                intent = self.journal.get(user_id)
                event_only = False
                if status(user) == "on_hold":
                    if intent and intent["phase"] != "complete" and intent.get("hold_signature") != hold_signature(user):
                        raise ActivationError("Reserve cycle changed since the pending request; operator review required.")
                    duration = int(user.get("on_hold_expire_duration") or 0)
                    if duration <= 0 or duration > self.backend.max_duration:
                        raise ActivationError("Reserve has no valid paid duration.", 400)
                    if user.get("_refunded"):
                        raise ActivationError("Refunded reserves cannot be activated.", 409)
                    if user.get("data_limit") and int(user.get("used_traffic") or 0) >= int(user["data_limit"]):
                        raise ActivationError("Reserve has no remaining traffic; renewal review required.", 409)
                    if due_only and (
                        utc(user.get("on_hold_timeout")) is None
                        or utc(user["on_hold_timeout"]) > datetime.now(UTC)
                    ):
                        return self.response(user, "waiting")
                    # Native review selects without a lock/CAS. Do not race a row
                    # that it can already select; let its healthy first-connect
                    # path finish, and retain an intent for later sync verification.
                    if user.get("_native_eligible"):
                        self.journal.save(user_id, username, "native_processing", {
                            "duration": duration, "before": invariants(user),
                            "hold_signature": hold_signature(user),
                        })
                        return self.response(user, "processing", native_worker=True)
                    before = invariants(user)
                    intent = {"duration": duration, "before": before, "hold_signature": hold_signature(user)}
                    self.journal.save(user_id, username, "prepared", intent)
                    await handle.start()
                    user = await handle.snapshot()
                    after = invariants(user)
                    if before != after:
                        # No compensating write: repairing accounting or NextPlan
                        # would be a separate operation, never an activation retry.
                        self.journal.save(user_id, username, "invariant_failure", intent)
                        raise ActivationError("Activation invariant changed; operator review required.", 503)
                    if not valid_active(user):
                        raise ActivationError("Panel activation did not produce a valid active reserve.", 503)
                    intent["expire"] = user["expire"]
                    self.journal.save(user_id, username, "pending_sync", intent)
                elif valid_active(user):
                    if intent and intent["phase"] == "complete":
                        # A later normal paid renewal may change expiry. Completed
                        # activation work must never reject or restart that clock.
                        return self.response(user, "active", idempotent=True)
                    if intent and intent.get("expire") and utc(intent["expire"]) != utc(user["expire"]):
                        raise ActivationError("Service expiry changed since activation; operator review required.")
                    # Covers a crash after native commit but before journal update.
                    if intent and intent["phase"] in {"prepared", "native_processing"}:
                        intent["expire"] = user["expire"]
                        self.journal.save(user_id, username, "pending_sync", intent)
                    elif intent and intent["phase"] == "pending_event":
                        event_only = True
                    elif not intent:
                        # An already active service requires no clock changes.
                        return self.response(user, "active", idempotent=True)
                    elif intent["phase"] != "pending_sync":
                        raise ActivationError("Activation needs operator review.", 503)
                else:
                    raise ActivationError("Only current on-hold reserves can be started.")
            # Native helper committed before this point. Retry only sync, never
            # start_users_expire again for an already-active service.
            synced = user
            if not event_only:
                try:
                    synced = await self.backend.sync(user_id, credentials)
                    if not valid_active(synced) or utc(synced["expire"]) != utc(user["expire"]):
                        raise ActivationError("Native synchronization did not confirm the persisted activation.")
                except Exception:
                    return self.response(user, "pending_sync")
                self.journal.save(user_id, username, "pending_event", intent)
            # The completion audit uses a separate durable phase; failure never
            # restarts the clock or repeats confirmed native synchronization.
            try:
                audit = await self.backend.notify(synced, credentials)
                if isinstance(audit, dict):
                    intent["audit"] = audit
            except Exception:
                return self.response(synced, "active", event_pending=True)
            self.journal.save(user_id, username, "complete", intent)
            return self.response(synced, "active", audit=intent.get("audit"))

    async def reconcile_once(self, credentials, limit=25):
        # Scope-all validation lives in the native backend. No broad paused sweep.
        await self.backend.authorize_reconciler(credentials)
        results = []
        pending = [row[0] for row in self.journal.pending(limit)]
        due = await self.backend.due_reserves(credentials, limit)
        # Retrying an explicitly authorized manual intent is not a fresh timeout
        # activation. A failure before native commit must also retry future timers.
        candidates = [(name, False) for name in dict.fromkeys(pending)]
        candidates.extend((name, True) for name in dict.fromkeys(due) if name not in pending)
        for username, due_only in candidates:
            try:
                results.append(await self.activate(username, credentials, due_only=due_only))
            except ActivationError as exc:
                # Invalid/stale authorized intents need a decision rather than
                # occupying the first retry batch forever. Transient native
                # commit errors remain prepared and are retried on the next tick.
                self.journal.require_review(username, exc)
                results.append({"_reserve_activation": {"state": "operator_review"}})
            except Exception:
                results.append({"_reserve_activation": {"state": "operator_review"}})
        return results

    async def reconcile_loop(self, credentials_factory, interval=30):
        while True:
            try:
                # Avoid creating native login events or probing credentials when
                # there is no authorized retry work and no due allowlist.
                if self.journal.pending(1) or getattr(self.backend, "allowlist_path", None):
                    credentials = await credentials_factory()
                    await self.reconcile_once(credentials)
            except Exception:
                # Credentials never enter logs or durable intents. Retry next tick.
                pass
            await asyncio.sleep(interval)
