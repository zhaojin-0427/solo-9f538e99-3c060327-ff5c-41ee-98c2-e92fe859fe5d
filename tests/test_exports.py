"""Sanitized data-delivery API: width gating, CSV content, revision safety."""
from __future__ import annotations

import csv
import io

import pytest

from conftest import check, create_instance, create_package

ALL = [
    "acct:Cash", "acct:Receivables", "acct:Inventory", "acct:CurrentAssets",
    "acct:NonCurrentAssets", "acct:Assets",
    "acct:Revenues", "acct:Expenses", "acct:NetIncome",
]
# Everything public except Cash, so Cash alone must absorb the rounding slack of
# CurrentAssets - Receivables - Inventory: [98.5, 101.5], width 3.
RETAINED_WIDE = [c for c in ALL if c != "acct:Cash"]


def _feasible_report(client, samples, xml=None):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], xml or samples["feasible"])
    rep = check(client, inst["instance_id"], 1, 1)
    assert rep.status_code == 200, rep.text
    return pkg, inst, rep.json()["report"]


def _export(client, report, retained, sensitive, inst_rev=1, pkg_rev=1):
    return client.post("/exports", json={
        "report_id": report["report_id"],
        "expected_instance_revision": inst_rev,
        "expected_package_revision": pkg_rev,
        "retained_concepts": retained,
        "sensitive_concepts": sensitive,
    })


def _csv_rows(data: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


# ------------------------------------------------------------------ happy path
def test_publishes_csv_with_only_retained_concepts(client, samples):
    _, _, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 2}])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["row_count"] == len(RETAINED_WIDE)
    # Cash is bounded to [98.5, 101.5] once everything else is public: width 3.
    # The response exposes the width only — never where the hidden amount sits.
    cash = out["ranges"]["acct:Cash"]
    assert cash["unbounded"] is False
    assert cash["width"] == pytest.approx(3.0)
    assert cash["satisfied"] is True
    assert "min" not in cash and "max" not in cash
    assert "98.5" not in r.text and "101.5" not in r.text

    dl = client.get(out["download_path"])
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("text/csv")
    assert "attachment" in dl.headers["content-disposition"]
    rows = _csv_rows(dl.content)
    assert set(rows[0].keys()) == {"concept", "value", "decimals"}
    assert {row["concept"] for row in rows} == set(RETAINED_WIDE)

    by_concept = {row["concept"]: row for row in rows}
    # report values preserved verbatim
    assert by_concept["acct:Assets"]["value"] == "500"
    assert by_concept["acct:Assets"]["decimals"] == "0"
    assert by_concept["acct:NetIncome"]["value"] == "300"

    # no sensitive amount, no witness, no raw snapshot in the delivered bytes
    text = dl.content.decode()
    assert "acct:Cash" not in text
    assert "100" not in {row["value"] for row in rows}
    assert "<xbrli" not in text and "<acct:" not in text
    assert "true_value" not in text and "lower" not in text
    import hashlib
    assert hashlib.sha256(dl.content).hexdigest() == out["sha256"]


def test_unbounded_sensitive_concept_satisfies_any_width(client, samples):
    _, _, report = _feasible_report(client, samples)
    # With Receivables / Inventory also free, Cash can absorb any amount.
    retained = ["acct:Assets", "acct:CurrentAssets", "acct:NonCurrentAssets",
                "acct:Revenues", "acct:Expenses", "acct:NetIncome"]
    r = _export(client, report, retained,
                [{"concept": "acct:Cash", "min_width": 1e9}])
    assert r.status_code == 201, r.text
    assert r.json()["ranges"]["acct:Cash"]["unbounded"] is True


def test_insufficient_width_lists_concepts_and_leaves_nothing(client, samples):
    _, _, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 5}])
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "insufficient_inference_width"
    assert body["details"]["insufficient_concepts"] == ["acct:Cash"]

    assert client.get("/exports").json() == []


def test_partial_failure_lists_only_narrow_concepts(client, samples):
    _, _, report = _feasible_report(client, samples)
    retained = [c for c in ALL if c not in ("acct:Cash", "acct:NetIncome")]
    r = _export(client, report, retained, [
        {"concept": "acct:Cash", "min_width": 5},       # width 3 -> too narrow
        {"concept": "acct:NetIncome", "min_width": 1},  # width 2 -> fine
    ])
    assert r.status_code == 422
    assert r.json()["details"]["insufficient_concepts"] == ["acct:Cash"]
    assert client.get("/exports").json() == []


