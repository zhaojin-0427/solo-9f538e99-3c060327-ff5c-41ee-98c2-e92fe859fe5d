"""Service / persistence operations with optimistic revision checks."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone

from . import db, sanitizer
from .errors import (NotFound, ReportNotCurrent, ReportNotFeasible,
                     SourceRevisionChanged, StaleRevision)
from .instance import parse_instance
from .solver import check_facts
from .taxonomy import parse_package

_publish_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------- packages
def create_package(name: str, xsd: bytes, calc: bytes) -> dict:
    info = parse_package(xsd, calc)
    conn = db.get_conn()
    pid = _new_id("pkg")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO packages(id, name, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (pid, name, now, now),
        )
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, 1, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
    return get_package(pid)


def revise_package(pid: str, expected_revision: int, xsd: bytes, calc: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT latest_rev FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    if row["latest_rev"] != expected_revision:
        raise StaleRevision(
            f"expected package revision {expected_revision}, server has {row['latest_rev']}"
        )
    info = parse_package(xsd, calc)
    new_rev = expected_revision + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, new_rev, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
        conn.execute(
            "UPDATE packages SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, pid),
        )
    return get_package(pid)


def _package_revision_row(pid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM package_revisions WHERE package_id=? AND revision=?",
        (pid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' revision {revision} not found")
    return dict(row)


def get_package(pid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    rev = _package_revision_row(pid, row["latest_rev"])
    return {
        "package_id": row["id"],
        "name": row["name"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "package_id": pid,
            "revision": rev["revision"],
            "target_ns": rev["target_ns"],
            "prefix": rev["prefix"],
            "concepts": json.loads(rev["concepts_json"]),
            "equations": json.loads(rev["equations_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_packages() -> list[dict]:
    rows = db.get_conn().execute("SELECT id FROM packages ORDER BY created_at").fetchall()
    return [get_package(r["id"]) for r in rows]


# ---------------------------------------------------------------- instances
def create_instance(pid: str, raw: bytes) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM packages WHERE id=?", (pid,)).fetchone() is None:
        raise NotFound(f"package '{pid}' not found")
    pkg = get_package(pid)
    info = parse_instance(raw, pkg["latest"])
    iid = _new_id("inst")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instances(id, package_id, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (iid, pid, now, now),
        )
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, 1, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
    return get_instance(iid)


def revise_instance(iid: str, expected_package_revision: int, raw: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT package_id, latest_rev FROM instances WHERE id=?", (iid,)
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    pkg = get_package(row["package_id"])
    # 受理时校验两侧所见修订：实例提交必须针对当前 package 修订。
    if pkg["latest_revision"] != expected_package_revision:
        raise StaleRevision(
            f"expected package revision {expected_package_revision}, "
            f"server has {pkg['latest_revision']}"
        )
    info = parse_instance(raw, pkg["latest"])
    new_rev = row["latest_rev"] + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, new_rev, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
        conn.execute(
            "UPDATE instances SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, iid),
        )
    return get_instance(iid)


def _instance_revision_row(iid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM instance_revisions WHERE instance_id=? AND revision=?",
        (iid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' revision {revision} not found")
    return dict(row)


def get_instance(iid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    rev = _instance_revision_row(iid, row["latest_rev"])
    return {
        "instance_id": row["id"],
        "package_id": row["package_id"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "instance_id": iid,
            "package_id": row["package_id"],
            "revision": rev["revision"],
            "entity": rev["entity"],
            "period": rev["period"],
            "currency": rev["currency"],
            "facts": json.loads(rev["facts_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_instances(pid: str | None = None) -> list[dict]:
    conn = db.get_conn()
    if pid:
        rows = conn.execute(
            "SELECT id FROM instances WHERE package_id=? ORDER BY created_at", (pid,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM instances ORDER BY created_at").fetchall()
    return [get_instance(r["id"]) for r in rows]


# ---------------------------------------------------------------- reports
def _report_out(row: dict) -> dict:
    return {
        "report_id": row["id"],
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "status": row["status"],
        "is_correction": bool(row["is_correction"]),
        "superseded": bool(row["superseded"]),
        "late": bool(row["late"]),
        "witness": json.loads(row["witness_json"]) if row["witness_json"] else None,
        "conflicts": json.loads(row["conflicts_json"]) if row["conflicts_json"] else None,
        "missing_facts": json.loads(row["missing_json"]) if row["missing_json"] else None,
        "detail": json.loads(row["detail_json"]),
        "created_at": row["created_at"],
        "has_instance_snapshot": bool(row["instance_snapshot"]),
        "has_xsd_snapshot": bool(row["xsd_snapshot"]),
        "has_calc_snapshot": bool(row["calc_snapshot"]),
    }


def run_check(iid: str, expected_instance_revision: int,
              expected_package_revision: int, late: bool = False) -> dict:
    conn = db.get_conn()
    inst_row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if inst_row is None:
        raise NotFound(f"instance '{iid}' not found")

    pkg = get_package(inst_row["package_id"])

    # 受理时校验两侧所见修订：
    # - 客户端所见修订领先于服务器 → 过期拒绝（412）
    # - 落后于服务器：默认仍拒绝；仅当显式声明为迟到补报时受理，仅存历史。
    if expected_instance_revision > inst_row["latest_rev"]:
        raise StaleRevision(
            f"expected instance revision {expected_instance_revision} is ahead of "
            f"server latest {inst_row['latest_rev']}"
        )
    if expected_package_revision > pkg["latest_revision"]:
        raise StaleRevision(
            f"expected package revision {expected_package_revision} is ahead of "
            f"server latest {pkg['latest_revision']}"
        )
    behind = (expected_instance_revision < inst_row["latest_rev"]
              or expected_package_revision < pkg["latest_revision"])
    if behind and not late:
        raise StaleRevision(
            f"revisions ({expected_instance_revision},"
            f"{expected_package_revision}) are behind server latest "
            f"({inst_row['latest_rev']},{pkg['latest_revision']}); "
            f"re-check with late=true to file a late historical report"
        )

    i_rev = _instance_revision_row(iid, expected_instance_revision)
    p_rev = _package_revision_row(inst_row["package_id"], expected_package_revision)

    # 迟到报告：依据的修订已落后于两侧最新组合，仅存历史，不取代当前报告。
    is_late = behind
    facts = json.loads(i_rev["facts_json"])
    equations = json.loads(p_rev["equations_json"])

    result = check_facts(facts, equations)

    now = _now()
    report_id = _new_id("rpt")
    prior = conn.execute(
        "SELECT id FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    is_correction = len(prior) > 0

    detail = {
        "entity": i_rev["entity"],
        "period": i_rev["period"],
        "currency": i_rev["currency"],
        "fact_count": len(facts),
        "equation_count": len(equations),
        "solver": "scipy.optimize.linprog(highs)",
    }
    payload = {
        "id": report_id,
        "instance_id": iid,
        "package_id": inst_row["package_id"],
        "instance_revision": i_rev["revision"],
        "package_revision": p_rev["revision"],
        "status": result["status"],
        "is_correction": 1 if is_correction else 0,
        "late": 1 if is_late else 0,
        "witness_json": json.dumps(result["witness"]) if "witness" in result else None,
        "conflicts_json": json.dumps(result["conflicts"]) if "conflicts" in result else None,
        "missing_json": json.dumps(result["missing_facts"]) if "missing_facts" in result else None,
        "detail_json": json.dumps(detail),
        "instance_snapshot": i_rev["instance_raw"],
        "xsd_snapshot": p_rev["xsd_raw"],
        "calc_snapshot": p_rev["calc_raw"],
        "created_at": now,
    }

    superseded_ids: list[str] = []
    with conn:
        conn.execute(
            "INSERT INTO reports(id, instance_id, package_id, instance_revision, "
            "package_revision, status, is_correction, superseded, late, witness_json, "
            "conflicts_json, missing_json, detail_json, instance_snapshot, xsd_snapshot, "
            "calc_snapshot, created_at) VALUES (:id,:instance_id,:package_id,"
            ":instance_revision,:package_revision,:status,:is_correction,0,:late,"
            ":witness_json,:conflicts_json,:missing_json,:detail_json,:instance_snapshot,"
            ":xsd_snapshot,:calc_snapshot,:created_at)",
            payload,
        )
        if not is_late:
            old = conn.execute(
                "SELECT id FROM reports WHERE instance_id=? AND id!=? AND superseded=0",
                (iid, report_id),
            ).fetchall()
            superseded_ids = [r["id"] for r in old]
            conn.execute(
                "UPDATE reports SET superseded=1 WHERE instance_id=? AND id!=?",
                (iid, report_id),
            )

    out = _report_out(dict(conn.execute(
        "SELECT * FROM reports WHERE id=?", (report_id,)
    ).fetchone()))
    return {"report": out, "superseded_report_ids": superseded_ids}


def get_report(rid: str) -> dict:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    return _report_out(dict(row))


def get_report_snapshot(rid: str, kind: str) -> tuple[bytes, str]:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    column = {"instance": "instance_snapshot", "xsd": "xsd_snapshot",
              "calc": "calc_snapshot"}[kind]
    media = {"instance": "application/xml", "xsd": "application/xml",
             "calc": "application/xml"}[kind]
    return bytes(row[column]), media


def list_reports(iid: str) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM instances WHERE id=?", (iid,)).fetchone() is None:
        raise NotFound(f"instance '{iid}' not found")
    rows = conn.execute(
        "SELECT * FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    history = [_report_out(dict(r)) for r in rows]
    # 迟到报告仅存历史，永远不作为当前报告。
    current = next(
        (r for r in reversed(history) if not r["superseded"] and not r["late"]), None
    )
    return {"instance_id": iid, "current": current, "history": history}


# ------------------------------------------------- sanitized data deliveries
def _export_out(row: dict) -> dict:
    internal_ranges = json.loads(row["widths_json"])
    # Public projection: a sensitive concept's reachable interval *position* is
    # itself an inference clue, so only its width and compliance are exposed.
    ranges = {
        concept: {
            "width": rng["width"],
            "min_width": rng["min_width"],
            "unbounded": rng["width"] is None,
            "satisfied": True,
        }
        for concept, rng in internal_ranges.items()
    }
    return {
        "export_id": row["id"],
        "report_id": row["report_id"],
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "retained_concepts": json.loads(row["retained_json"]),
        "sensitive_concepts": json.loads(row["sensitive_json"]),
        "ranges": ranges,
        "row_count": len(json.loads(row["retained_json"])),
        "sha256": row["csv_sha256"],
        "created_at": row["created_at"],
        "download_path": f"/exports/{row['id']}/csv",
    }


def _validated_report_source(rid: str) -> tuple[dict, dict, list[dict], list[dict], dict]:
    """Load a report and pin the exact revisions that produced it.

    Refuses reports that are not a usable feasible basis: non-feasible, late
    (historical only) or superseded by a newer report.
    """
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    report = dict(row)
    if report["status"] != "feasible":
        raise ReportNotFeasible(
            f"report '{rid}' has status '{report['status']}'; only a feasible report "
            f"can be sanitized for external delivery"
        )
    if report["superseded"] or report["late"]:
        raise ReportNotCurrent(
            f"report '{rid}' is not the current report "
            f"(superseded={bool(report['superseded'])}, late={bool(report['late'])})"
        )

    current = conn.execute(
        "SELECT r.id FROM reports r JOIN instances i ON i.id = r.instance_id "
        "WHERE r.instance_id=? AND r.superseded=0 AND r.late=0 "
        "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1",
        (report["instance_id"],),
    ).fetchone()
    if current is None or current["id"] != rid:
        raise ReportNotCurrent(f"report '{rid}' is not the current report")

    inst = conn.execute(
        "SELECT latest_rev FROM instances WHERE id=?", (report["instance_id"],)
    ).fetchone()
    pkg = conn.execute(
        "SELECT latest_rev FROM packages WHERE id=?", (report["package_id"],)
    ).fetchone()
    if inst is None or pkg is None:
        raise NotFound("source instance or package disappeared")
    if (report["instance_revision"] != inst["latest_rev"]
            or report["package_revision"] != pkg["latest_rev"]):
        raise SourceRevisionChanged(
            f"report '{rid}' was built from revisions "
            f"(instance {report['instance_revision']}, package {report['package_revision']}) "
            f"but the latest are (instance {inst['latest_rev']}, package {pkg['latest_rev']})"
        )

    i_rev = _instance_revision_row(report["instance_id"], report["instance_revision"])
    p_rev = _package_revision_row(report["package_id"], report["package_revision"])

    # Revisions are append-only, but verify the bytes the report actually pinned
    # are still the bytes on record before deriving anything from them.
    if (bytes(i_rev["instance_raw"]) != bytes(report["instance_snapshot"])
            or bytes(p_rev["xsd_raw"]) != bytes(report["xsd_snapshot"])
            or bytes(p_rev["calc_raw"]) != bytes(report["calc_snapshot"])):
        raise SourceRevisionChanged(
            f"the source bytes behind report '{rid}' no longer match the recorded revision"
        )

    facts = json.loads(i_rev["facts_json"])
    equations = json.loads(p_rev["equations_json"])
    pkg_info = {"prefix": p_rev["prefix"]}
    return report, i_rev, facts, equations, pkg_info


def _assert_publishable(conn, rid: str, report: dict) -> None:
    """Re-verify, against current DB state, that a report may still be published.

    Must run inside the publishing write transaction so a correction committed
    while the CSV was being assembled cannot slip past the check.
    """
    fresh = conn.execute(
        "SELECT superseded, late FROM reports WHERE id=?", (rid,)
    ).fetchone()
    if fresh is None:
        raise NotFound(f"report '{rid}' not found")
    if fresh["superseded"] or fresh["late"]:
        raise ReportNotCurrent(f"report '{rid}' was superseded during generation")
    latest_i = conn.execute(
        "SELECT latest_rev FROM instances WHERE id=?", (report["instance_id"],)
    ).fetchone()
    latest_p = conn.execute(
        "SELECT latest_rev FROM packages WHERE id=?", (report["package_id"],)
    ).fetchone()
    if (latest_i is None or latest_p is None
            or latest_i["latest_rev"] != report["instance_revision"]
            or latest_p["latest_rev"] != report["package_revision"]):
        raise SourceRevisionChanged(
            "a source revision changed while the sanitized CSV was being generated"
        )


def create_export(rid: str, expected_instance_revision: int,
                  expected_package_revision: int, retained: list[str],
                  sensitive: list[dict]) -> dict:
    """Validate the sanitization policy, then atomically publish a CSV.

    Any failure leaves no downloadable artifact behind: the CSV is computed in
    memory and the only durable write is a single INSERT inside one write
    transaction, whose preconditions are re-checked at commit time.
    """
    retained, sensitive = sanitizer.normalize_requirements(retained, sensitive)
    with _publish_lock:
        report, i_rev, facts, equations, pkg_info = _validated_report_source(rid)

        # The caller's view of both revisions must match what this report pinned.
        if (expected_instance_revision, expected_package_revision) != (
                report["instance_revision"], report["package_revision"]):
            raise StaleRevision(
                f"expected revisions ({expected_instance_revision},"
                f"{expected_package_revision}) do not match the report's pinned "
                f"revisions ({report['instance_revision']},{report['package_revision']})"
            )

        # Width gate: raise before writing anything.
        width_result = sanitizer.require_wide_enough(
            facts, equations, retained, sensitive
        )

        csv_bytes = sanitizer.build_csv(
            facts, retained, bytes(i_rev["instance_raw"]), pkg_info["prefix"]
        )

        export_id = _new_id("exp")
        now = _now()
        # The internal record keeps the full interval for audit; the public
        # response discloses only the achieved width, never where the hidden
        # amount sits (see _export_out).
        ranges = {
            concept: {"min": rng["min"], "max": rng["max"],
                      "width": rng["width"], "min_width": rng["min_width"],
                      "unbounded": rng["width"] is None}
            for concept, rng in width_result["ranges"].items()
        }
        payload = {
            "id": export_id,
            "report_id": rid,
            "instance_id": report["instance_id"],
            "package_id": report["package_id"],
            "instance_revision": report["instance_revision"],
            "package_revision": report["package_revision"],
            "retained_json": json.dumps(retained),
            "sensitive_json": json.dumps(sensitive),
            "widths_json": json.dumps(ranges),
            "csv_blob": csv_bytes,
            "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
            "created_at": now,
        }
        row = dict(payload)
        # BEGIN IMMEDIATE on a dedicated connection takes the SQLite write lock
        # before the final precondition check: either the revisions are unchanged
        # and the row lands, or the whole transaction rolls back and nothing
        # becomes downloadable.
        conn = db.new_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _assert_publishable(conn, rid, report)
                conn.execute(
                    "INSERT INTO exports(id, report_id, instance_id, package_id, "
                    "instance_revision, package_revision, retained_json, sensitive_json, "
                    "widths_json, csv_blob, csv_sha256, created_at) VALUES (:id,:report_id,"
                    ":instance_id,:package_id,:instance_revision,:package_revision,"
                    ":retained_json,:sensitive_json,:widths_json,:csv_blob,:csv_sha256,"
                    ":created_at)",
                    payload,
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        del row["csv_blob"]
        return _export_out(row)


def get_export(export_id: str) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM exports WHERE id=?", (export_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"export '{export_id}' not found")
    out = dict(row)
    del out["csv_blob"]
    return _export_out(out)


def get_export_csv(export_id: str) -> tuple[bytes, str]:
    row = db.get_conn().execute(
        "SELECT csv_blob, csv_sha256 FROM exports WHERE id=?", (export_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"export '{export_id}' not found")
    data = bytes(row["csv_blob"])
    if hashlib.sha256(data).hexdigest() != row["csv_sha256"]:
        from .errors import ApiError
        raise ApiError(500, "export_corrupt", "stored CSV failed its integrity check")
    return data, "text/csv; charset=utf-8"


def list_exports(instance_id: str | None = None) -> list[dict]:
    conn = db.get_conn()
    if instance_id:
        rows = conn.execute(
            "SELECT * FROM exports WHERE instance_id=? ORDER BY created_at, rowid",
            (instance_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM exports ORDER BY created_at, rowid"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        del item["csv_blob"]
        out.append(_export_out(item))
    return out
