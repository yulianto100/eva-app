from __future__ import annotations
import hashlib
import json
import math
import os
import re
import logging
import threading
import time
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger("eva.ai_safe")


BASE_DIR = Path(__file__).resolve().parent
_MEMORY_PATH = BASE_DIR / "data" / "eva_ai_memory.json"
_SKILLS_PATH = BASE_DIR / "data" / "eva_ai_skills.json"
_REFERENCE_PATH = BASE_DIR / "data" / "eva_kmz_reference.json"
_AI_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_AI_CACHE_LOCK = threading.Lock()
_AI_CIRCUIT_OPEN_UNTIL = 0.0
_AI_FALLBACK_CIRCUIT_OPEN_UNTIL = 0.0


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        LOGGER.warning("AI json read failed %s: %s", path, exc)
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _load_memory() -> dict[str, Any]:
    return _read_json(_MEMORY_PATH, {"version": 1, "decisions": [], "patterns": [], "updated_at": None})


def _load_skills() -> dict[str, Any]:
    return _read_json(_SKILLS_PATH, {"version": 1, "skills": []})


def _load_reference() -> dict[str, Any]:
    return _read_json(_REFERENCE_PATH, {"version": 1, "samples": [], "universal_rules": {}})


def _ai_context() -> dict[str, Any]:
    memory = _load_memory()
    skills = _load_skills()
    reference = _load_reference()
    return {
        "skills": skills.get("skills", [])[:20],
        "memory_decisions": memory.get("decisions", [])[-30:],
        "memory_patterns": memory.get("patterns", [])[-30:],
        "reference_rules": reference.get("universal_rules", {}),
        "reference_samples": reference.get("samples", [])[:5],
    }


