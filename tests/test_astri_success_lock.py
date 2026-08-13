from pathlib import Path

import pytest

import app as web_app


SUCCESSFUL_ITEM = {
    "matched": True,
    "target_type": "cluster",
    "cluster_code": "BKS003685",
    "work_order_number": "WO/ALL/2026/DOCU/16/80681",
    "latest_status_label": "Successful",
    "latest_status_name": "successful",
}


class NoMutationSession:
    def post(self, *_args, **_kwargs):
        raise AssertionError("ASTRI mutation must not run for Successful WO")


class MutationResponse:
    status_code = 200
    headers = {}


class MutationSession:
    def __init__(self):
        self.calls = []

    def post(self, url, data=None, **_kwargs):
        self.calls.append((url, data))
        return MutationResponse()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_app, "require_astri_session", lambda: {"session": NoMutationSession()})
    web_app.app.config["TESTING"] = True
    with web_app.app.test_client() as test_client:
        yield test_client


def install_successful_searches(monkeypatch):
    def successful(_record, _query, status=""):
        assert status == ""
        return [dict(SUCCESSFUL_ITEM)]

    monkeypatch.setattr(web_app, "search_astri_clusters_v3", successful)
    monkeypatch.setattr(web_app, "search_astri_subfeeders", successful)
    monkeypatch.setattr(web_app, "search_astri_feeders", successful)


def test_successful_status_match_is_exact():
    assert web_app.astri_status_is_successful("Successful", "successful") is True
    assert web_app.astri_status_is_successful("Unsuccessful", "unsuccessful") is False
    assert web_app.astri_status_is_successful("Successfully Closed", "successfully_closed") is False


def test_live_status_selects_exact_code_and_work_order(monkeypatch):
    rows = [
        {**SUCCESSFUL_ITEM, "work_order_number": "WO/OTHER"},
        dict(SUCCESSFUL_ITEM),
    ]
    monkeypatch.setattr(web_app, "search_astri_clusters_v3", lambda *_args, **_kwargs: rows)

    status = web_app.fetch_astri_work_order_status(
        {"session": object()},
        {"cluster_code": SUCCESSFUL_ITEM["cluster_code"], "work_order_number": SUCCESSFUL_ITEM["work_order_number"]},
        "cluster",
    )

    assert status["successful"] is True
    assert status["latest_status_label"] == "Successful"
    assert status["work_order_number"] == SUCCESSFUL_ITEM["work_order_number"]


def test_schedule_guard_uses_outbound_astri_identifiers_before_eva_hidden_fields():
    match = web_app.astri_schedule_match_from_form({
        "cluster[cluster_code]": SUCCESSFUL_ITEM["cluster_code"],
        "work_order[number]": SUCCESSFUL_ITEM["work_order_number"],
        "__eva_cluster_code": "BKS-BYPASS",
        "__eva_work_order_number": "WO/BYPASS",
    }, "cluster")

    assert match == {
        "cluster_code": SUCCESSFUL_ITEM["cluster_code"],
        "work_order_number": SUCCESSFUL_ITEM["work_order_number"],
    }