def test_retained_and_sensitive_must_not_overlap(client, samples):
    _, _, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE + ["acct:Cash"],
                [{"concept": "acct:Cash", "min_width": 1}])
    assert r.status_code == 422
    assert "both retained" in r.json()["error"]["message"]


@pytest.mark.parametrize("width", [0, -1, 0.0])
def test_min_width_must_be_positive(client, samples, width):
    _, _, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": width}])
    assert r.status_code == 422


def test_unknown_or_empty_concepts_rejected(client, samples):
    _, _, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Ghost", "min_width": 1}])
    assert r.status_code == 422
    assert "Ghost" in r.json()["error"]["message"]

    r = _export(client, report, ["acct:Ghost"],
                [{"concept": "acct:Cash", "min_width": 1}])
    assert r.status_code == 422

    r = _export(client, report, RETAINED_WIDE, [])
    assert r.status_code == 422


def test_only_feasible_current_report_can_be_used(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["infeasible"])
    infeasible = check(client, inst["instance_id"], 1, 1).json()["report"]
    r = _export(client, infeasible, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 1}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "report_not_feasible"

    # a feasible report that later becomes superseded may not be exported either
    pkg2 = create_package(client, samples)
    inst2 = create_instance(client, pkg2["package_id"], samples["feasible"])
    old = check(client, inst2["instance_id"], 1, 1).json()["report"]
    client.post(
        f"/instances/{inst2['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    fresh = check(client, inst2["instance_id"], 2, 1).json()["report"]
    assert fresh["report_id"] != old["report_id"]
    r = _export(client, old, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 1}], inst_rev=1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "report_not_current"

    late = check(client, inst2["instance_id"], 1, 1, late=True).json()["report"]
    r = _export(client, late, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 1}], inst_rev=1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "report_not_current"

    r = _export(client, fresh, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 2}], inst_rev=2)
    assert r.status_code == 201, r.text


def test_expected_revisions_must_match_the_report(client, samples):
    _, _, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 1}], inst_rev=7, pkg_rev=1)
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "stale_revision"

    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 1}], inst_rev=1, pkg_rev=9)
    assert r.status_code == 412


# ---------------------------------------------------- corrections around export
def test_correction_between_report_and_export_is_refused(client, samples):
    _, inst, report = _feasible_report(client, samples)
    r = client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    assert r.status_code == 200

    resp = _export(client, report, RETAINED_WIDE,
                   [{"concept": "acct:Cash", "min_width": 1}])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "source_revision_changed"
    assert client.get("/exports").json() == []


def test_package_revision_change_between_report_and_export_is_refused(client, samples):
    pkg, _, report = _feasible_report(client, samples)
    r = client.post(
        f"/packages/{pkg['package_id']}/revisions",
        headers={"Expected-Revision": "1"},
        files={
            "xsd": ("acct.xsd", samples["xsd"], "application/xml"),
            "calculation": ("acct-calc.xml", samples["calc"], "application/xml"),
        },
    )
    assert r.status_code == 200
    resp = _export(client, report, RETAINED_WIDE,
                   [{"concept": "acct:Cash", "min_width": 1}])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "source_revision_changed"


def test_correction_committed_mid_generation_is_refused(client, samples, monkeypatch):
    """A correction landing while the CSV is assembled must abort publication."""
    _, inst, report = _feasible_report(client, samples)

    from app import sanitizer, storage
    real_build = sanitizer.build_csv
    state = {"done": False}

    def build_then_correct(*args, **kwargs):
        data = real_build(*args, **kwargs)
        if not state["done"]:
            state["done"] = True
            r = client.post(
                f"/instances/{inst['instance_id']}/revisions",
                headers={"Expected-Package-Revision": "1"},
                files={"instance": ("i.xml", samples["rounding"], "application/xml")},
            )
            assert r.status_code == 200, r.text
        return data

    monkeypatch.setattr(storage.sanitizer, "build_csv", build_then_correct)
    resp = _export(client, report, RETAINED_WIDE,
                   [{"concept": "acct:Cash", "min_width": 2}])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "source_revision_changed"
    # no partial artifact: neither metadata nor CSV is downloadable
    assert client.get("/exports").json() == []


