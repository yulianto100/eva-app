from __future__ import annotations
import json
import os
import re
import logging
import time
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger("eva.ai_safe")


BASE_DIR = Path(__file__).resolve().parent
_MEMORY_PATH = BASE_DIR / "data" / "eva_ai_memory.json"
_SKILLS_PATH = BASE_DIR / "data" / "eva_ai_skills.json"
_REFERENCE_PATH = BASE_DIR / "data" / "eva_kmz_reference.json"


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
    return _env_int("EVA_AI_TIMEOUT", 20)


# ── generic LLM call ────────────────────────────────────────

def _post_json(messages: list[dict[str, Any]], *, max_tokens: int = 220, timeout: int | None = None) -> dict[str, Any] | None:
    api_key, base_url, model = _provider_config()
    if not api_key or not base_url:
        return None
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = timeout or ai_timeout()
    try:
        url = _provider_url(base_url)
        t0 = time.monotonic()
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code in {400, 415, 422}:
            payload.pop("response_format", None)
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
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
        LOGGER.info("AI respond in %.2fs", elapsed)
        return parsed if isinstance(parsed, dict) else None
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
        timeout=min(ai_timeout(), 12),
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


def suggest_dxf_visual_parcel_layout(*, preview_svg: str, fat_angle: float, road_angle: float | None, point_count: int) -> dict[str, Any] | None:
    """Vision-only parcel hint. SVG shows FAT=cyan, ROAD=red, HP=yellow.
    It returns hints only; converter owns all coordinates and topology."""
    if not ai_visual_parcel_enabled() or not preview_svg or point_count < 2:
        return None
    import base64
    image = 'data:image/svg+xml;base64,' + base64.b64encode(preview_svg.encode('utf-8')).decode('ascii')
    system = (
        'You visually review EVA telecom HP COVER parcel layout. Return ONLY JSON: '
        '{"angle_degrees":number,"width_scale":number,"depth_scale":number,"road_side":1,"confidence":number,"reason":string}. '
        'Image legend: cyan=FAT boundary, red=ROAD exclusion, yellow=fixed HP source points. '
        'Choose ONE clean shared grid angle. Do not output coordinates or polygons. '
        'HP points must never move; parcels must stay inside cyan and outside red, not overlap.'
    )
    content = [
        {'type': 'text', 'text': json.dumps({'fat_angle_degrees': round(float(fat_angle), 2), 'road_angle_degrees': round(float(road_angle), 2) if road_angle is not None else None, 'hp_cover_count': int(point_count)})},
        {'type': 'image_url', 'image_url': {'url': image}},
    ]
    result = _post_json([{'role': 'system', 'content': system}, {'role': 'user', 'content': content}], max_tokens=160, timeout=min(ai_timeout(), 20))
    if not result:
        return None
    try:
        confidence=float(result.get('confidence', 0)); angle=float(result.get('angle_degrees'))
        width=max(0.70, min(1.30, float(result.get('width_scale', 1.0))))
        depth=max(0.70, min(1.30, float(result.get('depth_scale', 1.0))))
        side=1 if float(result.get('road_side', 1)) >= 0 else -1
    except (TypeError, ValueError):
        return None
    if confidence < ai_confidence_min() or not -90 <= angle <= 90:
        return None
    return {'angle_degrees': angle, 'width_scale': width, 'depth_scale': depth, 'road_side': side, 'confidence': confidence, 'reason': str(result.get('reason') or '')}


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
        timeout=min(ai_timeout(), 8),
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
        "skills_loaded": bool(_load_skills()),
        "memory_path": str(_MEMORY_PATH),
        "memory_count": len(memory.get("decisions", [])),
        "memory_updated_at": memory.get("updated_at"),
        "reference_path": str(_REFERENCE_PATH),
        "reference_samples": len((_load_reference() or {}).get("samples", []) or []),
    }
