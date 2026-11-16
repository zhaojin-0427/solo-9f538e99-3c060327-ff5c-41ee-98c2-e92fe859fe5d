"""Sanitized data delivery for external research libraries.

The constructor of this module never sees the "current" database state: it is
handed one immutable report (its pinned instance revision, package revision and
raw snapshots).  Only retained (public) concepts keep their ``decimals``
rounding interval; every other fact becomes a free variable.  Each sensitive
concept is then projected out of the feasible polyhedron to obtain its
reachable range.  An unbounded direction counts as an infinitely wide range;
a finite range narrower than the requested minimum width blocks publication.
"""
from __future__ import annotations

import csv
import io
import math
from decimal import Decimal, InvalidOperation
from typing import Optional

import numpy as np
from lxml import etree
from scipy.optimize import linprog

from . import config
from .errors import ApiError, BadPayload, InsufficientInferenceWidth


# ---------------------------------------------------------------- validation
def normalize_requirements(retained: list[str], sensitive) -> tuple[list[str], list[dict]]:
    """Validate and de-duplicate the requested public / sensitive concepts."""
    if not isinstance(retained, list) or any(not isinstance(c, str) for c in retained):
        raise BadPayload("retained_concepts must be a list of concept QNames")

    if isinstance(sensitive, dict):  # accept {"acct:Inventory": 5.0} as a convenience
        sensitive = [{"concept": c, "min_width": w} for c, w in sensitive.items()]
    if not isinstance(sensitive, list):
        raise BadPayload("sensitive_concepts must be a list of {concept, min_width}")

    retained_out: list[str] = []
    for concept in retained:
        concept = concept.strip()
        if concept not in retained_out:
            retained_out.append(concept)

    sensitive_out: list[dict] = []
    seen: set[str] = set()
    for item in sensitive:
        if isinstance(item, dict):
            concept = item.get("concept")
            width = item.get("min_width")
        else:
            concept = getattr(item, "concept", None)
            width = getattr(item, "min_width", None)
        if not isinstance(concept, str) or not concept.strip():
            raise BadPayload("every sensitive concept needs a non-empty concept QName")
        concept = concept.strip()
        if concept in seen:
            raise BadPayload(f"duplicate sensitive concept '{concept}'")
        seen.add(concept)
        if isinstance(width, bool) or not isinstance(width, (int, float)):
            raise BadPayload(f"min_width for '{concept}' must be a positive number")
        width = float(width)
        if not np.isfinite(width) or width <= 0:
            raise BadPayload(f"min_width for '{concept}' must be a positive finite number")
        sensitive_out.append({"concept": concept, "min_width": width})

    overlap = sorted(set(retained_out) & seen)
    if overlap:
        raise BadPayload(
            "a concept cannot be both retained (public) and sensitive: "
            + ", ".join(overlap)
        )
    if not sensitive_out:
        raise BadPayload("at least one sensitive concept is required")
    return retained_out, sensitive_out


