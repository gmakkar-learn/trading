"""Append-only audit logger. Every decision is written before the action is taken."""
import dataclasses
import logging
from typing import Any

from sqlalchemy import select, desc

logger = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def log(
        self,
        decision: str,
        *,
        market_id: str | None = None,
        ticker: str | None = None,
        reason: str | None = None,
        signal: Any | None = None,
        signal_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        from infrastructure.database.models.audit_log import AuditLog  # noqa: PLC0415

        signal_json: dict | None = None
        if signal is not None:
            try:
                signal_json = dataclasses.asdict(signal)
                # Make datetime fields JSON-serializable
                for k, v in signal_json.items():
                    if hasattr(v, "isoformat"):
                        signal_json[k] = v.isoformat()
            except TypeError:
                signal_json = {"raw": str(signal)}
        elif signal_id is not None:
            signal_json = {"signal_id": signal_id}

        entry = AuditLog(
            market_id=market_id,
            ticker=ticker,
            decision=decision,
            reason=reason,
            signal_json=signal_json,
            order_id=order_id,
        )
        try:
            async with self._session_factory() as session:
                session.add(entry)
                await session.commit()
        except Exception as exc:
            # Never silently swallow audit failures — fallback to logging
            logger.critical(
                "AUDIT WRITE FAILED: %s | decision=%s ticker=%s reason=%s",
                exc, decision, ticker, reason,
            )

    async def get_signals(
        self,
        market_id: str | None = None,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Return persisted signals from audit_log (decision='SIGNAL'), newest first."""
        from infrastructure.database.models.audit_log import AuditLog  # noqa: PLC0415
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(AuditLog)
                    .where(AuditLog.decision == "SIGNAL")
                    .order_by(desc(AuditLog.created_at))
                    .limit(limit)
                )
                if market_id:
                    stmt = stmt.where(AuditLog.market_id == market_id)
                rows = (await session.execute(stmt)).scalars().all()
                result = []
                for row in rows:
                    sig = row.signal_json or {}
                    if action and sig.get("recommended_action") != action.upper():
                        continue
                    result.append(sig)
                return result
        except Exception as exc:
            logger.error("get_signals query failed: %s", exc)
            return []
