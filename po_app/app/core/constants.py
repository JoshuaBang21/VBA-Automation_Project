"""
설계문서(PO_PARSING_DESIGN.md) 기준 상수 정의.
common.py 성격의 모듈 — 값이 바뀌면 여기만 고치면 된다 (Single Source of Truth).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Channel Type (Header) — SELLING CHANNEL -> INTL / OMNI  (설계문서 6번)
# ---------------------------------------------------------------------------
OMNI_CHANNELS: set[str] = {"WEB", "STORE", "CANADA"}

INTL_CHANNELS: set[str] = {
    "MEXICO", "MEXICO WHOLESALE", "GCO GT", "GCO CO", "GCO EC",
    "ALSHAYA KSA", "ALSHAYA KW", "ALSHAYA EG", "ALSHAYA ME",
    "PACIFICA HK", "FOX IL", "EU JV", "CRM", "DAVID CA",
    "EFASHION CL HOLD", "ADITYA BIRLA IN", "COPPEL WHOLESALE",
    "GCO PE HOLD", "PACIFICA TH",
}


def classify_channel(selling_channel: str | None) -> str:
    """SELLING CHANNEL 문자열을 INTL/OMNI/UNKNOWN 으로 분류."""
    normalized = selling_channel.strip().upper() if selling_channel else None
    if normalized in OMNI_CHANNELS:
        return "OMNI"
    if normalized in INTL_CHANNELS:
        return "INTL"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Flow 매핑 (Delivery -> flow_type)  (설계문서 5번)
# ---------------------------------------------------------------------------
FLOW_MAP: dict[str, str] = {
    "02_INITIAL": "INI",
    "02_INITIAL-1": "INI-1",  # 수정 재발행, 별도 구분
    "03_FLOW01": "FL1",
    "04_FLOW02": "FL2",
    "05_FLOW03": "FL3",
    "06_FLOW04": "FL4",
}


def classify_flow(delivery: str | None) -> str | None:
    if delivery is None:
        return None
    return FLOW_MAP.get(delivery, delivery)  # 매핑 안 되면 원본값 유지 + 파싱 경고로 노출


# ---------------------------------------------------------------------------
# Size 표준명 매핑 (국가 무시, 전체 통일)  (설계문서 7번)
# ---------------------------------------------------------------------------
SIZE_MAP: dict[str, str] = {
    # US
    "US - XS": "XS", "US - S": "S", "US - M": "M", "US - L": "L", "US - XL": "XL",
    "US - XXL": "XXL", "US - XXXL": "XXXL",
    "US - M Tall": "MTall", "US - L Tall": "LTall", "US - XL Tall": "XLTall",
    # EU
    "EU - XS": "XS", "EU - S": "S", "EU - M": "M", "EU - L": "L", "EU - XL": "XL",
    # CA (프랑스어 표기)
    "CA - TP": "XS", "CA - P": "S", "CA - M": "M", "CA - G": "L", "CA - TG": "XL",
    "CA - TTG": "XXL", "CA - TTTG": "XXXL",
    "CA - MTE": "MTall", "CA - GTE": "LTall", "CA - TGTE": "XLTall",
    # MX (스페인어 표기)
    "MX - EXCH": "XS", "MX - CH": "S", "MX - M": "M", "MX - G": "L",
    "MX - EXG": "XL", "MX - XXG": "XXL",
}

# 표준 사이즈 정렬 순서 (전체 우주 — 실제 PO에는 이 중 일부만 등장)
SIZE_ORDER: list[str] = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "MTall", "LTall", "XLTall"]

# L 사이즈 포함(>= L) 업차지 검토 대상 — Tall류 포함
OVER_L_SIZES: set[str] = {"L", "XL", "XXL", "XXXL", "MTall", "LTall", "XLTall"}

# 사용자 지정 업차지(Upcharge) 검토 임계값 — 사이드바에서 조정 가능하도록 기본값만 상수로 둔다
DEFAULT_UPCHARGE_THRESHOLD = 0.50  # 50%

RAW_SIZE_LABEL_RE = r"(?:US|EU|CA|MX)\s*-\s*[A-Za-z]+(?:\s+Tall)?"
