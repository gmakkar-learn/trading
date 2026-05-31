"""Weighted composite scorer for earnings signals. All weights live in fundamental.yaml."""
from __future__ import annotations
from dataclasses import dataclass
from .result_document import ResultDocument


@dataclass
class ScoredResult:
    composite_score: float
    action: str        # "BUY" | "SELL" | "HOLD"
    confidence: str    # "high" | "medium" | "low"
    context: dict


def _score_revenue(yoy_pct: float | None) -> float:
    if yoy_pct is None:
        return 50.0
    if yoy_pct >= 15:  return 90.0
    if yoy_pct >= 10:  return 75.0
    if yoy_pct >= 5:   return 60.0
    if yoy_pct >= 0:   return 45.0
    if yoy_pct >= -5:  return 30.0
    return 15.0


def _score_earnings(yoy_pct: float | None) -> float:
    if yoy_pct is None:
        return 50.0
    if yoy_pct >= 20:  return 92.0
    if yoy_pct >= 10:  return 75.0
    if yoy_pct >= 5:   return 60.0
    if yoy_pct >= 0:   return 42.0
    if yoy_pct >= -10: return 25.0
    return 10.0


def _score_margin(direction: str | None) -> float:
    if direction == "expanding":   return 80.0
    if direction == "stable":      return 50.0
    if direction == "contracting": return 20.0
    return 50.0


def _score_guidance(direction: str | None) -> float:
    if direction == "raised":     return 90.0
    if direction == "maintained": return 55.0
    if direction == "cut":        return 10.0
    return 50.0


def _score_dividend(change: str | None) -> float:
    if change in ("initiated", "increased"): return 80.0
    if change == "maintained":               return 50.0
    if change in ("cut", "omitted"):         return 15.0
    return 50.0


def score(result: ResultDocument, config: dict) -> ScoredResult:
    weights = config.get("scoring", {}).get("weights", {})
    w_rev  = float(weights.get("revenue_beat", 0.20))
    w_pat  = float(weights.get("pat_beat", 0.30))
    w_marg = float(weights.get("margin_direction", 0.20))
    w_guid = float(weights.get("guidance_change", 0.15))
    w_div  = float(weights.get("dividend_signal", 0.10))
    w_exc  = float(weights.get("exceptional_penalty", -0.15))  # negative weight

    components: dict[str, float] = {}
    weighted_sum = 0.0
    weight_sum = 0.0

    r = _score_revenue(result.revenue.yoy_growth_pct)
    components["revenue"] = r
    weighted_sum += r * w_rev
    weight_sum += w_rev

    p_pct = result.earnings.eps_yoy_growth_pct or result.earnings.net_income_yoy_growth_pct
    p = _score_earnings(p_pct)
    components["pat"] = p
    weighted_sum += p * w_pat
    weight_sum += w_pat

    m = _score_margin(result.margins.operating_margin_direction)
    components["margin"] = m
    weighted_sum += m * w_marg
    weight_sum += w_marg

    if result.guidance.provided:
        g = _score_guidance(result.guidance.direction)
        components["guidance"] = g
        weighted_sum += g * w_guid
        weight_sum += w_guid

    if result.dividend.declared:
        d = _score_dividend(result.dividend.change)
        components["dividend"] = d
        weighted_sum += d * w_div
        weight_sum += w_div

    composite = (weighted_sum / weight_sum) if weight_sum > 0 else 50.0

    if result.exceptional_items.present:
        penalty = abs(w_exc) * 100  # e.g. 0.15 → subtract 15 points
        composite = max(0.0, composite - penalty)
        components["exceptional_penalty"] = -penalty

    composite = min(100.0, max(0.0, composite))

    thresholds = config.get("scoring", {}).get("thresholds", {})
    strong_buy   = float(thresholds.get("strong_buy", 75))
    moderate_buy = float(thresholds.get("moderate_buy", 60))
    neutral_low  = float(thresholds.get("neutral_low", 40))

    if composite >= strong_buy:
        action, strength = "BUY", "high"
    elif composite >= moderate_buy:
        action, strength = "BUY", "medium"
    elif composite >= neutral_low:
        action, strength = "HOLD", "low"
    else:
        action, strength = "SELL", "medium"

    # Downgrade confidence if Claude was uncertain, never upgrade beyond score-derived strength
    _rank = {"low": 0, "medium": 1, "high": 2}
    final_confidence = min(strength, result.confidence, key=lambda c: _rank.get(c, 1))

    return ScoredResult(
        composite_score=round(composite, 2),
        action=action,
        confidence=final_confidence,
        context={
            "components": {k: round(v, 2) for k, v in components.items()},
            "quarter": result.quarter,
            "exceptional_items": result.exceptional_items.present,
            "guidance_direction": result.guidance.direction,
            "revenue_yoy_pct": result.revenue.yoy_growth_pct,
            "eps_yoy_pct": result.earnings.eps_yoy_growth_pct,
            "margin_direction": result.margins.operating_margin_direction,
        },
    )