@pytest.mark.parametrize(
    ("endpoint", "scope", "code_field"),
    [
        ("/api/astri/schedule/approve", "cluster", "cluster[cluster_code]"),
        ("/api/astri/subfeeder/schedule/approve", "subfeeder", "work_order[subfeeder_code]"),
        ("/api/astri/feeder/schedule/approve", "feeder", "osp_route[feeder_code]"),
    ],
)
def test_schedule_mutation_is_blocked_for_successful_wo(client, monkeypatch, endpoint, scope, code_field):
    install_successful_searches(monkeypatch)
    response = client.post(
        endpoint,
        data={
            "work_order[number]": SUCCESSFUL_ITEM["work_order_number"],
            code_field: SUCCESSFUL_ITEM["cluster_code"],
            "__eva_scope": scope,
        },
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["code"] == "ASTRI_WO_SUCCESSFUL"
    assert payload["wo_status"]["latest_status_label"] == "Successful"


@pytest.mark.parametrize(
    ("endpoint", "scope"),
    [
        ("/api/astri/boq/submit", "cluster"),
        ("/api/astri/subfeeder/boq/submit", "subfeeder"),
        ("/api/astri/feeder/boq/submit", "feeder"),
    ],
)
def test_boq_mutation_is_blocked_for_successful_wo(client, monkeypatch, endpoint, scope):
    install_successful_searches(monkeypatch)
    match = {**SUCCESSFUL_ITEM, "target_type": scope}
    response = client.post(endpoint, json={"match": match, "items": [{"code": "500001"}]})

    assert response.status_code == 409
    assert response.get_json()["code"] == "ASTRI_WO_SUCCESSFUL"


def test_status_lookup_failure_blocks_mutation(client, monkeypatch):
    monkeypatch.setattr(web_app, "search_astri_clusters_v3", lambda *_args, **_kwargs: [])
    response = client.post(
        "/api/astri/schedule/approve",
        data={
            "work_order[number]": SUCCESSFUL_ITEM["work_order_number"],
            "cluster[cluster_code]": SUCCESSFUL_ITEM["cluster_code"],
        },
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "ASTRI_WO_STATUS_UNAVAILABLE"


def test_status_endpoint_returns_canonical_live_status(client, monkeypatch):
    install_successful_searches(monkeypatch)
    response = client.get(
        "/api/astri/work-order/status",
        query_string={
            "scope": "cluster",
            "cluster_code": SUCCESSFUL_ITEM["cluster_code"],
            "work_order_number": SUCCESSFUL_ITEM["work_order_number"],
        },
    )

    assert response.status_code == 200
    assert response.get_json()["successful"] is True


@pytest.mark.parametrize(
    ("endpoint", "scope", "code_field"),
    [
        ("/api/astri/schedule/approve", "cluster", "cluster[cluster_code]"),
        ("/api/astri/subfeeder/schedule/approve", "subfeeder", "work_order[subfeeder_code]"),
        ("/api/astri/feeder/schedule/approve", "feeder", "osp_route[feeder_code]"),
    ],
)
def test_non_successful_schedule_still_posts_to_astri(monkeypatch, endpoint, scope, code_field):
    mutable_session = MutationSession()
    monkeypatch.setattr(web_app, "require_astri_session", lambda: {"session": mutable_session})
    live = {**SUCCESSFUL_ITEM, "target_type": scope, "latest_status_label": "In Progress", "latest_status_name": "in_progress"}
    monkeypatch.setattr(web_app, {
        "cluster": "search_astri_clusters_v3",
        "subfeeder": "search_astri_subfeeders",
        "feeder": "search_astri_feeders",
    }[scope], lambda *_args, **_kwargs: [live])
    web_app.app.config["TESTING"] = True

    with web_app.app.test_client() as test_client:
        response = test_client.post(endpoint, data={
            "work_order[number]": SUCCESSFUL_ITEM["work_order_number"],
            code_field: SUCCESSFUL_ITEM["cluster_code"],
            "__eva_scope": scope,
            "__eva_cluster_code": SUCCESSFUL_ITEM["cluster_code"],
            "__eva_work_order_number": SUCCESSFUL_ITEM["work_order_number"],
        })

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert len(mutable_session.calls) == 1
    _, outbound_data = mutable_session.calls[0]
    assert all(not key.startswith("__eva_") for key in outbound_data)


@pytest.mark.parametrize(
    ("endpoint", "scope", "link_helper"),
    [
        ("/api/astri/boq/submit", "cluster", "astri_create_boq_link"),
        ("/api/astri/subfeeder/boq/submit", "subfeeder", "astri_create_subfeeder_boq_link"),
        ("/api/astri/feeder/boq/submit", "feeder", "astri_create_feeder_boq_link"),
    ],
)
def test_non_successful_boq_still_reaches_submit(monkeypatch, endpoint, scope, link_helper):
    live = {
        **SUCCESSFUL_ITEM,
        "target_type": scope,
        "latest_status_label": "In Progress",
        "latest_status_name": "in_progress",
    }
    monkeypatch.setattr(web_app, "require_astri_session", lambda: {"session": NoMutationSession()})
    monkeypatch.setattr(web_app, {
        "cluster": "search_astri_clusters_v3",
        "subfeeder": "search_astri_subfeeders",
        "feeder": "search_astri_feeders",
    }[scope], lambda *_args, **_kwargs: [live])
    monkeypatch.setattr(web_app, link_helper, lambda *_args, **_kwargs: {"url": "https://astri.invalid/insert", "enabled": True})
    submitted = []

    def fake_submit(*args, **kwargs):
        submitted.append((args, kwargs))
        return {"ok": True, "close_ok": True, "inserted": []}

    monkeypatch.setattr(web_app, "submit_astri_boq_items", fake_submit)
    web_app.app.config["TESTING"] = True

    match = {**live, "cluster_code": SUCCESSFUL_ITEM["cluster_code"]}
    with web_app.app.test_client() as test_client:
        response = test_client.post(endpoint, json={
            "match": match,
            "items": [{"code": "500001", "material_qty": 1}],
        })

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert len(submitted) == 1


def test_frontend_notifies_status_and_disables_both_actions():
    source = Path("static/js/app.js").read_text(encoding="utf-8")
    assert "/api/astri/work-order/status" in source
    assert "Status WO ASTRI" in source
    assert "button.disabled = locked" in source
    assert '"astriScheduleButton", "astriAddBoqButton"' in source
    assert "refreshAstriWorkOrderStatus" in source
    assert "error.payload = payload" in source
    assert "handleAstriMutationError" in source
    assert "error?.payload?.wo_status" in source
    assert "status?.successful || status?.unavailable" in source
    assert "applyAstriWorkOrderStatus({ unavailable: true })" in source


def test_boq_status_check_uses_modal_owned_match():
    source = Path("static/js/app.js").read_text(encoding="utf-8")
    assert "matchOverride = null" in source
    assert "const activeBoqMatch = state.astriBoqMatch || state.astriMatch || {};" in source
    assert "refreshAstriWorkOrderStatus({ matchOverride: activeBoqMatch })" in source
    assert "match: activeBoqMatch" in source


@pytest.mark.parametrize(
    ("endpoint", "scope"),
    [
        ("/api/astri/boq/submit/job-1", "cluster"),
        ("/api/astri/subfeeder/boq/submit/job-1", "subfeeder"),
        ("/api/astri/feeder/boq/submit/job-1", "feeder"),
    ],
)
def test_job_boq_routes_are_blocked_before_files_or_mutation(client, monkeypatch, endpoint, scope):
    install_successful_searches(monkeypatch)
    monkeypatch.setattr(web_app, "get_job", lambda _job_id: {"id": "job-1"})
    monkeypatch.setattr(web_app, "cluster_name_for_job", lambda _job: SUCCESSFUL_ITEM["cluster_code"])
    monkeypatch.setattr(web_app, "find_astri_cluster", lambda *_args: {
        "Target Cluster Code": SUCCESSFUL_ITEM["cluster_code"],
        "Target Cluster Name": "TEST",
        "Number": SUCCESSFUL_ITEM["work_order_number"],
    })

    response = client.post(endpoint, json={"items": [{"code": "500001"}]})

    assert response.status_code == 409
    assert response.get_json()["code"] == "ASTRI_WO_SUCCESSFUL"


def test_job_schedule_route_is_blocked_before_mutation(client, monkeypatch):
    install_successful_searches(monkeypatch)
    monkeypatch.setattr(web_app, "get_job", lambda _job_id: {"id": "job-1"})

    response = client.post(
        "/api/astri/schedule/approve/job-1",
        data={
            "work_order[number]": SUCCESSFUL_ITEM["work_order_number"],
            "cluster[cluster_code]": SUCCESSFUL_ITEM["cluster_code"],
        },
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "ASTRI_WO_SUCCESSFUL"


@pytest.mark.parametrize(
    ("endpoint", "scope", "search_name"),
    [
        ("/api/astri/boq/submit/job-1", "cluster", "search_astri_clusters_v3"),
        ("/api/astri/subfeeder/boq/submit/job-1", "subfeeder", "search_astri_subfeeders"),
        ("/api/astri/feeder/boq/submit/job-1", "feeder", "search_astri_feeders"),
    ],
)
def test_job_boq_guard_uses_payload_exact_match_not_rediscovered_first_row(client, monkeypatch, endpoint, scope, search_name):
    target = {
        **SUCCESSFUL_ITEM,
        "target_type": scope,
        "subfeeder_code": SUCCESSFUL_ITEM["cluster_code"] if scope == "subfeeder" else "",
        "feeder_code": SUCCESSFUL_ITEM["cluster_code"] if scope == "feeder" else "",
    }
    wrong = {
        **target,
        "work_order_number": "WO/OTHER",
        "latest_status_label": "In Progress",
        "latest_status_name": "in_progress",
    }

    monkeypatch.setattr(web_app, "get_job", lambda _job_id: {"id": "job-1"})
    monkeypatch.setattr(web_app, "cluster_name_for_job", lambda _job: "WRONG-CODE - OLD JOB")
    monkeypatch.setattr(web_app, search_name, lambda _record, query, status="": [target] if query == SUCCESSFUL_ITEM["cluster_code"] else [wrong])
    monkeypatch.setattr(web_app, "find_astri_cluster", lambda *_args: {
        "Target Cluster Code": "WRONG-CODE",
        "Target Cluster Name": "OLD JOB",
        "Number": "WO/OTHER",
    })

    response = client.post(endpoint, json={
        "match": target,
        "items": [{"code": "500001"}],
    })

    assert response.status_code == 409
    assert response.get_json()["code"] == "ASTRI_WO_SUCCESSFUL"


def test_frontend_job_boq_payload_carries_modal_exact_match():
    source = Path("static/js/app.js").read_text(encoding="utf-8")
    assert "items: merged, match: activeBoqMatch" in source
