from __future__ import annotations

import logging
import sys
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)
if not logger.handlers:
    # Make sure diagnostics are visible even if the caller hasn't
    # configured logging (competition harnesses often just capture
    # stdout/stderr). Safe to remove once the tool-name/field-name
    # assumptions below are confirmed against a real run.
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[workflow] %(levelname)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)


# ----------------------------------------------------------------------
# NOTE ON ASSUMPTIONS
# ----------------------------------------------------------------------
# We were given the real MCP tool names (get_order, get_order_items,
# get_order_payments, get_payment_timeline, get_refund_timeline,
# get_shipment_summary, get_sellers, get_product_context, get_policy,
# get_customer_history) and the envelope schema (evidence_ref, domain,
# result_hash, data, warnings). We were NOT given the schema of what sits
# inside `data` for each tool. Every place below that reads a specific
# key out of `data` is therefore a best-effort guess, flagged with
# `# ASSUMPTION`. Before trusting this in grading, dump one real
# `evidence["data"]` per tool and adjust the key names if they differ.
# ----------------------------------------------------------------------

# Primary issues this workflow is actually able to produce. Kept as an
# explicit allow-list so we NEVER emit an arbitrary topic string into
# `assessment.primary_issue` — l3a's $defs/primaryIssue is very likely a
# closed enum, and one bad value there fails the whole output schema.
_KNOWN_PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "payment_mismatch",
    "duplicate_charge",
    "refund_failed",
    "refund_pending",
    "valid_split_payment",
    "unsupported_claim",
    "insufficient_evidence",
}


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3B multi-agent workflow — Coordinator routes to Order/Item, Payment and
    Shipment specialists, then Policy Agent then Verifier Agent.

    Architecture:

        Coordinator / Router
          ├── Order/Item Agent
          ├── Payment Agent
          └── Shipment Agent
                 │
                 ▼
           MCP Evidence Collector
                 │
                 ▼
           Policy Agent
                 │
                 ▼
           Verifier Agent
                 │
                 ▼
             [END OUTPUT]
    """
    case_id: str = case["case_id"]
    claims: list[dict[str, str]] = case["customer_request"]["claims"]
    candidates: list[str] = case.get("candidate_order_ids", [])
    claimed_order: str = case["customer_request"]["claimed_order_id"]
    customer_hint: str | None = case.get("customer_unique_id_hint")
    scope = case.get("investigation_scope", {})

    # ==================================================================
    # LIFECYCLE: case_received — must be first event emitted for a case
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
        decision_code="case_received",
    )

    # ------------------------------------------------------------------
    # Discover MCP tools once
    # ------------------------------------------------------------------
    tools = await gateway.list_tools()
    tool_set = set(tools)
    logger.warning("case %s: gateway.list_tools() returned: %s", case_id, sorted(tool_set))

    collected: dict[str, dict[str, Any]] = {}
    ev_tool_map: dict[str, str] = {}
    failed_calls: list[dict[str, str]] = []  # local debugging aid only, not emitted to trace

    async def mcpcall(
        tool_name: str, *, case_id: str = case_id, actor: str = "coordinator", **kwargs: str
    ) -> dict[str, Any] | None:
        """Safe one-shot MCP call with trace emission.

        Only emits `tool_result_consumed` on success (matches the closed
        trace-event-v1 enum — there is no "tool_failed" event type
        available, so failures are intentionally not fabricated into the
        trace; the MCP server keeps its own independent audit of
        hash/latency/status for those). Failures are still tracked
        locally in `failed_calls` (and logged) so a case that comes back
        thin — like zero evidence_refs — is easy to diagnose instead of
        silently producing an empty, low-confidence output.
        """
        if tool_name not in tool_set:
            failed_calls.append({"tool": tool_name, "reason": "not_in_tool_set"})
            logger.warning(
                "case %s: tool %r not in gateway.list_tools() (%d tools available) — "
                "check for a naming/prefix mismatch between the name used here and "
                "what the gateway actually exposes",
                case_id, tool_name, len(tool_set),
            )
            return None
        try:
            result = await gateway.call(tool_name, case_id=case_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see note above
            failed_calls.append({"tool": tool_name, "reason": str(exc)})
            logger.warning(
                "case %s: gateway.call(%r, case_id=%r, %s) raised: %s",
                case_id, tool_name, case_id, kwargs, exc,
            )
            return None

        ref = result["evidence_ref"]
        collected[ref] = result
        ev_tool_map[ref] = tool_name
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref],
        )
        return result

    # ==================================================================
    # COORDINATOR / ROUTER — entity resolution + customer context
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-resolver",
        decision_code="entity_resolution",
    )

    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []
    entity_confidence: float = 0.3

    for cid in candidates:
        resp = await mcpcall("get_order", order_id=cid)
        if resp is not None:
            data = resp.get("data", {})
            if isinstance(data, dict) and data.get("order_status"):
                resolved_order_ids.append(cid)
            else:
                rejected_candidates.append(cid)
        else:
            rejected_candidates.append(cid)

    if claimed_order not in resolved_order_ids:
        resp = await mcpcall("get_order", order_id=claimed_order)
        if resp is not None:
            resolved_order_ids.append(claimed_order)
        elif claimed_order not in candidates:
            rejected_candidates.append(claimed_order)

    all_order_ids = list(resolved_order_ids) if resolved_order_ids else [claimed_order]

    if len(resolved_order_ids) == 1 and resolved_order_ids[0] == claimed_order:
        entity_status = "resolved"
        entity_confidence = 0.90
    elif resolved_order_ids:
        entity_status = "resolved"
        entity_confidence = 0.65
    else:
        # Schema allows "not_found" too (not just "ambiguous") — use it
        # when nothing at all resolved instead of mislabeling as ambiguous.
        entity_status = "ambiguous" if candidates else "not_found"
        entity_confidence = 0.25

    # Customer context (coordinator collects before routing)
    customer_unique_id: str | None = customer_hint
    related_order_ids: list[str] = []

    if customer_hint:
        cust_resp = await mcpcall("get_customer_history", customer_unique_id=customer_hint)
        if cust_resp is not None:
            cust_data = cust_resp.get("data", {})
            if isinstance(cust_data, dict):
                related = cust_data.get("order_ids", [])  # ASSUMPTION
                if isinstance(related, list):
                    related_order_ids = [o for o in related if o not in all_order_ids][:20]

    # ==================================================================
    # ORDER / ITEM AGENT
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="order-item-agent",
        decision_code=f"entity_{entity_status}",
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-item-agent",
        decision_code="order_item_investigation",
    )

    item_ids: list[str] = []
    seller_ids: list[str] = []

    for oid in all_order_ids:
        items_resp = await mcpcall("get_order_items", order_id=oid, actor="order-item-agent")
        if items_resp is not None:
            data = items_resp.get("data", {})
            if isinstance(data, dict):
                items = data.get("items", data.get("order_items", []))  # ASSUMPTION
                if isinstance(items, list):
                    for it in items:
                        iid = it.get("item_id") or it.get("product_id")
                        if iid and iid not in item_ids:
                            item_ids.append(iid)
                        sid = it.get("seller_id")
                        if sid and sid not in seller_ids:
                            seller_ids.append(sid)

        if scope.get("include_product_context", False):
            await mcpcall("get_product_context", order_id=oid, actor="order-item-agent")

    # Validate/enrich sellers found via the authoritative seller tool
    # rather than trusting whatever seller_id happened to sit on an item
    # row. Anything that fails to resolve is dropped from seller_ids so
    # `affected_entities.seller_ids` only ever contains gateway-confirmed
    # sellers.
    confirmed_seller_ids: list[str] = []
    for sid in seller_ids:
        seller_resp = await mcpcall("get_sellers", seller_id=sid, actor="order-item-agent")
        if seller_resp is not None:
            confirmed_seller_ids.append(sid)
    seller_ids = confirmed_seller_ids

    # ==================================================================
    # PAYMENT AGENT
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="payment-agent",
        decision_code="order_item_done",
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
        decision_code="payment_investigation",
    )

    payment_refs: list[str] = []
    payment_verdict: str = "insufficient_evidence"
    captured_total: float | None = None
    refunded_total: float | None = None
    refundable_total: float | None = None

    for oid in all_order_ids:
        pay_resp = await mcpcall("get_order_payments", order_id=oid, actor="payment-agent")
        if pay_resp is not None:
            data = pay_resp.get("data", {})
            if isinstance(data, dict):
                pref = data.get("payment_reference") or data.get("payment_id")  # ASSUMPTION
                if pref and pref not in payment_refs:
                    payment_refs.append(pref)
                val = data.get("payment_value") or data.get("captured_amount")  # ASSUMPTION
                try:
                    captured_total = float(val) if val is not None else captured_total
                except (TypeError, ValueError):
                    pass
                payment_verdict = "reconciled"

        # get_payment_timeline: the actual source for mismatch / duplicate
        # detection, since a single order-payment snapshot can't show
        # multiple capture attempts over time. ASSUMPTION: timeline data
        # exposes a list of capture-like events under "events" or
        # "timeline", each with "event_type"/"type", "status", and
        # "amount"; and optionally an "order_total"/"expected_amount" to
        # compare against what was actually captured.
        timeline_resp = await mcpcall("get_payment_timeline", order_id=oid, actor="payment-agent")
        if timeline_resp is not None:
            data = timeline_resp.get("data", {})
            if isinstance(data, dict):
                events = data.get("events") or data.get("timeline") or []
                if isinstance(events, list):
                    capture_events = [
                        e for e in events
                        if isinstance(e, dict)
                        and (e.get("event_type") or e.get("type") or "").lower()
                        in ("capture", "captured", "charge")
                    ]
                    successful = [
                        e for e in capture_events
                        if (e.get("status") or "").lower()
                        in ("success", "succeeded", "completed", "captured")
                    ]
                    if len(successful) > 1:
                        amounts = [e.get("amount") for e in successful if e.get("amount") is not None]
                        if amounts and len(amounts) != len(set(amounts)):
                            payment_verdict = "duplicate_capture"

                    expected = data.get("order_total") or data.get("expected_amount")
                    if (
                        expected is not None
                        and captured_total is not None
                        and payment_verdict != "duplicate_capture"
                    ):
                        try:
                            if abs(float(expected) - captured_total) > 0.01:
                                payment_verdict = "capture_mismatch"
                        except (TypeError, ValueError):
                            pass

        ref_resp = await mcpcall("get_refund_timeline", order_id=oid, actor="payment-agent")
        if ref_resp is not None:
            data = ref_resp.get("data", {})
            if isinstance(data, dict):
                try:
                    rfd = data.get("refunded_amount") or data.get("total_refunded")  # ASSUMPTION
                    if rfd is not None:
                        refunded_total = float(rfd)
                except (TypeError, ValueError):
                    pass
                try:
                    rfa = data.get("refundable_amount") or data.get("total_refundable")  # ASSUMPTION
                    if rfa is not None:
                        refundable_total = float(rfa)
                except (TypeError, ValueError):
                    pass
                rstatus = (data.get("refund_status") or "").lower()
                # Duplicate/mismatch findings from the payment timeline take
                # priority over a plain refund-status read, since they are
                # more specific evidence of a payment-processing problem.
                if payment_verdict not in ("duplicate_capture", "capture_mismatch"):
                    if rstatus == "completed" and (refunded_total or 0) > 0:
                        payment_verdict = "refunded"
                    elif rstatus in ("pending", "processing"):
                        payment_verdict = "refund_pending"
                    elif rstatus in ("failed", "rejected"):
                        payment_verdict = "refund_failed"

    # ==================================================================
    # SHIPMENT AGENT
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="shipment-agent",
        decision_code="payment_done",
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
        decision_code="shipment_investigation",
    )

    shipment_ids: list[str] = []
    shipment_verdict: str = "insufficient_evidence"
    late_seller_ids: list[str] = []
    timeline_complete: bool = False
    _distinct_shipment_verdicts: set[str] = set()

    for oid in all_order_ids:
        ship_resp = await mcpcall("get_shipment_summary", order_id=oid, actor="shipment-agent")
        if ship_resp is not None:
            data = ship_resp.get("data", {})
            if isinstance(data, dict):
                shipments_raw = data.get("shipments", [data])  # ASSUMPTION
                if isinstance(shipments_raw, list):
                    for s in shipments_raw:
                        sid = s.get("shipment_id") or data.get("shipment_id")
                        if sid and sid not in shipment_ids:
                            shipment_ids.append(sid)

                        status = (s.get("delivery_status") or "").lower()
                        # ASSUMPTION: shipment_summary exposes which party is
                        # responsible for a delay via "delay_owner" or
                        # "responsible_party" (e.g. "seller" vs
                        # "logistics"/"carrier"). Falls back to treating any
                        # generic "late"/"delayed" as seller_delay if no
                        # owner field is present, same as before.
                        delay_owner = (s.get("delay_owner") or s.get("responsible_party") or "").lower()

                        this_verdict: str | None = None
                        if status in ("logistics_delay",) or (
                            status in ("late", "delayed") and delay_owner in ("logistics", "carrier", "logistics_provider")
                        ):
                            this_verdict = "logistics_delay"
                        elif status in ("seller_delay", "late", "delayed"):
                            this_verdict = "seller_delay"
                            ls = s.get("seller_id") or data.get("seller_id", "")
                            if ls and ls not in late_seller_ids:
                                late_seller_ids.append(ls)
                        elif status == "lost":
                            this_verdict = "lost"
                        elif status == "returned":
                            this_verdict = "returned"
                        elif status in ("on_time", "delivered"):
                            this_verdict = "on_time"

                        if this_verdict:
                            _distinct_shipment_verdicts.add(this_verdict)
                            if shipment_verdict == "insufficient_evidence":
                                shipment_verdict = this_verdict

                    timeline_complete = len(shipments_raw) > 0

    # Multiple shipments under the same order disagreeing (e.g. one
    # on_time, one seller_delay) is a real conflicting-evidence state the
    # schema has a slot for — surface it instead of silently keeping
    # whichever verdict happened to be seen first.
    _meaningful_verdicts = _distinct_shipment_verdicts - {"insufficient_evidence"}
    if len(_meaningful_verdicts) > 1:
        shipment_verdict = "conflicting"

    # ==================================================================
    # MCP EVIDENCE COLLECTOR — all evidence gathered, ready for analysis
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="policy-agent",
        decision_code="shipment_done",
    )

    # ==================================================================
    # POLICY AGENT
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        decision_code="policy_conflict_analysis",
    )

    policy_version = case.get("policy_version", "EC_POLICY_V2")
    await mcpcall("get_policy", policy_version=policy_version, actor="policy-agent")

    topic_set = {c["topic"] for c in claims}
    primary_issue = _resolve_primary_issue(topic_set, shipment_verdict, payment_verdict)
    data_conflicts = _detect_conflicts(collected)
    claim_assessments = _assess_claims(claims, shipment_verdict, payment_verdict, ev_tool_map)

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=f"primary_issue_{primary_issue}",
        attributes={"conflict_count": len(data_conflicts)},
    )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier",
        decision_code="policy_resolved",
    )

    # ==================================================================
    # VERIFIER AGENT
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier",
        decision_code="verification",
    )

    case_status = _infer_case_status(primary_issue, payment_verdict)
    ranked_causes, responsible_parties = _root_cause(primary_issue, seller_ids, shipment_verdict)

    # Cross-field consistency check: if the root cause says the seller is
    # responsible, no non-seller party should be sitting in
    # responsible_parties, and vice versa. This is the actual verifier
    # step Pha 4 asks for, not just recomputing numbers.
    responsible_parties = _cross_check_responsible_parties(
        primary_issue, responsible_parties, shipment_verdict
    )

    all_evidence_refs = list(collected.keys())
    financial_resolution = _financial_resolution(
        case_status, primary_issue, captured_total, refunded_total,
    )
    resolution_actions = _resolution_actions(
        case_status, primary_issue, payment_verdict, seller_ids,
    )

    if not collected:
        logger.warning(
            "case %s: zero MCP calls succeeded (%d attempted, all failed: %s) — "
            "output will be all insufficient_evidence with no evidence_refs. "
            "This is almost certainly a tool-name/argument mismatch, not missing "
            "case data — check the [workflow] WARNING lines above for the exact "
            "tool names and exceptions.",
            case_id, len(failed_calls), failed_calls,
        )

    claim_avg_conf = (
        sum(c["confidence"] for c in claim_assessments) / max(len(claim_assessments), 1)
    )
    overall_confidence = (
        0.40 * entity_confidence
        + 0.25 * claim_avg_conf
        + 0.20 * min(1.0, len(all_evidence_refs) / 5)
        + 0.15 * (0.9 if entity_status == "resolved" else 0.3)
    )
    # Calibration: never let confidence stay high when the evidence
    # itself disagrees with itself. Each conflicting field knocks
    # confidence down, capped so a single conflict doesn't zero it out.
    conflict_penalty = min(0.30, 0.08 * len(data_conflicts))
    overall_confidence = round(max(0.0, min(1.0, overall_confidence - conflict_penalty)), 2)

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [t for t in topic_set if t != primary_issue][:10],
            "case_status": case_status,
            "confidence": overall_confidence,
        },
        "affected_entities": {
            "order_ids": all_order_ids[:20],
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_refs[:20],
            "shipment_ids": shipment_ids[:20],
        },
        "claim_assessments": claim_assessments[:5],
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved_order_ids[:20],
            "rejected_candidates": rejected_candidates[:20],
            "confidence": entity_confidence,
        },
        "customer_context": {
            "customer_unique_id": customer_unique_id,
            "related_order_ids": related_order_ids[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_seller_ids[:20],
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured_total,
            "refunded_total_brl": refunded_total,
            "refundable_total_brl": refundable_total,
        },
        "root_cause_analysis": {
            "ranked_causes": ranked_causes[:5],
            "responsible_parties": responsible_parties[:5],
        },
        "evidence_refs": all_evidence_refs[:30],
        "data_conflicts": data_conflicts[:5],
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions[:8],
    }

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=f"confidence_{overall_confidence:.2f}",
        attributes={
            "evidence_count": len(all_evidence_refs),
            "order_count": len(all_order_ids),
            "conflict_count": len(data_conflicts),
            "failed_call_count": len(failed_calls),
        },
    )

    # ==================================================================
    # LIFECYCLE: case_finalized — must be the last event for this case
    # ==================================================================
    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="verifier",
        decision_code=f"case_status_{case_status}",
    )

    return output


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def _resolve_primary_issue(
    topics: set[str], shipment_verdict: str, payment_verdict: str
) -> str:
    priority = [
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "payment_mismatch",
        "duplicate_charge",
        "refund_failed",
        "refund_pending",
        "valid_split_payment",
    ]
    for issue in priority:
        if issue in topics:
            return issue

    if "requested_full_refund" in topics:
        if shipment_verdict == "seller_delay":
            return "late_delivery_seller"
        if shipment_verdict == "logistics_delay":
            return "late_delivery_logistics"
        if shipment_verdict in ("lost", "returned"):
            return "late_delivery_seller"
        if payment_verdict == "refund_pending":
            return "refund_pending"
        if payment_verdict == "refund_failed":
            return "refund_failed"

    # Only ever fall back to a topic string if it is itself a value the
    # output schema is known to accept for primary_issue. Anything else
    # (an unrecognized claim topic) becomes "insufficient_evidence"
    # rather than risking a schema-breaking arbitrary value.
    for topic in topics:
        if topic in _KNOWN_PRIMARY_ISSUES:
            return topic

    return "insufficient_evidence"


def _infer_case_status(primary_issue: str, payment_verdict: str) -> str:
    if primary_issue in ("unsupported_claim", "valid_split_payment"):
        return "no_action"
    if primary_issue in ("insufficient_evidence",):
        return "needs_investigation"
    if primary_issue == "refund_pending" and payment_verdict in ("refunded",):
        return "no_action"
    return "action_required"


def _assess_claims(
    claims: list[dict],
    shipment_verdict: str,
    payment_verdict: str,
    ev_tool_map: dict[str, str],
) -> list[dict]:
    results: list[dict] = []
    domain_refs: dict[str, list[str]] = {}
    for ref, tool in ev_tool_map.items():
        domain_refs.setdefault(tool, []).append(ref)

    for claim in claims:
        cid = claim["claim_id"]
        topic = claim["topic"]
        verdict: str
        conf: float

        if not ev_tool_map:
            verdict, conf = "insufficient_evidence", 0.15
        elif topic in ("late_delivery_seller",):
            if shipment_verdict == "seller_delay":
                verdict, conf = "supported", 0.85
            elif shipment_verdict in ("logistics_delay",):
                verdict, conf = "unsupported", 0.70
            elif shipment_verdict == "lost":
                verdict, conf = "supported", 0.60
            elif shipment_verdict == "conflicting":
                verdict, conf = "partially_supported", 0.40
            else:
                verdict, conf = "insufficient_evidence", 0.35
        elif topic in ("late_delivery_logistics",):
            if shipment_verdict == "logistics_delay":
                verdict, conf = "supported", 0.85
            elif shipment_verdict == "seller_delay":
                verdict, conf = "unsupported", 0.70
            elif shipment_verdict == "conflicting":
                verdict, conf = "partially_supported", 0.40
            else:
                verdict, conf = "insufficient_evidence", 0.35
        elif topic == "valid_split_payment":
            if payment_verdict == "reconciled":
                verdict, conf = "supported", 0.80
            else:
                verdict, conf = "insufficient_evidence", 0.35
        elif topic == "payment_mismatch":
            if payment_verdict == "capture_mismatch":
                verdict, conf = "supported", 0.85
            else:
                verdict, conf = "insufficient_evidence", 0.30
        elif topic == "duplicate_charge":
            if payment_verdict == "duplicate_capture":
                verdict, conf = "supported", 0.85
            else:
                verdict, conf = "insufficient_evidence", 0.30
        elif topic in ("refund_pending", "refund_failed"):
            if payment_verdict == "refund_pending":
                verdict, conf = "supported", 0.80
            elif payment_verdict == "refund_failed":
                verdict, conf = "supported", 0.85
            else:
                verdict, conf = "insufficient_evidence", 0.30
        elif topic == "requested_full_refund":
            if payment_verdict == "refunded" and shipment_verdict in (
                "seller_delay", "logistics_delay", "lost", "returned"
            ):
                verdict, conf = "supported", 0.90
            elif payment_verdict == "refunded":
                verdict, conf = "supported", 0.80
            elif payment_verdict == "refund_pending":
                verdict, conf = "partially_supported", 0.60
            elif shipment_verdict in ("seller_delay", "logistics_delay", "lost", "returned"):
                verdict, conf = "partially_supported", 0.55
            else:
                verdict, conf = "insufficient_evidence", 0.30
        else:
            verdict, conf = "insufficient_evidence", 0.20

        claim_refs: list[str] = []
        if "seller" in topic or "delivery" in topic:
            claim_refs = domain_refs.get("get_shipment_summary", [])
        elif "payment" in topic or "refund" in topic or "charge" in topic:
            claim_refs = (
                domain_refs.get("get_order_payments", [])
                + domain_refs.get("get_payment_timeline", [])
                + domain_refs.get("get_refund_timeline", [])
            )
        elif "order" in topic:
            claim_refs = domain_refs.get("get_order", [])
        claim_refs = claim_refs[:30]

        results.append({
            "claim_id": cid,
            "verdict": verdict,
            "confidence": conf,
            "evidence_refs": claim_refs,
        })

    return results


def _detect_conflicts(collected: dict[str, dict[str, Any]]) -> list[dict]:
    conflicts: list[dict] = []
    groups: dict[str, list[str]] = {}
    for ref, ev in collected.items():
        # `domain` is guaranteed present & schema-valid here because
        # gateway.call() already ran validate_evidence() before this
        # dict was populated — no need for a fallback default.
        dom = ev["domain"]
        groups.setdefault(dom, []).append(ref)

    conflict_fields = [
        "order_status", "delivery_status", "payment_value",
        "payment_status", "refund_status", "order_id",
    ]
    for domain, refs in groups.items():
        if len(refs) < 2:
            continue
        data_list = [collected[r].get("data", {}) for r in refs]
        data_list = [d for d in data_list if isinstance(d, dict)]
        if len(data_list) < 2:
            continue
        for field in conflict_fields:
            vals: set[str] = set()
            for d in data_list:
                if field in d:
                    vals.add(str(d[field]))
            if len(vals) > 1:
                conflicts.append({
                    "field": f"{domain}.{field}",
                    "sources": refs[:5],
                    "selected_source": refs[0],
                    "resolution_code": "latest_source_preferred",
                })

    return conflicts


def _root_cause(
    primary_issue: str,
    seller_ids: list[str],
    shipment_verdict: str,
) -> tuple[list[dict], list[dict]]:
    causes: list[dict] = []
    parties: list[dict] = []

    cause_map: dict[str, tuple[str, str]] = {
        "late_delivery_seller": ("SELLER_SHIPPING_DELAY", "seller"),
        "late_delivery_logistics": ("LOGISTICS_DELAY", "logistics_provider"),
        "canceled_order_paid": ("CANCELED_ORDER_PAYMENT_ISSUE", "platform"),
        "unavailable_order_paid": ("UNAVAILABLE_ORDER_PAYMENT_ISSUE", "seller"),
        "payment_mismatch": ("PAYMENT_PROCESSING_ERROR", "payment_provider"),
        "duplicate_charge": ("DUPLICATE_CHARGE_ERROR", "payment_provider"),
        "refund_pending": ("REFUND_PENDING", "platform"),
        "refund_failed": ("REFUND_FAILED", "payment_provider"),
        "valid_split_payment": ("VALID_SPLIT_PAYMENT", "platform"),
    }

    if primary_issue in cause_map:
        code, ptype = cause_map[primary_issue]
        causes.append({"cause_code": code, "rank": 1})
        if ptype == "seller" and seller_ids:
            for sid in seller_ids[:3]:
                parties.append({"party_type": "seller", "party_id": sid})
        else:
            parties.append({"party_type": ptype, "party_id": None})
    else:
        causes.append({"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1})
        parties.append({"party_type": "unknown", "party_id": None})

    return causes, parties


def _cross_check_responsible_parties(
    primary_issue: str,
    responsible_parties: list[dict],
    shipment_verdict: str,
) -> list[dict]:
    """Verifier-level consistency check.

    Guards against the exact failure mode Pha 4 calls out: a
    logistics-caused delay must not leave the seller holding
    responsibility, and vice versa. `_root_cause` already maps these
    correctly today, but this check exists so that responsibility is
    actually *verified* against the shipment verdict rather than only
    being correct "by construction" of one hardcoded table.
    """
    if primary_issue == "late_delivery_seller" and shipment_verdict == "logistics_delay":
        return [{"party_type": "logistics_provider", "party_id": None}]
    if primary_issue == "late_delivery_logistics" and shipment_verdict == "seller_delay":
        return [
            p for p in responsible_parties if p.get("party_type") != "logistics_provider"
        ] or [{"party_type": "seller", "party_id": None}]
    return responsible_parties


def _financial_resolution(
    case_status: str,
    primary_issue: str,
    captured: float | None,
    refunded: float | None,
) -> dict:
    rec = 0.0
    lines: list[dict] = []

    if case_status == "action_required" and captured is not None and captured > 0:
        deducted = refunded if refunded is not None else 0.0
        rec = max(0.0, captured - deducted)
        if rec > 0:
            lines.append({
                "reason_code": primary_issue,
                "amount_brl": round(rec, 2),
                "entity_id": None,
            })

    return {
        "currency": "BRL",
        "recommended_refund_brl": round(rec, 2),
        "refund_lines": lines[:10],
    }


def _resolution_actions(
    case_status: str,
    primary_issue: str,
    payment_verdict: str,
    seller_ids: list[str],
) -> list[str]:
    actions: list[str] = []

    if case_status == "action_required":
        act_map = {
            "late_delivery_seller": "Process_refund_for_delayed_order",
            "late_delivery_logistics": "Process_refund_for_logistics_delay",
            "canceled_order_paid": "Initiate_refund_for_canceled_order",
            "unavailable_order_paid": "Process_refund_for_unavailable_order",
            "payment_mismatch": "Investigate_payment_discrepancy_with_provider",
            "duplicate_charge": "Process_corrective_refund",
            "refund_pending": "Escalate_pending_refund_to_priority",
            "refund_failed": "Reattempt_refund_processing",
        }
        if primary_issue in act_map:
            actions.append(act_map[primary_issue])
        if primary_issue == "late_delivery_seller" and seller_ids:
            for sid in seller_ids[:2]:
                actions.append(f"Notify_seller_{sid}_about_delivery_delay")
        if payment_verdict in ("refund_pending",) and "Escalate_pending_refund_to_priority" not in actions:
            actions.append("Escalate_pending_refund_to_priority")

    if case_status == "no_action" and primary_issue == "valid_split_payment":
        actions.append("Communicate_split_payment_confirmation_to_customer")

    actions.append("Close_case_with_documentation")

    # uniqueItems is required by submission-manifest / output schema style
    # lists elsewhere; keep this list unique defensively.
    seen: set[str] = set()
    deduped: list[str] = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            deduped.append(a)
    return deduped[:8]