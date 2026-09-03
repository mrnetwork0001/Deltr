"""deltr/receipts.py — ExecutionReceipt factory + append-only store.

``make_receipt`` is the single place a receipt is assembled (the model seals its
own sha256 over ``digest_body()``).  ``ReceiptStore`` keeps a bounded in-memory
deque, mirrors into ``State.receipts`` when a State is attached, and appends every
receipt as one canonical JSON line to ``state/<mode>/receipts.jsonl``.
``decision_log_sha256`` hashes the ordered gate decisions so two replay runs can be
compared byte-for-byte (tests/test_replay_determinism.py).
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from deltr.config import Mode
from deltr.models import (
    ExecutionReceipt,
    Fill,
    HedgePlan,
    ReceiptStatus,
    RiskDecisionRecord,
    TraceSource,
    TraceStep,
    canonical_json,
)

log = logging.getLogger("deltr.receipts")

STORE_MAXLEN = 1000


def make_receipt(
    plan: HedgePlan,
    decision: RiskDecisionRecord,
    fills: List[Fill],
    status: ReceiptStatus,
    steps: List[TraceStep],
    mode: Mode,
    source: TraceSource,
    client: Optional[str],
    position_id: Optional[str],
    residual_delta_base: float,
    realized_cost_usd: float,
    legging_window_ms: Optional[int],
    stress_active: Optional[str],
) -> ExecutionReceipt:
    """Assemble a sealed receipt (sha256 computed by the model validator)."""
    return ExecutionReceipt(
        plan_id=plan.id,
        source=source,
        client=client,
        prompt=plan.prompt,
        mode=mode,
        status=status,
        decision=decision,
        plan=plan,
        fills=list(fills),
        position_id=position_id,
        residual_delta_base=float(residual_delta_base),
        realized_cost_usd=float(realized_cost_usd),
        legging_window_ms=legging_window_ms,
        steps=list(steps),
        stress_active=stress_active,
    )


class ReceiptStore:
    """Bounded deque + append-only JSONL log (state/<mode>/receipts.jsonl)."""

    def __init__(self, state: Any, path: Optional[str]) -> None:
        self.state = state
        self.path = path
        self._items: Deque[ExecutionReceipt] = deque(maxlen=STORE_MAXLEN)
        self._by_id: Dict[str, ExecutionReceipt] = {}

    def put(self, r: ExecutionReceipt) -> None:
        if len(self._items) == self._items.maxlen and self._items:
            self._by_id.pop(self._items[0].id, None)
        self._items.append(r)
        self._by_id[r.id] = r
        st = self.state
        if st is not None:
            rec = getattr(st, "record_receipt", None)
            try:
                if callable(rec):
                    rec(r)
                elif hasattr(st, "receipts"):
                    st.receipts.append(r)
            except Exception as exc:  # pragma: no cover - never let the UI mirror break execution
                log.debug("receipts: state mirror skipped: %s", exc)
        self._append_jsonl(r)

    def _append_jsonl(self, r: ExecutionReceipt) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(canonical_json(r.model_dump(mode="json")) + "\n")
        except OSError as exc:
            log.error("receipts: append failed: %s", exc)

    def get(self, receipt_id: str) -> Optional[ExecutionReceipt]:
        return self._by_id.get(receipt_id)

    def recent(self, n: int = 10) -> List[ExecutionReceipt]:
        if n <= 0:
            return []
        return list(self._items)[-n:]

    def all(self) -> List[ExecutionReceipt]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def decision_entries(self) -> List[Dict[str, Any]]:
        """Ordered, canonical view of every decision this store has seen."""
        return [
            {
                "code": r.decision.code,
                "approved": r.decision.approved,
                "reason": r.decision.reason,
                "status": r.status,
                "plan_hash": r.plan.plan_hash,
            }
            for r in self._items
        ]

    def decision_log_sha256(self) -> str:
        """sha256 over the canonical JSON of the ordered decision codes/reasons."""
        return hashlib.sha256(canonical_json(self.decision_entries()).encode("utf-8")).hexdigest()


__all__ = ["make_receipt", "ReceiptStore", "STORE_MAXLEN"]