def remember_decision(title: str, content: str, tags: list[str] | None = None) -> None:
    memory = _load_memory()
    item = {
        "title": str(title).strip(),
        "content": str(content).strip(),
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if item["title"] and item["content"]:
        memory.setdefault("decisions", []).append(item)
        memory["decisions"] = memory["decisions"][-200:]
        memory["updated_at"] = item["created_at"]
        _write_json(_MEMORY_PATH, memory)


def remember_pattern(name: str, rule: str, example: dict[str, Any] | None = None, tags: list[str] | None = None) -> None:
    memory = _load_memory()
    item = {
        "name": str(name).strip(),
        "rule": str(rule).strip(),
        "example": example or {},
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if item["name"] and item["rule"]:
        memory.setdefault("patterns", []).append(item)
        memory["patterns"] = memory["patterns"][-200:]
        memory["updated_at"] = item["created_at"]
        _write_json(_MEMORY_PATH, memory)


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _provider_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _provider_config() -> tuple[str, str, str]:
    local_env = _read_env_file(Path(__file__).resolve().parent / ".env")
    bot_env = _read_env_file(Path(__file__).resolve().parent / "eva-wa-bot" / ".env")
    api_key = (
        os.getenv("EVA_AI_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
        or local_env.get("OPENAI_API_KEY", "").strip()
        or bot_env.get("OPENAI_API_KEY", "").strip()
    )
    base_url = (
        os.getenv("EVA_AI_BASE_URL", "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or local_env.get("OPENAI_BASE_URL", "").strip()
        or bot_env.get("OPENAI_BASE_URL", "").strip()
    )
    model = (
        os.getenv("EVA_AI_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or local_env.get("OPENAI_MODEL", "").strip()
        or bot_env.get("OPENAI_MODEL", "").strip()
        or "9r"
    )
    return api_key, base_url, model


# ── public gate ──────────────────────────────────────────────

def ai_assist_enabled() -> bool:
    return _env_bool("EVA_AI_ASSIST", False)


def ai_visual_parcel_enabled() -> bool:
    return ai_assist_enabled() and _env_bool("EVA_AI_VISUAL_PARCEL", False)


def ai_confidence_min() -> float:
    return _env_float("EVA_AI_CONFIDENCE_MIN", 0.80)


def ai_timeout() -> int:
    return _env_int("EVA_AI_TIMEOUT", 60)


def ai_cache_ttl_seconds() -> int:
    return max(0, _env_int("EVA_AI_CACHE_TTL_SECONDS", 3600))


def ai_cache_max_entries() -> int:
    return max(1, _env_int("EVA_AI_CACHE_MAX_ENTRIES", 256))


def ai_circuit_breaker_seconds() -> int:
    return max(0, _env_int("EVA_AI_CIRCUIT_BREAKER_SECONDS", 300))


def ai_reasoning_effort() -> str:
    value = os.getenv("EVA_AI_REASONING_EFFORT", "low").strip().lower()
    return value if value in {"none", "minimal", "low", "medium", "high", "xhigh", "max"} else "low"


def _ai_cache_key(url: str, model: str, messages: list[dict[str, Any]], max_tokens: int, reasoning_effort: str) -> str:
    raw = json.dumps(
        {
            "url": url,
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _ai_cache_get(key: str) -> dict[str, Any] | None:
    ttl = ai_cache_ttl_seconds()
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _AI_CACHE_LOCK:
        cached = _AI_CACHE.get(key)
        if cached is None:
            return None
        saved_at, value = cached
        if now - saved_at > ttl:
            _AI_CACHE.pop(key, None)
            return None
        return dict(value)


def _ai_cache_put(key: str, value: dict[str, Any]) -> None:
    if ai_cache_ttl_seconds() <= 0:
        return
    now = time.monotonic()
    with _AI_CACHE_LOCK:
        _AI_CACHE[key] = (now, dict(value))
        excess = len(_AI_CACHE) - ai_cache_max_entries()
        if excess > 0:
            oldest = sorted(_AI_CACHE, key=lambda item: _AI_CACHE[item][0])[:excess]
            for item in oldest:
                _AI_CACHE.pop(item, None)


# ── generic LLM call ────────────────────────────────────────

def _post_json(messages: list[dict[str, Any]], *, max_tokens: int = 220, timeout: int | None = None,
               provider: tuple[str, str, str] | None = None, retry_response_format: bool = True,
               allow_redirects: bool = True) -> dict[str, Any] | None:
    global _AI_CIRCUIT_OPEN_UNTIL, _AI_FALLBACK_CIRCUIT_OPEN_UNTIL
    api_key, base_url, model = provider or _provider_config()
    use_fallback_circuit = provider is not None
    if not api_key or not base_url:
        return None
    url = _provider_url(base_url)
    reasoning_effort = ai_reasoning_effort()
    cache_key = _ai_cache_key(url, model, messages, max_tokens, reasoning_effort)
    cached = _ai_cache_get(cache_key)
    if cached is not None:
        LOGGER.info("AI hint cache hit")
        return cached
    now = time.monotonic()
    with _AI_CACHE_LOCK:
        circuit_open_until = _AI_FALLBACK_CIRCUIT_OPEN_UNTIL if use_fallback_circuit else _AI_CIRCUIT_OPEN_UNTIL
    if now < circuit_open_until:
        LOGGER.debug("AI call skipped: timeout circuit open for %.1fs", circuit_open_until - now)
        return None
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = timeout or ai_timeout()
    try:
        t0 = time.monotonic()
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout, allow_redirects=allow_redirects)
        if retry_response_format and resp.status_code in {400, 415, 422}:
            payload.pop("response_format", None)
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout, allow_redirects=allow_redirects)
        if not allow_redirects and 300 <= resp.status_code < 400:
            return None
        resp.raise_for_status()
        data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        elapsed = time.monotonic() - t0
        if not content:
            LOGGER.warning("AI empty response in %.2fs", elapsed)
            return None
        match = re.search(r"\{.*\}", content, re.S)
        text = match.group(0) if match else content
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return None
        with _AI_CACHE_LOCK:
            if use_fallback_circuit:
                _AI_FALLBACK_CIRCUIT_OPEN_UNTIL = 0.0
            else:
                _AI_CIRCUIT_OPEN_UNTIL = 0.0
        _ai_cache_put(cache_key, parsed)
        LOGGER.info("AI respond in %.2fs", elapsed)
        return parsed
    except requests.Timeout as exc:
        breaker_seconds = ai_circuit_breaker_seconds()
        with _AI_CACHE_LOCK:
            if use_fallback_circuit:
                _AI_FALLBACK_CIRCUIT_OPEN_UNTIL = time.monotonic() + breaker_seconds
            else:
                _AI_CIRCUIT_OPEN_UNTIL = time.monotonic() + breaker_seconds
        LOGGER.warning("AI call timed out after %ss; circuit open %ss: %s", timeout, breaker_seconds, exc)
        return None
    except Exception as exc:
        LOGGER.warning("AI call failed: %s", exc)
        return None


# ── KMZ fallback ────────────────────────────────────────────

def suggest_kmz_target(
    *,
    placemark_name: str,
    folder_stack: list[str],
    point: tuple[float, float] | None,
    nearest_fat_codes: list[str],
    nearest_cables: list[dict[str, Any]],
    candidate_line_prefix: str,
    source_folder: str | None,
) -> dict[str, Any] | None:
    if not ai_assist_enabled():
        return None
    system = (
        "You are a telecom rollout KML/KMZ folder classifier. "
        "Given a placemark and context, decide which folder_path it should go under. "
        "Return ONLY JSON: {\"folder_path\":[str,...], \"confidence\":float, \"reason\":str}. "
        "folder_path is an array of 1-3 folder names. "
        "Rules: first element must start with LINE or match candidate_line_prefix. "
        "Use HP COVER, BOUNDARY FAT, FAT, NEW POLE, SLACK HANGER, DISTRIBUTION CABLE, etc. "
        "If HP COVER, add a third element for the FAT code (e.g. A01, B09). "
        "Never return BOUNDARY CLUSTER. Never invent unsupported folder families. "
        "Use provided EVA_AI_SKILLS, EVA_AI_MEMORY, and EVA_KMZ_REFERENCE as durable local knowledge."
    )
    context = _ai_context()
    user_payload = {
        "EVA_AI_SKILLS": context["skills"],
        "EVA_AI_MEMORY": {"decisions": context["memory_decisions"], "patterns": context["memory_patterns"]},
        "EVA_KMZ_REFERENCE": {"rules": context["reference_rules"], "samples": context["reference_samples"]},
        "placemark_name": placemark_name,
        "source_folder": source_folder,
        "folder_stack": folder_stack[-10:],
        "point": list(point) if point else None,
        "candidate_line_prefix": candidate_line_prefix,
        "nearest_fat_codes": nearest_fat_codes[:6],
        "nearest_cables": nearest_cables[:5],
        "examples": [
            [candidate_line_prefix, "HP COVER", nearest_fat_codes[0] if nearest_fat_codes else "UNCATEGORIZED"],
            [candidate_line_prefix, "BOUNDARY FAT"],
            [candidate_line_prefix, "DISTRIBUTION CABLE"],
        ],
    }
    result = _post_json([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ])
    if not result:
        return None
    confidence = result.get("confidence")
    try:
        confidence = float(confidence)
    except Exception:
        return None
    folder_path = result.get("folder_path")
    if confidence < ai_confidence_min() or not isinstance(folder_path, list) or not folder_path:
        return None
    clean = [str(item).strip() for item in folder_path[:3] if str(item).strip()]
    if not clean:
        return None
    return {"folder_path": clean, "confidence": confidence, "reason": str(result.get("reason") or "").strip()}


def suggest_dxf_parcel_orientation(
    *,
    fat_angle: float,
    road_angle: float | None,
    point_count: int,
    region_area: float,
    region_width: float,
    region_height: float,
) -> dict[str, Any] | None:
    """Ask EVA AI for one *group-level* HP COVER parcel axis.

    AI only selects an angle. The DXF converter still deterministically clips
    parcels to Voronoi cells, FAT/block boundary and road clearance, so it
    cannot create overlapping or out-of-area boxes.
    """
    if not ai_assist_enabled() or point_count < 2:
        return None
    system = (
        "You select a single parcel-grid orientation for telecom DXF HP COVER. "
        "Return ONLY JSON: {\"angle_degrees\": number, \"confidence\": number, \"reason\": string}. "
        "Use FAT area axis as default. If a nearby road angle is supplied and it clearly gives a cleaner "
        "road-front parcel row, use that road angle. Keep the result within -90..90 degrees. "
        "Do not propose geometry, dimensions, or coordinates."
    )
    payload = {
        "fat_angle_degrees": round(float(fat_angle), 2),
        "road_angle_degrees": round(float(road_angle), 2) if road_angle is not None else None,
        "hp_cover_count": int(point_count),
        "region_area_m2": round(float(region_area), 2),
        "region_width_m": round(float(region_width), 2),
        "region_height_m": round(float(region_height), 2),
        "constraint": "full area, rectangular parcels, no overlap, never cross road/boundary",
    }
    result = _post_json(
        [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload)}],
        max_tokens=120,
        timeout=min(ai_timeout(), 45),
    )
    if not result:
        return None
    try:
        confidence = float(result.get("confidence", 0))
        angle = float(result.get("angle_degrees"))
    except (TypeError, ValueError):
        return None
    if confidence < ai_confidence_min() or not -90.0 <= angle <= 90.0:
        return None
    return {"angle_degrees": angle, "confidence": confidence, "reason": str(result.get("reason") or "").strip()}


def suggest_dxf_visual_parcel_layout(
    *,
    preview_png: bytes,
    fat_angle: float,
    road_angle: float | None,
    point_count: int,
    axis_only: bool = False,
    timeout: float | None = None,
    deadline_s: float | None = None,
) -> dict[str, Any] | None:
    """Vision-only parcel hint. PNG shows FAT=cyan, ROAD=red, HP=yellow.
    It returns hints only; converter owns all coordinates and topology."""
    if not ai_visual_parcel_enabled() or not preview_png or point_count < 2:
        return None
    import base64
    image = 'data:image/png;base64,' + base64.b64encode(preview_png).decode('ascii')
    system = (
        'You visually review EVA telecom HP COVER parcel layout. Return ONLY JSON: '
        '{"angle_degrees":number,"width_scale":number,"depth_scale":number,'
        '"along_shift_m":number,"normal_shift_m":number,"confidence":number,"reason":string}. '
        'Image legend: cyan=local boundary, red=ROAD exclusion, yellow=fixed HP source points, green=legal deterministic cells. '
        'Choose ONE clean shared grid angle. Optional shifts translate the entire local strip, never individual HP. '
        'Do not output coordinates or polygons. Keep angle within 15 degrees of supplied ROAD/FAT reference; '
        'width/depth scales must be 1.0..1.3; each source must stay within 12m of its final cell; '
        'cells must stay outside red and never overlap.'
    )
    if axis_only:
        system = (
            'Choose one shared axis for this local HP parcel block. Return ONLY JSON: '
            '{"angle_degrees":number,"confidence":number,"reason":string}. '
            'Image: cyan=local extent, red=ROAD exclusion, yellow=immutable source points, '
            'green=initial candidate cells (may overlap each other), purple=ROAD-blocked sources. '
            'Recommend a clean common row axis within 15 degrees of the supplied ROAD reference. '
            'Confidence refers only to the axis recommendation, not geometry feasibility or whether '
            'all parcels already fit. Source relocation, parcel sizes and all ROAD/overlap checks '
            'are handled by the deterministic solver. Do not return shifts, coordinates or polygons. '
            'If the picture does not support a reliable axis, give a genuinely low confidence. '
            'Keep reason under 20 words.'
        )
    content = [
        {'type': 'text', 'text': json.dumps({
            'fat_angle_degrees': round(float(fat_angle), 2),
            'road_angle_degrees': round(float(road_angle), 2) if road_angle is not None else None,
            'hp_cover_count': int(point_count),
            'constraints': 'allowed parcel ladder only: 6x12m, 5x10m, 4x10m, 4x8m; ROAD positive-area overlap forbidden',
        })},
        {'type': 'image_url', 'image_url': {'url': image}},
    ]
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]

    def accepted(result):
        if axis_only:
            if not isinstance(result, dict):
                return None
            confidence, angle = result.get('confidence'), result.get('angle_degrees')
            if isinstance(confidence, bool) or isinstance(angle, bool):
                return None
            try:
                confidence, angle = float(confidence), float(angle)
            except (TypeError, ValueError):
                return None
            reference = road_angle if road_angle is not None else fat_angle
            if (not math.isfinite(confidence) or not math.isfinite(angle)
                    or not max(.8, ai_confidence_min()) <= confidence <= 1.
                    or not -90. <= angle <= 90.
                    or abs(((angle - reference + 90.) % 180.) - 90.) > 15.):
                return None
            return {'angle_degrees': angle, 'confidence': confidence,
                    'width_scale': 1., 'depth_scale': 1., 'along_shift_m': 0.,
                    'normal_shift_m': 0., 'road_side': 1,
                    'reason': str(result.get('reason') or '').strip()}
        try:
            confidence = float(result.get('confidence', 0))
            angle = float(result.get('angle_degrees'))
            width = max(1.0, min(1.30, float(result.get('width_scale', 1.0))))
            depth = max(1.0, min(1.30, float(result.get('depth_scale', 1.0))))
            along = float(result.get('along_shift_m', 0.0) or 0.0)
            normal = float(result.get('normal_shift_m', 0.0) or 0.0)
            road_side = 1 if float(result.get('road_side', 1) or 1) >= 0 else -1
        except (AttributeError, TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in (confidence, angle, width, depth, along, normal)):
            return None
        if confidence < ai_confidence_min() or not -90 <= angle <= 90:
            return None
        shift = math.hypot(along, normal)
        if shift > 12.0:
            along *= 12.0 / shift
            normal *= 12.0 / shift
        return {
            'angle_degrees': angle, 'width_scale': width, 'depth_scale': depth,
            'along_shift_m': along, 'normal_shift_m': normal, 'road_side': road_side,
            'confidence': confidence, 'reason': str(result.get('reason') or '').strip(),
        }

    t_remaining = None
    if deadline_s is not None:
        t_remaining = max(0.0, deadline_s - time.monotonic())
        if t_remaining <= 0.5:
            return None

    eff_timeout = min(ai_timeout(), 60)
    if timeout is not None:
        eff_timeout = min(eff_timeout, float(timeout))
    if t_remaining is not None:
        eff_timeout = min(eff_timeout, t_remaining)

    result = _post_json(messages, max_tokens=180, timeout=eff_timeout, retry_response_format=not axis_only, allow_redirects=not axis_only)
    if result is None:
        return None
    hint = accepted(result)
    if hint is not None:
        return hint
    model = os.getenv('EVA_AI_FALLBACK_MODEL', '').strip()
    base_url = os.getenv('EVA_AI_FALLBACK_BASE_URL', '').strip()
    api_key = os.getenv('EVA_AI_API_KEY', '').strip() or os.getenv('OPENAI_API_KEY', '').strip()
    if not (model and base_url and api_key):
        return None

    if deadline_s is not None:
        t_remaining = max(0.0, deadline_s - time.monotonic())
        if t_remaining <= 0.5:
            return None

    sec_timeout = min(ai_timeout(), 30)
    if timeout is not None:
        sec_timeout = min(sec_timeout, float(timeout))
    if deadline_s is not None:
        sec_timeout = min(sec_timeout, t_remaining)

    LOGGER.info('AI visual hint rejected; retrying once with %s', model)
    secondary = _post_json(messages, max_tokens=180, timeout=sec_timeout,
                           provider=(api_key, base_url, model), retry_response_format=not axis_only, allow_redirects=not axis_only)
    hint = accepted(secondary) if secondary is not None else None
    if hint is not None:
        LOGGER.info('AI visual fallback accepted: model=%s confidence=%.2f', model, hint['confidence'])
    return hint


def suggest_dxf_hp_recovery(
    *, preview_png: bytes, reference_angle: float, point_count: int,
    placed_count: int, failure_kind: str, timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Review failed converter cells; return one bounded shared-axis/phase hint."""
    if not ai_visual_parcel_enabled() or not preview_png or point_count < 1:
        return None
    try:
        reference, budget = float(reference_angle), min(float(timeout), float(ai_timeout()), 10.0)
        if not math.isfinite(reference) or not math.isfinite(budget) or budget <= .5:
            return None
        import base64
        summary = {"schema": "hp-local-recovery-v1", "reference_angle": round(reference, 2),
                   "hp_cover_count": int(point_count), "converter_cell_count": int(placed_count),
                   "failure_kind": str(failure_kind)[:64],
                   "allowed_axis_deltas": [-15, -10, -5, 0, 5, 10, 15], "allowed_phases": [0, 1, 2]}
        result = _post_json([
            {"role": "system", "content":
             "Recheck a failed EVA HP COVER placement from the deterministic converter. "
             "Image: cyan=local extent, red=ROAD exclusion, yellow=immutable source points, "
             "green=converter-created cells (may conflict), orange=failed sources. "
             "Recommend a shared local re-layout axis and lattice origin, not individual moves. "
             "Return ONLY JSON with axis_delta_degrees, along_phase, normal_phase, confidence. "
             "Choose axis_delta_degrees only from allowed_axis_deltas and integer phases only from allowed_phases. "
             "A phase is one third of the converter's fixed width/depth, not an absolute coordinate. "
             "Keep one intact cell plus label per source, minimum4x8m, center/label within8m, "
             "same ROAD side, no ROAD/parcel overlap, original class and FAT ownership. "
             "Converter owns all coordinates and rejects geometrically invalid advice. "
             "Confidence concerns the axis/phase recommendation; never claim geometry verification. "
             "A confident axis replaces the trial axis; a weak one only reorders trials. "
             "Never return coordinates, polygons, new IDs, deletion or constraint overrides."},
            {"role": "user", "content": [
                {"type": "text", "text": json.dumps(summary, sort_keys=True)},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(preview_png).decode("ascii")}},
            ]},
        ], max_tokens=100, timeout=budget, retry_response_format=False, allow_redirects=False)
        keys = {"axis_delta_degrees", "along_phase", "normal_phase", "confidence"}
        if not isinstance(result, dict) or set(result) != keys:
            return None
        delta, along, normal, confidence = (result[k] for k in ("axis_delta_degrees", "along_phase", "normal_phase", "confidence"))
        if (any(isinstance(value, bool) for value in (delta, along, normal, confidence))
                or type(along) is not int or type(normal) is not int
                or not isinstance(delta, (int, float)) or not isinstance(confidence, (int, float))):
            return None
        threshold = ai_confidence_min()
        if (not math.isfinite(delta) or not math.isfinite(confidence) or not math.isfinite(threshold)
                or delta not in summary["allowed_axis_deltas"] or along not in (0, 1, 2) or normal not in (0, 1, 2)
                or not .5 <= confidence <= 1.0):
            return None
        # Confidence only decides how strongly the hint is used. Every geometry
        # guard stays deterministic, so a weak hint may reorder trials but never
        # replace the solver domain or bypass validation.
        return {"axis_delta_degrees": float(delta), "along_phase": along,
                "normal_phase": normal, "confidence": float(confidence),
                "authoritative": confidence >= max(.8, threshold)}
    except Exception:
        LOGGER.info("HP recovery advice unavailable; deterministic guards retained")
        return None


def suggest_dxf_asset_layout(
    *,
    asset_type: str,
    asset_name: str,
    cable_angle: float,
    has_serving_pole: bool = False,
) -> dict[str, Any] | None:
    """Ask EVA AI for visual layout hints for UG/KML DXF assets.

    AI may only choose side/offset/rotation hint. Deterministic converter.py
    still owns the actual anchor rules: pole/HH/slack keep source coordinate,
    FAT anchors to nearest pole, cable tangent controls rotation, anti-cluster
    stays deterministic.
    """
    if not ai_assist_enabled():
        return None
    system = (
        "You tune visual placement hints for EVA telecom DXF assets. Return ONLY JSON: "
        "{\"normal_side\": 1 or -1, \"offset_scale\": number, \"confidence\": number, \"reason\": string}. "
        "Rules: FAT must attach beside its serving pole; pole/slack/handhole keep source coordinates; labels rotate with cable; "
        "never cluster multiple assets into one point; do not output coordinates."
    )
    payload = {
        "asset_type": str(asset_type or "ASSET").upper(),
        "asset_name": str(asset_name or ""),
        "cable_angle_degrees": round(float(cable_angle), 2),
        "has_serving_pole": bool(has_serving_pole),
        "constraint": "side/offset hint only; converter keeps deterministic anchors and anti-overlap",
    }
    result = _post_json(
        [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload)}],
        max_tokens=120,
        timeout=min(ai_timeout(), 45),
    )
    if not result:
        return None
    try:
        confidence = float(result.get("confidence", 0))
        normal_side = 1 if float(result.get("normal_side", 1)) >= 0 else -1
        offset_scale = max(0.75, min(1.35, float(result.get("offset_scale", 1.0))))
    except (TypeError, ValueError):
        return None
    if confidence < ai_confidence_min():
        return None
    return {"normal_side": normal_side, "offset_scale": offset_scale, "confidence": confidence, "reason": str(result.get("reason") or "").strip()}


def suggest_dxf_fat_boundary_trim(summary: dict[str, Any]) -> dict[str, float] | None:
    """One bounded recommendation per drawing; deterministic geometry stays authoritative."""
    if not ai_assist_enabled():
        return None
    try:
        options = summary.get('safe_cut_options')
        if not isinstance(options, list):
            return None
        allowed = {float(row['simplify_tolerance_m']) for row in options
                   if isinstance(row, dict) and isinstance(row.get('simplify_tolerance_m'), (int, float))
                   and not isinstance(row['simplify_tolerance_m'], bool)
                   and math.isfinite(row['simplify_tolerance_m']) and .01 <= row['simplify_tolerance_m'] <= .5}
        if not allowed:
            return None
        result = _post_json([
            {"role": "system", "content":
             "Recommend a small simplification tolerance for redundant FAT AREA boundary vertices. "
             "Return only JSON: {\"simplify_tolerance_m\": number, \"confidence\": number}. "
             "The safe_cut_options were prevalidated by exact geometry: whole HP containment, "
             "one Polygon, no added area and zero foreign/FAT overlap. Select the option removing "
             "the most redundant vertices; prefer the smaller tolerance on ties. Confidence refers "
             "to this bounded choice among proven-safe options, not guessing unprovided geometry. "
             "Tolerance must be 0.01..0.5 metres. Never propose coordinates, move HP boxes, "
             "split FAT or authorize foreign overlap. Converter rejects any cut losing a full HP "
             "box, connectivity, validity or exclusion of other FAT/foreign HP."},
            {"role": "user", "content": json.dumps(summary, sort_keys=True)},
        ], max_tokens=100, timeout=min(ai_timeout(), 20), retry_response_format=False, allow_redirects=False)
        if not isinstance(result, dict):
            return None
        tolerance = result.get("simplify_tolerance_m")
        confidence = result.get("confidence")
        if isinstance(tolerance, bool) or isinstance(confidence, bool):
            return None
        tolerance, confidence = float(tolerance), float(confidence)
        if not (math.isfinite(tolerance) and math.isfinite(confidence)
                and tolerance in allowed and max(.8, ai_confidence_min()) <= confidence <= 1):
            return None
        return {"simplify_tolerance_m": tolerance, "confidence": confidence}
    except Exception:
        LOGGER.info("FAT AREA trim advice unavailable; retaining validated boundary")
        return None


# ── DXF metadata ────────────────────────────────────────────

def dxf_ai_metadata() -> dict[str, Any]:
    enabled = ai_assist_enabled()
    _, _, model = _provider_config() if enabled else ("", "", "")
    return {
        "enabled": enabled,
        "model": model,
        "confidence_min": ai_confidence_min(),
    }

def ai_status() -> dict:
    enabled = ai_assist_enabled()
    if enabled:
        api_key, base_url, model = _provider_config()
    else:
        api_key, base_url, model = "", "", ""
    memory = _load_memory()
    return {
        "enabled": enabled,
        "model": model,
        "base_url": base_url,
        "api_key_configured": bool(api_key),
        "confidence_min": ai_confidence_min(),
        "timeout": ai_timeout(),
        "reasoning_effort": ai_reasoning_effort(),
        "cache_ttl_seconds": ai_cache_ttl_seconds(),
        "cache_max_entries": ai_cache_max_entries(),
        "circuit_breaker_seconds": ai_circuit_breaker_seconds(),
        "skills_loaded": bool(_load_skills()),
        "memory_path": str(_MEMORY_PATH),
        "memory_count": len(memory.get("decisions", [])),
        "memory_updated_at": memory.get("updated_at"),
        "reference_path": str(_REFERENCE_PATH),
        "reference_samples": len((_load_reference() or {}).get("samples", []) or []),
    }
