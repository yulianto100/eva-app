from __future__ import annotations

import html
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any


_SCOPE_CLASS_ALIASES = {
    "cluster": ("cluster_code",),
    "subfeeder": ("subfeeder_code", "cluster_code"),
    "feeder": ("feeder_code", "osp_route_code", "target_osp_route_code"),
}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _vendor_key(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = _key(raw)
    if normalized in {"internal", "internalteam"}:
        return "internal"
    # Comparison-only normalization: whitespace separates words, while other
    # punctuation remains significant. Preserve the one proven Buana display
    # alias whose ASTRI machine key ends in an underscore.
    without_legal_suffix = re.sub(
        r"(?:[\s,]+(?:PT\.?|PERSEROAN\s+TERBATAS))$",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    comparison = without_legal_suffix.casefold()
    if re.fullmatch(r"[a-z0-9_\s]+", comparison):
        compact = re.sub(r"[_\s]+", "", comparison)
        if compact == "buanamenaraindonesia":
            return "buana_menara_indonesia_"
        return compact
    return re.sub(r"\s+", "_", comparison)


def _quantity(value: Any) -> str:
    raw = str(value or "0").strip().replace(" ", "")
    if not raw:
        return "0"
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return raw.casefold()
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _work_order(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip().casefold()


def _item_code(item: dict[str, Any]) -> str:
    return str(
        item.get("code")
        or item.get("material_number")
        or item.get("material_code")
        or item.get("equipment_name")
        or ""
    ).strip()


def _item_vendor(item: dict[str, Any]) -> str:
    return str(item.get("supplier") or item.get("vendor_name") or item.get("vendor") or "").strip()


def _expected_rows(items: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    expected: list[tuple[str, str, str, str]] = []
    for item in items or []:
        material = _key(_item_code(item))
        material_qty = _quantity(item.get("material_qty") or item.get("quantity_material") or item.get("qty_material"))
        service_qty = _quantity(item.get("service_qty") or item.get("quantity_service") or item.get("qty_service"))
        if material and (material_qty != "0" or service_qty != "0"):
            expected.append((material, _vendor_key(_item_vendor(item)), material_qty, service_qty))
    return expected


def split_existing_boq_items(
    planned_items: list[dict[str, Any]],
    live_rows: list[tuple[str, str, str, str]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Split a retry plan without creating duplicate BOQ rows.

    Exact live code/vendor/qty rows are already complete and skipped. A live
    row with the same material code but different vendor/qty is a conflict and
    must block the retry rather than insert another row.
    """
    live = Counter(
        (_key(code), _vendor_key(vendor), _quantity(material_qty), _quantity(service_qty))
        for code, vendor, material_qty, service_qty in (live_rows or [])
    )
    live_codes = {row[0] for row in live}
    pending: list[dict[str, Any]] = []
    already_present: list[str] = []
    conflicts: list[str] = []
    for item in planned_items or []:
        display_code = _item_code(item)
        row = (
            _key(display_code),
            _vendor_key(_item_vendor(item)),
            _quantity(item.get("material_qty") or item.get("quantity_material") or item.get("qty_material")),
            _quantity(item.get("service_qty") or item.get("quantity_service") or item.get("qty_service")),
        )
        if not row[0] or (row[2] == "0" and row[3] == "0"):
            continue
        if live[row] > 0:
            live[row] -= 1
            already_present.append(display_code)
        elif row[0] in live_codes:
            conflicts.append(display_code)
        else:
            pending.append(item)
    return pending, already_present, conflicts


def _with_generated_permit(
    items: list[dict[str, Any]],
    permit_rt_rw: Any = "",
    permit_vendor: Any = "",
) -> list[dict[str, Any]]:
    """Include ASTRI's generated RT/RW Permit line in the reconciliation target."""
    clean = [dict(item) for item in (items or [])]
    has_permit = any(_key(_item_code(item)) == "500002155" for item in clean)
    amount = str(permit_rt_rw or "").strip().replace(".", "").replace(",", ".")
    try:
        has_amount = Decimal(amount or "0") > 0
    except InvalidOperation:
        has_amount = bool(amount)
    if has_amount and not has_permit:
        clean.append({
            "code": "500002155",
            "description": "RT/RW Permit",
            "service_qty": 1,
            "supplier": str(permit_vendor or (_item_vendor(clean[0]) if clean else "")).strip(),
        })
    return clean


def _description_aliases(items: list[dict[str, Any]]) -> dict[str, str]:
    """Map ASTRI text labels back to stable material codes when labels replace codes."""
    aliases: dict[str, str] = {}
    collisions: set[str] = set()
    for item in items or []:
        code = _key(_item_code(item))
        if not code:
            continue
        values = (code, item.get("description"), item.get("equipment_name"), item.get("material_description"))
        for value in values:
            alias = _key(value)
            if not alias:
                continue
            previous = aliases.get(alias)
            if previous and previous != code:
                collisions.add(alias)
            else:
                aliases[alias] = code
    for alias in collisions:
        aliases.pop(alias, None)
    return aliases


def _parse_boq_rows(
    scope: str,
    scope_code: str,
    page_html: str,
    work_order_number: str = "",
) -> tuple[list[tuple[str, str, str, str]], dict[str, int | bool]]:
    normalized_scope = str(scope or "").strip().lower()
    aliases = _SCOPE_CLASS_ALIASES.get(normalized_scope)
    if not aliases:
        raise ValueError("Scope Add BOQ tidak valid.")
    wanted_code = _key(scope_code)
    if not wanted_code:
        raise ValueError("Kode scope Add BOQ kosong.")
    wanted_work_order = _work_order(work_order_number)
    rows: list[tuple[str, str, str, str]] = []
    work_order_field_seen = False
    target_rows_seen = 0
    target_rows_missing_work_order = 0
    target_rows_wrong_work_order = 0
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page_html or "", flags=re.I | re.S):
        cells: dict[str, str] = {}
        for attrs, body in re.findall(r"<td\b([^>]*)>(.*?)</td>", row_html, flags=re.I | re.S):
            class_match = re.search(r"\bclass\s*=\s*(['\"])(.*?)\1", attrs, flags=re.I | re.S)
            if not class_match:
                continue
            value = _text(body)
            for class_name in class_match.group(2).split():
                key = re.sub(r"(?:-|_)column$", "", class_name.strip().lower()).replace("-", "_")
                cells[key] = value
        code = next((cells.get(alias, "") for alias in aliases if cells.get(alias)), "")
        if _key(code) != wanted_code:
            continue
        phase = _key(cells.get("phase"))
        if phase and phase != "drm":
            continue
        target_rows_seen += 1
        work_order_keys = ("work_order_number", "number", "work_order", "wo")
        has_work_order_field = any(key in cells for key in work_order_keys)
        work_order_field_seen = work_order_field_seen or has_work_order_field
        live_work_order = next((cells.get(key, "") for key in work_order_keys if cells.get(key, "")), "")
        if wanted_work_order:
            if has_work_order_field and not _work_order(live_work_order):
                target_rows_missing_work_order += 1
            elif has_work_order_field and _work_order(live_work_order) != wanted_work_order:
                target_rows_wrong_work_order += 1
                continue
        material = _key(cells.get("equipment_label") or cells.get("material_number") or cells.get("equipment_name"))
        if not material:
            continue
        rows.append((
            material,
            _vendor_key(cells.get("vendor_label") or cells.get("vendor_name")),
            _quantity(cells.get("quantity_material")),
            _quantity(cells.get("quantity_service")),
        ))
    return rows, {
        "work_order_field_seen": work_order_field_seen,
        "target_rows_seen": target_rows_seen,
        "target_rows_missing_work_order": target_rows_missing_work_order,
        "target_rows_wrong_work_order": target_rows_wrong_work_order,
    }


def parse_boq_rows(
    scope: str,
    scope_code: str,
    page_html: str,
    work_order_number: str = "",
) -> list[tuple[str, str, str, str]]:
    rows, _meta = _parse_boq_rows(scope, scope_code, page_html, work_order_number)
    return rows


def parse_boq_records(
    scope: str,
    scope_code: str,
    page_html: str,
    work_order_number: str = "",
) -> list[dict[str, Any]]:
    """Parse live BOQ rows with UUIDs for ASTRI's official quantity update form."""
    normalized_scope = str(scope or "").strip().lower()
    aliases = _SCOPE_CLASS_ALIASES.get(normalized_scope)
    if not aliases:
        raise ValueError("Scope Add BOQ tidak valid.")
    wanted_code = _key(scope_code)
    if not wanted_code:
        raise ValueError("Kode scope Add BOQ kosong.")
    wanted_work_order = _work_order(work_order_number)
    records: list[dict[str, Any]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page_html or "", flags=re.I | re.S):
        cells: dict[str, str] = {}
        for attrs, body in re.findall(r"<td\b([^>]*)>(.*?)</td>", row_html, flags=re.I | re.S):
            class_match = re.search(r"\bclass\s*=\s*(['\"])(.*?)\1", attrs, flags=re.I | re.S)
            if not class_match:
                continue
            value = _text(body)
            for class_name in class_match.group(2).split():
                key = re.sub(r"(?:-|_)column$", "", class_name.strip().lower()).replace("-", "_")
                cells[key] = value
        code = next((cells.get(alias, "") for alias in aliases if cells.get(alias)), "")
        if _key(code) != wanted_code:
            continue
        phase = _key(cells.get("phase"))
        if phase and phase != "drm":
            continue
        work_order_keys = ("work_order_number", "number", "work_order", "wo")
        live_work_order = next((cells.get(key, "") for key in work_order_keys if cells.get(key, "")), "")
        if wanted_work_order and live_work_order and _work_order(live_work_order) != wanted_work_order:
            continue
        uuid_match = re.search(r"\bdata-uuid\s*=\s*(['\"])(.*?)\1", row_html, flags=re.I | re.S)
        row_uuid = html.unescape(uuid_match.group(2)).strip() if uuid_match else ""
        if not row_uuid:
            continue
        records.append({
            "uuid": row_uuid,
            "code": str(cells.get("equipment_label") or cells.get("material_number") or cells.get("equipment_name") or "").strip(),
            "scope_code": str(code).strip(),
            "scope_name": str(cells.get("subfeeder_name") or cells.get("cluster_name") or cells.get("feeder_name") or "").strip(),
            "description": str(cells.get("description") or "").strip(),
            "vendor": str(cells.get("vendor_label") or cells.get("vendor_name") or "").strip(),
            "quantity_material": str(cells.get("quantity_material") or "0").strip() or "0",
            "quantity_service": str(cells.get("quantity_service") or "0").strip() or "0",
            "override_price_material": str(cells.get("override_price_material") or "0").strip() or "0",
            "override_price_service": str(cells.get("override_price_service") or "0").strip() or "0",
            "phase": phase or "drm",
        })
    return records


def build_boq_overwrite_actions(
    planned_items: list[dict[str, Any]],
    live_records: list[dict[str, Any]],
) -> dict[str, list[Any]]:
    """Plan exact rows, UUID quantity updates, new inserts, and warnings."""
    by_code: dict[str, list[dict[str, Any]]] = {}
    for record in live_records or []:
        code = _key(record.get("code"))
        if code:
            by_code.setdefault(code, []).append(record)

    def value(item: dict[str, Any], *keys: str, default: str = "") -> str:
        for key in keys:
            if key in item and item.get(key) is not None:
                return str(item.get(key)).strip()
        return default

    def qty(item: dict[str, Any], *keys: str) -> str:
        return _quantity(value(item, *keys, default="0"))

    pending: list[dict[str, Any]] = []
    already_present: list[str] = []
    updates: list[dict[str, Any]] = []
    vendor_mismatches: list[dict[str, Any]] = []
    unupdatable: list[dict[str, Any]] = []
    for planned in planned_items or []:
        code = _item_code(planned)
        code_key = _key(code)
        material_qty = qty(planned, "quantity_material", "material_qty", "qty_material")
        service_qty = qty(planned, "quantity_service", "service_qty", "qty_service")
        if not code_key or (material_qty == "0" and service_qty == "0"):
            continue
        target_vendor = _item_vendor(planned)
        target_override_material = value(planned, "override_price_material", default="")
        target_override_service = value(planned, "override_price_service", default="")
        matches = by_code.get(code_key, [])
        if not matches:
            pending.append(planned)
            continue
        for live in matches:
            live_material = _quantity(live.get("quantity_material"))
            live_service = _quantity(live.get("quantity_service"))
            live_override_material = value(live, "override_price_material", default="0") or "0"
            live_override_service = value(live, "override_price_service", default="0") or "0"
            next_override_material = target_override_material or live_override_material
            next_override_service = target_override_service or live_override_service
            vendor_before = str(live.get("vendor") or "").strip()
            vendor_mismatch = bool(target_vendor and vendor_before and _vendor_key(target_vendor) != _vendor_key(vendor_before))
            changed_fields = []
            if live_material != material_qty:
                changed_fields.append("quantity_material")
            if live_service != service_qty:
                changed_fields.append("quantity_service")
            if _quantity(live_override_material) != _quantity(next_override_material):
                changed_fields.append("override_price_material")
            if _quantity(live_override_service) != _quantity(next_override_service):
                changed_fields.append("override_price_service")
            if vendor_mismatch:
                vendor_mismatches.append({
                    "code": code,
                    "uuid": live.get("uuid", ""),
                    "vendor_before": vendor_before,
                    "vendor_target": target_vendor,
                    "vendor_updated": False,
                })
            if not changed_fields:
                if not vendor_mismatch:
                    already_present.append(code)
                continue
            action = {
                "action": "overwrite",
                "code": code,
                "uuid": str(live.get("uuid") or "").strip(),
                "description": str(live.get("description") or planned.get("description") or "").strip(),
                "scope_name": str(live.get("scope_name") or "").strip(),
                "vendor_before": vendor_before,
                "vendor_target": target_vendor,
                "vendor_updated": False,
                "changed_fields": changed_fields,
                "before": {
                    "quantity_material": live_material,
                    "quantity_service": live_service,
                    "override_price_material": live_override_material,
                    "override_price_service": live_override_service,
                },
                "after": {
                    "quantity_material": material_qty,
                    "quantity_service": service_qty,
                    "override_price_material": next_override_material,
                    "override_price_service": next_override_service,
                },
            }
            if not action["uuid"]:
                unupdatable.append({
                    "code": code,
                    "description": action["description"],
                    "reason": "UUID ASTRI tidak tersedia; tidak dibuat row duplikat.",
                })
            else:
                updates.append(action)
    return {
        "pending": pending,
        "already_present": already_present,
        "updates": updates,
        "vendor_mismatches": vendor_mismatches,
        "unupdatable": unupdatable,
    }


def compare_boq_rows(
    scope: str,
    scope_code: str,
    expected_items: list[dict[str, Any]],
    page_html: str,
    work_order_number: str = "",
    permit_rt_rw: str = "",
    permit_vendor: str = "",
    allow_unexpected: bool = False,
) -> dict[str, Any]:
    reconciliation_items = _with_generated_permit(expected_items, permit_rt_rw, permit_vendor)
    expected = Counter(_expected_rows(reconciliation_items))
    live_rows, meta = _parse_boq_rows(scope, scope_code, page_html, work_order_number)
    aliases = _description_aliases(reconciliation_items)
    live_rows = [
        (aliases.get(material, material), vendor, material_qty, service_qty)
        for material, vendor, material_qty, service_qty in live_rows
    ]
    live = Counter(live_rows)
    duplicate_count = sum(count - 1 for count in live.values() if count > 1)
    missing_count = sum((expected - live).values())
    unexpected_count = sum((live - expected).values())
    work_order_required = bool(_work_order(work_order_number))
    # The exact WO is verified by the preceding GET-only status lookup. ASTRI's
    # BOQ list normally omits WO per row; enforce WO only when the list exposes it.
    work_order_valid = (
        not work_order_required
        or not bool(meta["work_order_field_seen"])
        or (
            int(meta["target_rows_missing_work_order"]) == 0
            and int(meta["target_rows_wrong_work_order"]) == 0
        )
    )
    return {
        "matched": bool(expected) and missing_count == 0 and duplicate_count == 0 and work_order_valid and (allow_unexpected or unexpected_count == 0),
        "expected_count": sum(expected.values()),
        "live_count": sum(live.values()),
        "duplicate_count": duplicate_count,
        "missing_count": missing_count,
        "unexpected_count": unexpected_count,
        "work_order_required": work_order_required,
        "work_order_field_seen": bool(meta["work_order_field_seen"]),
        "work_order_mismatch": work_order_required and not work_order_valid,
    }


def fetch_boq_reconciliation(
    session: Any,
    base_url: str,
    scope: str,
    scope_code: str,
    expected_items: list[dict[str, Any]],
    work_order_number: str = "",
    permit_rt_rw: str = "",
    permit_vendor: str = "",
    allow_unexpected: bool = False,
) -> dict[str, Any]:
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in _SCOPE_CLASS_ALIASES:
        raise ValueError("Scope Add BOQ tidak valid.")
    filter_field = {"cluster": "cluster_code", "subfeeder": "subfeeder_code", "feeder": "feeder_code"}[normalized_scope]
    # The BOQ list surface has no reliable WO column/filter. WO ownership is
    # proven by the exact status GET; this list stays scope-native only.
    params = {f"filter[{filter_field}]": scope_code, "limit": -1}
    response = session.get(
        f"{str(base_url).rstrip('/')}/material/{normalized_scope}/boq-drm/list/",
        params=params,
        timeout=60,
    )
    response.raise_for_status()
    return compare_boq_rows(
        normalized_scope,
        scope_code,
        expected_items,
        response.text,
        work_order_number,
        permit_rt_rw,
        permit_vendor,
        allow_unexpected=allow_unexpected,
    )