# ---------------------------------------------------------------- reachability
def _range(concepts: list[str], retained: set[str], facts_by_concept: dict,
           equations: list[dict], target: str) -> dict:
    """Compute the reachable [min, max] of ``target`` under all equations.

    ``None`` means that direction is unbounded.
    """
    n = len(concepts)
    idx = {c: i for i, c in enumerate(concepts)}
    A_eq = np.zeros((len(equations), n))
    b_eq = np.zeros(len(equations))
    for r, eq in enumerate(equations):
        for concept, coef in eq["coefficients"].items():
            if concept not in idx:
                raise ApiError(
                    500, "solver_error",
                    f"equation references unknown fact '{concept}'",
                )
            A_eq[r, idx[concept]] = coef

    bounds: list[tuple[Optional[float], Optional[float]]] = []
    for concept in concepts:
        if concept in retained:
            f = facts_by_concept[concept]
            lo, hi = f["lower"], f["upper"]
            if lo > hi:
                lo, hi = hi, lo
            bounds.append((float(lo), float(hi)))
        else:
            # Every non-public fact is unconstrained: no amount boundary may be
            # revealed (directly or transitively) through the published data.
            bounds.append((None, None))

    def direction(sign: float) -> Optional[float]:
        c = np.zeros(n)
        c[idx[target]] = sign
        res = linprog(c, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
        if res.status == 3:  # unbounded in this direction
            return None
        if res.success:
            value = float(res.fun) * sign
            return 0.0 if abs(value) < config.WITNESS_TOL else value
        if res.status == 2:
            # The pinned report was feasible and removing constraints cannot
            # make the system infeasible; this signals a corrupt snapshot.
            raise ApiError(500, "solver_error", "sanitized feasibility system is infeasible")
        raise ApiError(500, "solver_error", f"range solver failed: {res.message}")

    return {"min": direction(1.0), "max": direction(-1.0)}


def evaluate_widths(facts: list[dict], equations: list[dict], retained: list[str],
                    sensitive: list[dict]) -> dict:
    """Return per-sensitive ranges and the list of concepts that fail the width."""
    concepts = sorted(f["concept"] for f in facts)
    facts_by_concept = {f["concept"]: f for f in facts}
    known = set(concepts)

    unknown = [c for c in retained if c not in known]
    unknown += [s["concept"] for s in sensitive if s["concept"] not in known]
    if unknown:
        raise BadPayload(
            "concepts are not facts of this report: " + ", ".join(sorted(set(unknown)))
        )
    if not facts:
        raise BadPayload("instance revision contains no facts")

    retained_set = set(retained)
    ranges: dict[str, dict] = {}
    insufficient: list[str] = []
    for req in sensitive:
        concept = req["concept"]
        rng = _range(concepts, retained_set, facts_by_concept, equations, concept)
        width: Optional[float]
        if rng["min"] is None or rng["max"] is None:
            width = None  # unbounded counts as satisfying any required width
        else:
            width = max(0.0, rng["max"] - rng["min"])
        rng["width"] = width
        rng["min_width"] = req["min_width"]
        if width is not None and width + 1e-9 < req["min_width"]:
            insufficient.append(concept)
        ranges[concept] = rng

    return {"ranges": ranges, "insufficient": sorted(insufficient)}


def require_wide_enough(facts: list[dict], equations: list[dict], retained: list[str],
                        sensitive: list[dict]) -> dict:
    result = evaluate_widths(facts, equations, retained, sensitive)
    if result["insufficient"]:
        raise InsufficientInferenceWidth(result["insufficient"])
    return result


# ---------------------------------------------------------------- CSV export
def _plain_number(text: str) -> str:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if not value.is_finite():
        return text
    return format(value, "f")


def _lexical_values(snapshot: bytes, prefix: str,
                    wanted: set[str]) -> dict[str, tuple[str, str]]:
    """Extract ``concept -> (lexical value, lexical decimals)`` from a snapshot.

    Only ``wanted`` (retained) concepts are read out of the snapshot; every
    other fact is skipped on the spot, so sensitive amounts never enter the
    delivery path at all.
    """
    try:
        root = etree.fromstring(snapshot)
    except etree.XMLSyntaxError:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for el in root:
        if not isinstance(el.tag, str) or el.get("contextRef") is None:
            continue
        qname = etree.QName(el)
        concept = f"{prefix}:{qname.localname}"
        if concept not in wanted:
            continue
        text = (el.text or "").strip()
        decimals = (el.get("decimals") or "").strip() or "INF"
        if text:
            out[concept] = (text, decimals)
    return out


def build_csv(facts: list[dict], retained: list[str], snapshot: bytes,
              prefix: str) -> bytes:
    """Build the public CSV: retained concepts only, report value and decimals."""
    by_concept = {f["concept"]: f for f in facts}
    lexical = _lexical_values(snapshot, prefix, set(retained))

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["concept", "value", "decimals"])
    for concept in retained:
        fact = by_concept[concept]
        text, decimals = lexical.get(
            concept, (str(fact["value"]), str(fact["decimals"]))
        )
        # Guard against a snapshot/parse mismatch: the CSV must carry the value
        # recorded in the pinned report, never a value rescued from elsewhere.
        try:
            agrees = math.isclose(
                float(Decimal(text)), float(fact["value"]),
                rel_tol=1e-12, abs_tol=1e-9,
            )
        except (InvalidOperation, ValueError):
            agrees = False
        if not agrees:
            text, decimals = str(fact["value"]), str(fact["decimals"])
        writer.writerow([concept, _plain_number(text), decimals])
    return buf.getvalue().encode("utf-8")