def test_published_csv_is_immutable_across_corrections(client, samples):
    _, inst, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 2}])
    assert r.status_code == 201
    export = r.json()
    original = client.get(export["download_path"]).content

    # correct the instance and re-check: the already published export is frozen
    client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    assert check(client, inst["instance_id"], 2, 1).status_code == 200

    again = client.get(export["download_path"])
    assert again.status_code == 200
    assert again.content == original
    meta = client.get(f"/exports/{export['export_id']}").json()
    assert meta["sha256"] == export["sha256"]
    assert meta["instance_revision"] == 1
    # a new export must now be based on the new current report
    listing = client.get("/exports").json()
    assert [e["export_id"] for e in listing] == [export["export_id"]]


def test_exports_survive_restart(client, samples):
    _, _, report = _feasible_report(client, samples)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 2}])
    assert r.status_code == 201
    export = r.json()
    original = client.get(export["download_path"]).content

    from app import db
    db.reset_conn()
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c2:
        again = c2.get(export["download_path"])
        assert again.status_code == 200
        assert again.content == original
        assert c2.get("/exports").json()[0]["sha256"] == export["sha256"]


# ------------------------------------------------------------- solver details
def test_bounded_range_respects_two_equations(client, samples):
    """Receivables is pinned by CurrentAssets once its siblings are public."""
    _, _, report = _feasible_report(client, samples)
    retained = [c for c in ALL if c != "acct:Receivables"]
    r = _export(client, report, retained,
                [{"concept": "acct:Receivables", "min_width": 2.5}])
    assert r.status_code == 201, r.text
    recv = r.json()["ranges"]["acct:Receivables"]
    assert recv["width"] == pytest.approx(3.0)
    assert recv["unbounded"] is False


def test_exact_sensitive_fact_has_zero_width(client, samples):
    """A sensitive fact reported with decimals=INF is pinned exactly."""
    exact = samples["feasible"]
    for concept in ("Revenues", "Expenses", "NetIncome"):
        exact = exact.replace(
            f'<acct:{concept} contextRef="ctx1" unitRef="usd" decimals="0">'.encode(),
            f'<acct:{concept} contextRef="ctx1" unitRef="usd" decimals="INF">'.encode(),
        )
    _, _, report = _feasible_report(client, samples, xml=exact)
    retained = [c for c in ALL if c != "acct:NetIncome"]
    r = _export(client, report, retained,
                [{"concept": "acct:NetIncome", "min_width": 0.01}])
    assert r.status_code == 422
    assert r.json()["details"]["insufficient_concepts"] == ["acct:NetIncome"]


def test_sensitive_amount_never_appears_in_csv(client, samples):
    # Give Cash a distinctive amount, rebalance the totals so the filing stays
    # feasible, and keep Cash sensitive.
    xml = samples["feasible"]
    xml = xml.replace(
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">100</acct:Cash>',
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">137</acct:Cash>',
    ).replace(
        b'<acct:CurrentAssets contextRef="ctx1" unitRef="usd" decimals="0">200</acct:CurrentAssets>',
        b'<acct:CurrentAssets contextRef="ctx1" unitRef="usd" decimals="0">237</acct:CurrentAssets>',
    ).replace(
        b'<acct:Assets contextRef="ctx1" unitRef="usd" decimals="0">500</acct:Assets>',
        b'<acct:Assets contextRef="ctx1" unitRef="usd" decimals="0">537</acct:Assets>',
    )
    _, _, report = _feasible_report(client, samples, xml=xml)
    r = _export(client, report, RETAINED_WIDE,
                [{"concept": "acct:Cash", "min_width": 2}])
    assert r.status_code == 201, r.text
    data = client.get(r.json()["download_path"]).content
    assert "acct:Cash" not in data.decode()
    assert "137" not in data.decode()
    assert "136.5" not in data.decode() and "137.5" not in data.decode()
    # the publicly rebalanced totals are present
    by_concept = {row["concept"]: row for row in _csv_rows(data)}
    assert by_concept["acct:CurrentAssets"]["value"] == "237"
    assert by_concept["acct:Assets"]["value"] == "537"


def test_malformed_export_body_uses_error_envelope(client):
    r = client.post("/exports", json={"report_id": "rpt_x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"

    r = client.post("/exports", json={
        "report_id": "rpt_x", "expected_instance_revision": 1,
        "expected_package_revision": 1, "retained_concepts": ["a:b"],
        "sensitive_concepts": [{"concept": "a:c", "min_width": "wide"}],
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_docs_available_and_list_exports(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/exports" in paths and "/exports/{export_id}/csv" in paths
    assert client.get("/docs").status_code == 200
    assert client.get("/exports/nope").status_code == 404
    assert client.get("/exports/nope/csv").status_code == 404
