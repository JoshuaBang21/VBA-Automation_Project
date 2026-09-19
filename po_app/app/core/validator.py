"""
PO 자기검증 (설계문서 9번) + L 포함 이상 사이즈 업차지 체크 (11번, 임계값은 앱에서 조정 가능).

EDI로 수량 검증하지 않는다 — PO 자기 자신의 COLOR SUMMARY / TOTAL ORDER SUMMARY
숫자끼리만 비교한다 (원칙: PO PDF가 Source of Truth).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .constants import OVER_L_SIZES
from .parser import ParsedPo


@dataclass
class ColorCheck:
    color_code: str
    color_name: str
    size_sum: int
    total_color_qty: int
    ok: bool


@dataclass
class ValidationResult:
    po_no: str
    color_checks: list[ColorCheck] = field(default_factory=list)
    color_sum_vs_order_ok: bool | None = None
    order_units: int | None = None
    sum_of_colors: int | None = None
    all_ok: bool = False
    parse_warnings: list[str] = field(default_factory=list)   # 심각(FAIL 사유)
    info_notes: list[str] = field(default_factory=list)        # 경미(WARN 사유, Size/Qty 무관)

    @property
    def status(self) -> str:
        # FAIL이 WARN보다 항상 우선한다: Size/Qty 추출 실패(critical)나 자기검증
        # 불일치가 있으면, Store Type Code 누락 같은 부가정보 경고(info_notes)가
        # 있더라도 반드시 FAIL로 표시해야 한다 (경고에 가려 실패가 숨는 것 방지).
        if not self.all_ok:
            return "FAIL"
        if self.info_notes:
            return "WARN"
        return "OK"


def to_dataframe(parsed: ParsedPo) -> pd.DataFrame:
    """ParsedPo -> 라인 레벨 DataFrame (Color x Size)."""
    rows = []
    for ln in parsed.lines:
        rows.append({
            "po_no": parsed.header.po_no,
            "style_no": parsed.header.style_no,
            "factory_code": parsed.header.factory_code,
            "factory_name": parsed.header.factory_name,
            "channel_type": parsed.header.channel_type,
            "selling_channel": parsed.header.selling_channel,
            "flow_type": parsed.header.flow_type,
            "floorset": parsed.header.floorset,
            "sub_channel": ln.sub_channel,
            "color_code": ln.color_code,
            "color_name": ln.color_name,
            "design_color": ln.design_color,
            "pack_type": ln.pack_type,
            "fob": ln.fob,
            "size_code": ln.size_code,
            "qty": ln.qty,
            "total_color_qty": ln.total_color_qty,
            "total_line_qty": ln.total_line_qty,
        })
    return pd.DataFrame(rows)


def validate(parsed: ParsedPo) -> tuple[ValidationResult, pd.DataFrame]:
    """3단 검증:
    1) Size 합 == Total Color Qty (Color별)
    2) 모든 Color 합 == TOTAL ORDER SUMMARY (PO 전체)
    3) 1)+2) 모두 통과 -> 파싱 정확 확정

    parsed.warnings(critical: 라벨/수량 추출 실패 등)만 all_ok를 FAIL시킨다.
    parsed.info_notes(Sub Channel 누락 등 부가정보)는 상태를 WARN으로만 낮춘다
    (Size/Qty 정확성과는 무관하므로 all_ok 판정에는 포함하지 않는다).
    """
    df = to_dataframe(parsed)
    result = ValidationResult(
        po_no=parsed.header.po_no,
        parse_warnings=list(parsed.warnings),
        info_notes=list(parsed.info_notes),
    )

    if df.empty:
        result.all_ok = False
        return result, df

    per_color = df.groupby("color_code", as_index=False).agg(
        size_sum=("qty", "sum"),
        total_color_qty=("total_color_qty", "first"),
        color_name=("color_name", "first"),
    )
    for _, r in per_color.iterrows():
        result.color_checks.append(ColorCheck(
            color_code=r["color_code"],
            color_name=r["color_name"],
            size_sum=int(r["size_sum"]),
            total_color_qty=int(r["total_color_qty"]),
            ok=int(r["size_sum"]) == int(r["total_color_qty"]),
        ))

    sum_of_colors = int(df.drop_duplicates("color_code")["total_color_qty"].sum())
    order_units = parsed.header.total_order_units
    result.sum_of_colors = sum_of_colors
    result.order_units = order_units
    result.color_sum_vs_order_ok = (order_units is not None and sum_of_colors == order_units)

    result.all_ok = (
        all(c.ok for c in result.color_checks)
        and bool(result.color_sum_vs_order_ok)
        and not parsed.warnings
    )
    return result, df


def upcharge_check(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Color별 L 포함 이상 사이즈 비율 계산 (설계문서 11번, Tall류 포함).

    threshold: 0.5 = 50% 초과 시 경고 (사용자 요청 기준. 설계문서 기본값은 51%).
    """
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["is_over_l"] = df["size_code"].isin(OVER_L_SIZES)

    grouped = df.groupby(["po_no", "color_code", "color_name"], as_index=False).apply(
        lambda g: pd.Series({
            "under_l_qty": int(g.loc[~g["is_over_l"], "qty"].sum()),
            "over_l_qty": int(g.loc[g["is_over_l"], "qty"].sum()),
            "total_qty": int(g["qty"].sum()),
        }),
        include_groups=False,
    )
    grouped["over_l_ratio"] = grouped["over_l_qty"] / grouped["total_qty"].replace(0, pd.NA)
    grouped["needs_upcharge_review"] = grouped["over_l_ratio"] > threshold
    return grouped
