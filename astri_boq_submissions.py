from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any


class SubmissionConflict(RuntimeError):
    def __init__(self, submission_id: str) -> None:
        self.submission_id = submission_id
        super().__init__("Payload Add BOQ berbeda untuk WO yang masih terkunci.")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


class RedisBoqSubmissionStore:
    """Durable, atomic identity claim for non-retryable ASTRI BOQ mutations."""

    def __init__(self, redis_url: str, *, prefix: str = "eva", ttl_seconds: int = 86400) -> None:
        if not str(redis_url or "").strip():
            raise RuntimeError("Redis wajib tersedia untuk Add BOQ async.")
        import redis

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._redis.ping()
        self._prefix = f"{str(prefix or 'eva').strip(':')}:astri-boq"
        self._ttl = max(300, int(ttl_seconds))
        self._resolved_ttl = max(self._ttl, 604800)

    @staticmethod
    def identity(scope: str, scope_code: str, work_order_number: str) -> str:
        normalized_scope = str(scope or "").strip().lower()
        code = str(scope_code or "").strip().casefold()
        work_order = str(work_order_number or "").strip().casefold()
        if normalized_scope not in {"cluster", "subfeeder", "feeder"} or not code or not work_order:
            raise ValueError("Scope, kode, dan work order Add BOQ wajib lengkap.")
        raw = "\0".join((normalized_scope, code, work_order))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint(snapshot: dict[str, Any]) -> str:
        payload = json.dumps(_json_safe(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def claim(
        self,
        identity_hash: str,
        fingerprint: str,
        snapshot: dict[str, Any],
        *,
        supersede_submission_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        submission_id = uuid.uuid4().hex
        now = time.time()
        public_scope = str(snapshot.get("scope") or "").strip().lower()
        match = snapshot.get("match") if isinstance(snapshot.get("match"), dict) else {}
        scope_code = str(
            match.get({"cluster": "cluster_code", "subfeeder": "subfeeder_code", "feeder": "feeder_code"}.get(public_scope, ""))
            or match.get("scope_code")
            or ""
        ).strip()
        work_order = str(match.get("work_order_number") or "").strip()
        record_key = self._record_key(submission_id)
        pointer = f"{submission_id}|{fingerprint}"
        script = """
        local current = redis.call('GET', KEYS[1])
        if current then
          local sep = string.find(current, '|', 1, true)
          local current_id = sep and string.sub(current, 1, sep - 1) or ''
          local current_key = ARGV[1] .. current_id
          local current_fingerprint = sep and string.sub(current, sep + 1) or ''
          local record_fingerprint = redis.call('HGET', current_key, 'fingerprint') or ''
          if ARGV[12] ~= '' and current_id == ARGV[12]
             and current_fingerprint ~= '' and current_fingerprint == record_fingerprint
             and redis.call('EXISTS', current_key) == 1
             and (redis.call('HGET', current_key, 'state') == 'done'
                  or redis.call('HGET', current_key, 'state') == 'unknown'
                  or redis.call('HGET', current_key, 'state') == 'partial') then
            redis.call('HSET', current_key, 'state', 'superseded',
                       'updated_at', ARGV[8], 'finished_at', ARGV[8],
                       'message', 'Digantikan oleh attempt Add BOQ baru.')
            redis.call('EXPIRE', current_key, ARGV[13])
          else
            -- Existing or orphan pointers stay locked as unresolved tombstones
            -- unless one exact done submission is atomically superseded by an
            -- explicit new attempt.
            return {current, '0'}
          end
        end
        redis.call('HSET', KEYS[2],
          'submission_id', ARGV[2], 'identity_hash', ARGV[3], 'fingerprint', ARGV[4],
          'state', 'queued', 'scope', ARGV[5], 'scope_code', ARGV[6],
          'work_order_number', ARGV[7], 'created_at', ARGV[8], 'updated_at', ARGV[8],
          'mutation_started', '0', 'close_ok', '', 'message', 'Add BOQ masuk antrean.',
          'snapshot_json', ARGV[9])
        redis.call('PERSIST', KEYS[2])
        redis.call('SET', KEYS[1], ARGV[11])
        return {ARGV[11], '1'}
        """
        result = self._redis.eval(
            script,
            2,
            self._identity_key(identity_hash),
            record_key,
            f"{self._prefix}:submission:",
            submission_id,
            identity_hash,
            fingerprint,
            public_scope,
            scope_code,
            work_order,
            repr(now),
            json.dumps(_json_safe(snapshot), ensure_ascii=False, separators=(",", ":")),
            str(self._ttl),
            pointer,
            str(supersede_submission_id or ""),
            str(self._resolved_ttl),
        )
        current_pointer, created_raw = str(result[0]), str(result[1])
        current_id, current_fingerprint = current_pointer.split("|", 1)
        if current_fingerprint != fingerprint:
            raise SubmissionConflict(current_id)
        record = self.get(current_id)
        if not record:
            raise RuntimeError("Record submission Add BOQ gagal disimpan.")
        return record, created_raw == "1"

    def get_by_identity(self, identity_hash: str) -> dict[str, Any] | None:
        pointer = self._redis.get(self._identity_key(identity_hash))
        if not pointer or "|" not in pointer:
            return None
        submission_id, _fingerprint = pointer.split("|", 1)
        return self.get(submission_id)

    def get(self, submission_id: str) -> dict[str, Any] | None:
        raw = self._redis.hgetall(self._record_key(submission_id))
        if not raw:
            return None
        result: dict[str, Any] = dict(raw)
        for key in ("created_at", "updated_at", "started_at", "finished_at"):
            if raw.get(key):
                result[key] = float(raw[key])
        result["mutation_started"] = raw.get("mutation_started") == "1"
        result["close_ok"] = None if raw.get("close_ok", "") == "" else raw.get("close_ok") == "1"
        if raw.get("snapshot_json"):
            result["snapshot"] = json.loads(raw["snapshot_json"])
        if raw.get("mutation_plan_json"):
            result["mutation_plan"] = json.loads(raw["mutation_plan_json"])
        if raw.get("result_json"):
            result["result"] = json.loads(raw["result_json"])
        if raw.get("history_context_json"):
            result["history_context"] = json.loads(raw["history_context_json"])
        return result

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "submission_id", "state", "scope", "scope_code", "work_order_number",
            "created_at", "updated_at", "started_at", "finished_at", "mutation_started",
            "close_ok", "message", "error_code", "last_stage",
        }
        payload = {key: value for key, value in record.items() if key in allowed}
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        summary_keys = {
            "processed_count", "expected_count", "ok_count", "failed_count", "skipped_count",
            "updated_count", "inserted_count", "already_present_count", "already_present_codes",
            "active_code", "vendor_mismatch_count", "duplicate_count", "missing_count",
            "unexpected_count", "reconciled",
            "skipped_codes", "skipped_items", "items", "updated_items", "vendor_mismatches",
        }
        payload["result"] = {key: result[key] for key in summary_keys if key in result}
        return payload

    def mark_running(self, submission_id: str) -> bool:
        now = repr(time.time())
        record_key = self._record_key(submission_id)
        identity_hash = str(self._redis.hget(record_key, "identity_hash") or "")
        if not identity_hash:
            return False
        changed = self._redis.eval(
            """
            if redis.call('HGET', KEYS[1], 'state') ~= 'queued' then return 0 end
            local current = redis.call('GET', KEYS[2])
            local fingerprint = redis.call('HGET', KEYS[1], 'fingerprint') or ''
            local expected = ARGV[2] .. '|' .. fingerprint
            if current ~= expected then return 0 end
            redis.call('HSET', KEYS[1], 'state', 'running', 'started_at', ARGV[1],
                       'updated_at', ARGV[1], 'message', 'Add BOQ sedang diproses di ASTRI.')
            redis.call('PERSIST', KEYS[1])
            redis.call('PERSIST', KEYS[2])
            return 1
            """,
            2,
            record_key,
            self._identity_key(identity_hash),
            now,
            submission_id,
        )
        return bool(changed)

    def update(self, submission_id: str, **changes: Any) -> dict[str, Any] | None:
        """Attach enqueue metadata only; state transitions require CAS."""
        allowed = {"task_id"}
        unexpected = set(changes) - allowed
        if unexpected:
            raise ValueError(f"Non-CAS submission update ditolak: {sorted(unexpected)}")
        mapping: dict[str, str] = {"updated_at": repr(time.time())}
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "result":
                mapping["result_json"] = json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
            elif key == "mutation_plan":
                mapping["mutation_plan_json"] = json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
            elif key == "history_context":
                mapping["history_context_json"] = json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
            elif key in {"mutation_started", "close_ok"}:
                mapping[key] = "" if value is None else ("1" if bool(value) else "0")
            else:
                mapping[key] = "" if value is None else str(value)
        key = self._record_key(submission_id)
        identity_hash = str(self._redis.hget(key, "identity_hash") or "")
        if not identity_hash:
            return None
        flat_mapping = [part for pair in mapping.items() for part in pair]
        changed = int(self._redis.eval(
            """
            if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
            local current = redis.call('GET', KEYS[2])
            local fingerprint = redis.call('HGET', KEYS[1], 'fingerprint') or ''
            local expected = ARGV[1] .. '|' .. fingerprint
            if fingerprint == '' or current ~= expected then return -1 end
            for i = 3, #ARGV, 2 do
              redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
            end
            local state = redis.call('HGET', KEYS[1], 'state') or ''
            if state == 'queued' or state == 'running' or state == 'partial' or state == 'unknown' or state == 'reconciling' or state == 'done' or state == 'blocked-before-mutation' then
              redis.call('PERSIST', KEYS[1])
              redis.call('PERSIST', KEYS[2])
            else
              redis.call('EXPIRE', KEYS[1], ARGV[2])
              redis.call('EXPIRE', KEYS[2], ARGV[2])
            end
            return 1
            """,
            2,
            key,
            self._identity_key(identity_hash),
            submission_id,
            str(self._resolved_ttl),
            *flat_mapping,
        ))
        if changed < 0:
            raise RuntimeError("Identity pointer Add BOQ tidak cocok dengan record.")
        return self.get(submission_id) if changed else None

    def update_if_current(
        self,
        submission_id: str,
        *,
        expected_state: str,
        expected_updated_at: float,
        **changes: Any,
    ) -> dict[str, Any] | None:
        """Atomically apply a state transition only if no worker checkpoint advanced."""
        if expected_state not in {"queued", "running", "reconciling", "partial", "unknown"}:
            return self.get(submission_id)
        allowed = {
            "state", "message", "error_code", "mutation_started", "close_ok", "result",
            "mutation_plan", "finished_at", "task_id", "last_stage", "history_context",
        }
        history_event = changes.pop("history_event", None)
        history_json = ""
        history_score = "0"
        if history_event is not None:
            from astri_boq_history import validate_success_event
            history_event = validate_success_event(history_event)
            current = self.get(submission_id)
            if current is None:
                return None
            snapshot = current.get("snapshot") or {}
            if (changes.get("state") != "done" or changes.get("close_ok") is not True
                    or (changes.get("result") or {}).get("reconciled") is not True
                    or history_event["submission_id"] != submission_id
                    or history_event["scope"] != current.get("scope")
                    or history_event["scope_code"] != current.get("scope_code")
                    or history_event["work_order_number"] != current.get("work_order_number")
                    or history_event["operator_user_id"] != snapshot.get("eva_user_id")):
                raise ValueError("Identitas/bukti riwayat Add BOQ tidak cocok.")
            history_json = json.dumps(history_event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            history_score = repr(history_event["completed_at"])
        mapping: dict[str, str] = {"updated_at": repr(time.time())}
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "result":
                mapping["result_json"] = json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
            elif key == "mutation_plan":
                mapping["mutation_plan_json"] = json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
            elif key == "history_context":
                mapping["history_context_json"] = json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
            elif key in {"mutation_started", "close_ok"}:
                mapping[key] = "" if value is None else ("1" if bool(value) else "0")
            else:
                mapping[key] = "" if value is None else str(value)
        new_state = str(changes.get("state") or expected_state)
        persist = new_state in {
            "queued", "running", "partial", "unknown", "reconciling",
            "done", "blocked-before-mutation",
        }
        key = self._record_key(submission_id)
        identity_hash = str(self._redis.hget(key, "identity_hash") or "")
        if not identity_hash:
            return None
        flat_mapping = [part for pair in mapping.items() for part in pair]
        changed = int(self._redis.eval(
            """
            if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
            if redis.call('HGET', KEYS[1], 'state') ~= ARGV[1] then return 0 end
            if redis.call('HGET', KEYS[1], 'updated_at') ~= ARGV[2] then return 0 end
            local current = redis.call('GET', KEYS[2])
            local fingerprint = redis.call('HGET', KEYS[1], 'fingerprint') or ''
            local expected = ARGV[3] .. '|' .. fingerprint
            if fingerprint == '' or current ~= expected then return -1 end
            if ARGV[6] ~= '' then
              local event_type = redis.call('TYPE', KEYS[3]).ok
              local index_type = redis.call('TYPE', KEYS[4]).ok
              if (event_type ~= 'none' and event_type ~= 'string')
                 or (index_type ~= 'none' and index_type ~= 'zset') then return -2 end
              local prior = redis.call('GET', KEYS[3])
              if prior and prior ~= ARGV[6] then return -3 end
            end
            for i = 8, #ARGV, 2 do
              redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
            end
            if ARGV[6] ~= '' then
              redis.call('SET', KEYS[3], ARGV[6], 'NX')
              redis.call('ZADD', KEYS[4], 'NX', ARGV[7], ARGV[3])
              redis.call('PERSIST', KEYS[3])
              redis.call('PERSIST', KEYS[4])
            end
            if ARGV[4] == '1' then
              redis.call('PERSIST', KEYS[1])
              redis.call('PERSIST', KEYS[2])
            else
              redis.call('EXPIRE', KEYS[1], ARGV[5])
              redis.call('EXPIRE', KEYS[2], ARGV[5])
            end
            return 1
            """,
            4,
            key,
            self._identity_key(identity_hash),
            f"{self._prefix}:history:{submission_id}",
            f"{self._prefix}:history:index",
            str(expected_state),
            repr(float(expected_updated_at)),
            submission_id,
            "1" if persist else "0",
            str(self._resolved_ttl),
            history_json,
            history_score,
            *flat_mapping,
        ))
        if changed == -2:
            raise RuntimeError("Penyimpanan riwayat Add BOQ malformed; state tidak diubah.")
        if changed == -3:
            raise RuntimeError("Riwayat Add BOQ konflik; state tidak diubah.")
        if changed < 0:
            raise RuntimeError("Identity pointer Add BOQ tidak cocok dengan record.")
        return self.get(submission_id) if changed else None

    def list_success_history(self, *, page: int = 1, q: str = "", scope: str = "") -> dict[str, Any]:
        """Admin boundary authorizes the caller; this reads only sanitized journal events."""
        from astri_boq_history import validate_success_event
        if type(page) is not int or not 1 <= page <= 100000 or not isinstance(q, str) or len(q) > 256:
            raise ValueError("Filter riwayat Add BOQ tidak valid.")
        if scope not in {"", "cluster", "subfeeder", "feeder"}:
            raise ValueError("Target Type tidak valid.")
        needle = q.strip().casefold()
        ids = self._redis.zrevrange(f"{self._prefix}:history:index", 0, -1)
        matched = []
        # ponytail: admin history scan; add a DB query index if the journal exceeds routine operator scale.
        for start in range(0, len(ids), 200):
            values = self._redis.mget([f"{self._prefix}:history:{sid}" for sid in ids[start:start + 200]])
            for sid, raw in zip(ids[start:start + 200], values):
                if raw is None:
                    raise RuntimeError("Event riwayat Add BOQ tidak tersedia; index tetap dipertahankan.")
                event = validate_success_event(json.loads(raw))
                if event["submission_id"] != sid:
                    raise ValueError("Identitas event riwayat Add BOQ tidak cocok.")
                if (not scope or event["scope"] == scope) and (not needle or needle in json.dumps(event, ensure_ascii=False).casefold()):
                    matched.append(event)
        size = 20
        return {"history": matched[(page - 1) * size:page * size], "total": len(matched),
                "page": page, "page_size": size}

    def acquire_reconcile_lease(self, submission_id: str, *, seconds: int = 15) -> bool:
        return bool(self._redis.set(
            f"{self._prefix}:reconcile:{submission_id}",
            "1",
            nx=True,
            ex=max(5, int(seconds)),
        ))

    def release_identity(self, identity_hash: str, submission_id: str) -> bool:
        """Release only a proven pre-mutation block; never release ambiguous work."""
        released = self._redis.eval(
            """
            local current = redis.call('GET', KEYS[1])
            local fingerprint = redis.call('HGET', KEYS[2], 'fingerprint') or ''
            if fingerprint == '' or current ~= ARGV[1] .. '|' .. fingerprint then
              return 0
            end
            if redis.call('HGET', KEYS[2], 'state') ~= 'blocked-before-mutation'
               or redis.call('HGET', KEYS[2], 'mutation_started') ~= '0' then
              return 0
            end
            redis.call('DEL', KEYS[1])
            redis.call('EXPIRE', KEYS[2], ARGV[2])
            return 1
            """,
            2,
            self._identity_key(identity_hash),
            self._record_key(submission_id),
            submission_id,
            str(self._resolved_ttl),
        )
        return bool(released)

    def delete_test_prefix(self) -> None:
        keys = list(self._redis.scan_iter(match=f"{self._prefix}:*", count=200))
        if keys:
            self._redis.delete(*keys)

    def _identity_key(self, identity_hash: str) -> str:
        return f"{self._prefix}:identity:{identity_hash}"

    def _record_key(self, submission_id: str) -> str:
        return f"{self._prefix}:submission:{submission_id}"
