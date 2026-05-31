from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RevenueData:
    actual_millions: Optional[float] = None
    yoy_growth_pct: Optional[float] = None


@dataclass
class EarningsData:
    eps_actual: Optional[float] = None
    eps_yoy_growth_pct: Optional[float] = None
    net_income_millions: Optional[float] = None
    net_income_yoy_growth_pct: Optional[float] = None


@dataclass
class MarginData:
    gross_margin_pct: Optional[float] = None
    operating_margin_pct: Optional[float] = None
    operating_margin_direction: Optional[str] = None  # "expanding" | "contracting" | "stable"


@dataclass
class GuidanceData:
    provided: bool = False
    direction: Optional[str] = None   # "raised" | "maintained" | "cut"
    revenue_next_quarter: Optional[float] = None
    eps_next_quarter: Optional[float] = None


@dataclass
class DividendData:
    declared: bool = False
    change: Optional[str] = None  # "initiated"|"increased"|"maintained"|"cut"|"omitted"
    amount: Optional[float] = None


@dataclass
class ExceptionalItems:
    present: bool = False
    description: Optional[str] = None
    impact_millions: Optional[float] = None


@dataclass
class ResultDocument:
    ticker: str
    quarter: str
    revenue: RevenueData = field(default_factory=RevenueData)
    earnings: EarningsData = field(default_factory=EarningsData)
    margins: MarginData = field(default_factory=MarginData)
    guidance: GuidanceData = field(default_factory=GuidanceData)
    dividend: DividendData = field(default_factory=DividendData)
    exceptional_items: ExceptionalItems = field(default_factory=ExceptionalItems)
    confidence: str = "low"
    notes: str = ""
    raw_claude_response: str = ""

    @classmethod
    def from_claude_response(cls, data: dict, ticker: str, quarter: str) -> ResultDocument:
        rev = data.get("revenue", {}) or {}
        earn = data.get("earnings", {}) or {}
        marg = data.get("margins", {}) or {}
        guid = data.get("guidance", {}) or {}
        div = data.get("dividend", {}) or {}
        exc = data.get("exceptional_items", {}) or {}

        return cls(
            ticker=ticker,
            quarter=quarter,
            revenue=RevenueData(
                actual_millions=rev.get("actual"),
                yoy_growth_pct=rev.get("yoy_growth_pct"),
            ),
            earnings=EarningsData(
                eps_actual=earn.get("eps_actual"),
                eps_yoy_growth_pct=earn.get("eps_yoy_growth_pct"),
                net_income_millions=earn.get("net_income_actual"),
                net_income_yoy_growth_pct=earn.get("net_income_yoy_growth_pct"),
            ),
            margins=MarginData(
                gross_margin_pct=marg.get("gross_margin_pct"),
                operating_margin_pct=marg.get("operating_margin_pct"),
                operating_margin_direction=marg.get("operating_margin_direction"),
            ),
            guidance=GuidanceData(
                provided=bool(guid.get("provided", False)),
                direction=guid.get("direction"),
                revenue_next_quarter=guid.get("revenue_next_quarter"),
                eps_next_quarter=guid.get("eps_next_quarter"),
            ),
            dividend=DividendData(
                declared=bool(div.get("declared", False)),
                change=div.get("change"),
                amount=div.get("amount"),
            ),
            exceptional_items=ExceptionalItems(
                present=bool(exc.get("present", False)),
                description=exc.get("description"),
                impact_millions=exc.get("impact_millions"),
            ),
            confidence=data.get("confidence", "low"),
            notes=data.get("notes", ""),
        )
