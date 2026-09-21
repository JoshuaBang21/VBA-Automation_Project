"""
1차 파싱 엔진 (정규식 기반, "빠른 경로").

설계 배경
---------
설계문서는 LLM 파싱을 채택했지만, 실제 15~82건 표본을 정규식으로 파싱해본 결과
3단 자기검증(9번)을 100% 통과했다. 즉 AEO PO PDF는 격자(box) 양식이 고정적이라
정규식만으로도 충분히 정밀하다.

따라서 이 앱은 "정규식 우선 + 자기검증 실패 시에만 LLM 재파싱"(llm_fallback.py) 구조로
비용/속도를 최적화한다. 포맷이 조금이라도 바뀌면 자기검증이 즉시 잡아내므로
정규식만 쓰는 것이 위험하지 않다 — 검증이 안전망 역할을 한다.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from dataclasses import dataclass, field

import pdfplumber

from .constants import (
    RAW_SIZE_LABEL_RE,
    SIZE_MAP,
    classify_channel,
    classify_flow,
)


class PoParseError(Exception):
    """PO PDF에서 헤더/라인을 추출하지 못했을 때."""


def _normalize_factory_name(name: str | None) -> str | None:
    if not name:
        return name
    normalized = name.strip()
    if normalized.upper() == "SESHIN VN2 CO., LTD.":
        return "VN2"
    if normalized.upper().startswith("PT"):
        return "PT.SSI"
    return normalized


@dataclass
class PoLine:
    color_code: str
    color_name: str
    design_color: str
    sub_channel: str | None
    pack_type: str
    fob: float | None
    raw_size_label: str
    size_code: str
    qty: int
    total_color_qty: int
    total_line_qty: int | None
    hand_over: str | None = None  # 이 라인(PO ROW/배송분)의 실제 Hand Over (분할 출고 대응)


@dataclass
class PoHeader:
    po_no: str
    style_no: str | None
    factory_code: str | None
    factory_name: str | None
    selling_channel: str | None
    channel_type: str
    floorset: str | None
    delivery_raw: str | None
    flow_type: str | None
    total_order_units: int | None
    hand_over: str | None


@dataclass
class ParsedPo:
    header: PoHeader
    lines: list[PoLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # 심각(FAIL 사유): 사이즈/수량 추출 자체가 실패
    info_notes: list[str] = field(default_factory=list)  # 경미(WARN 사유): Sub Channel 등 부가정보 누락


def _norm(text: str) -> str:
    """추출기(pdfplumber/pdftotext 등)에 따라 달라지는 공백/개행을 정규화."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _clean_num(s: str) -> int:
    return int(round(float(s.replace(",", "").strip())))


def extract_pages_text(pdf_bytes: bytes) -> list[str]:
    """PDF 바이트 -> 페이지별 텍스트 리스트."""
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(_norm(page.extract_text() or ""))
    if not pages:
        raise PoParseError("PDF에서 텍스트를 추출하지 못했습니다 (스캔본일 가능성).")
    return pages


def parse_header(page1_text: str) -> PoHeader:
    def find(pattern: str, default=None):
        m = re.search(pattern, page1_text)
        return m.group(1).strip() if m else default

    po_no = find(r"PURCHASE ORDER:\s*(\d+)")
    if not po_no:
        raise PoParseError("PO번호(PURCHASE ORDER)를 찾지 못했습니다 — 포맷이 예상과 다릅니다.")

    selling_channel = find(r"SELLING CHANNEL:\s*(.+)")
    floorset = find(r"Floorset\s+([A-Z]+)")
    delivery = find(r"Delivery\s+([0-9A-Z_\-]+)")
    total_units_raw = find(r"UNITS\s+([\d,]+\.\d\d)")

    return PoHeader(
        po_no=po_no,
        style_no=None,       # 라인 페이지에서 채움
        factory_code=None,   # 라인 페이지에서 채움
        factory_name=None,
        selling_channel=selling_channel,
        channel_type=classify_channel(selling_channel),
        floorset=floorset,
        delivery_raw=delivery,
        flow_type=classify_flow(delivery),
        total_order_units=_clean_num(total_units_raw) if total_units_raw else None,
        hand_over=None,
    )


_COLOR_START_RE = re.compile(
    r"Color:\s*(\d+)\s*-\s*(.+?)\s*Design Color:\s*(.+?)(?:\n|$)"
)

_STORE_TYPE_RE = re.compile(
    r"Start Ship Hand Over Total Line Qty Store Type Code[ \t]*"
    r"(?:(?!\n(?:Start Ship|COLOR SUMMARY)).)*?"
    r"(\d\d/\d\d/\d\d)\s+(\d\d/\d\d/\d\d)\s+([\d,]+\.\d\d)\s+(\S+)",
    re.DOTALL,
)

_HAND_OVER_RE = re.compile(
    r"Start Ship\s+Hand Over.*?\n\s*"
    r"(\d\d/\d\d/\d\d)\s+(\d\d/\d\d/\d\d)",
    re.DOTALL,
)


def _extract_hand_over(pages: list[str]) -> tuple[str | None, str | None]:
    """PDF 전체의 Hand Over 날짜를 모아 대표값과 분할 출고 안내 메모를 반환한다.

    같은 PO/Color 안에서도 배송분(PO ROW)마다 Hand Over가 다를 수 있다
    (분할 출고, 예: PO 1390937 "split ship line 1 with 12.9 HO"). 이를
    에러로 막지 않고, PO_Header에는 가장 이른 날짜를 대표값으로 저장하며
    실제 배송분별 정확한 날짜는 my_po_line.hand_over에 남긴다.
    """
    dates = []
    for page in pages:
        dates.extend(match.group(2) for match in _HAND_OVER_RE.finditer(page))
    if not dates:
        return None, None
    unique_dates = sorted(set(dates), key=lambda d: datetime.strptime(d, "%m/%d/%y"))
    primary = unique_dates[0]
    note = None
    if len(unique_dates) > 1:
        note = (
            "PO 내 Hand Over 날짜가 여러 개입니다 (분할 출고 가능성): "
            + ", ".join(unique_dates)
            + " — PO_Header에는 최초 날짜만 대표로 표시되며, 실제 배송분별 날짜는 "
            "저장된 PO 라인(색상별 상세)에서 확인해야 합니다."
        )
    return (
        datetime.strptime(primary, "%m/%d/%y").strftime("%Y-%m-%d"),
        note,
    )


def _extract_color_segments(page_text: str) -> list[tuple[re.Match, str]]:
    """페이지 텍스트에서 Color별 COLOR SUMMARY 구간을 잘라낸다.

    구간 경계 = 다음 Color: 헤더 / 다음 "BULK/ PRE Class Style No." (다음 PO ROW) /
    "BLK PACKAGING" (포장 안내 섹션 시작) 중 가장 먼저 나오는 지점.
    이렇게 하면 Tall 사이즈처럼 SKU 줄 다음으로 줄바꿈되어 이어지는 연속 블록도
    같은 Color의 구간 안에 포함된다 (설계문서 12번이 지적한 "줄바꿈으로 라벨-숫자
    분리" 케이스).
    """
    segments = []
    starts = list(_COLOR_START_RE.finditer(page_text))
    for i, m in enumerate(starts):
        seg_start = m.end()
        candidates = [len(page_text)]
        if i + 1 < len(starts):
            candidates.append(starts[i + 1].start())
        row_m = re.search(r"\n(?:BULK|PRE) Class Style No\.", page_text[seg_start:])
        if row_m:
            candidates.append(seg_start + row_m.start())
        pack_m = re.search(r"\nBLK PACKAGING", page_text[seg_start:])
        if pack_m:
            candidates.append(seg_start + pack_m.start())
        seg_end = min(candidates)
        segments.append((m, page_text[seg_start:seg_end]))
    return segments


def _number_tokens(text: str) -> list[int]:
    return [
        _clean_num(token)
        for token in re.findall(r"\b\d[\d,]*\b", text)
        if len(token.replace(",", "")) < 10
    ]


def _extract_block_size_pairs(block_text: str) -> list[tuple[str, int]] | None:
    """PRE/BULK 블록 자체의 Size 라벨과 Qty/Size 수량을 짝짓는다.

    COLOR SUMMARY 전체 라벨이 아니라 이 블록(PO ROW)만의 라벨을 쓴다 —
    분할 출고 시 배송분마다 사이즈 구성(예: XS~XXL vs S~XL)이 다를 수 있어서다.
    """
    first_label_m = re.search(RAW_SIZE_LABEL_RE, block_text)
    if not first_label_m:
        return None
    country = first_label_m.group(0).split("-")[0].strip()
    labels = re.findall(rf"{re.escape(country)}\s*-\s*[A-Za-z]+(?:\s+Tall)?", block_text)
    qty_m = re.search(
        r"\nQty/Size(?!:)\s+((?:\d[\d,]*\s+){1,}\d[\d,]*)",
        block_text,
    )
    if not qty_m:
        return None
    quantities = _number_tokens(qty_m.group(1))
    if len(labels) != len(quantities):
        return None
    return list(zip(labels, quantities))


def _extract_pack_blocks(pages: list[str]) -> dict[str, dict[str, list[dict]]]:
    """Collect PREPACK/BULK PO ROW blocks by color across the whole PDF.

    A COLOR SUMMARY can be on the next page after its PO ROW, so this is
    intentionally collected before individual pages are parsed.

    한 Color 안에 같은 kind(PREPACK/BULK)의 PO ROW가 여러 개 있을 수 있다:
      1) 완전히 같은 Hand Over/사이즈 구성으로 나뉜 행 -> 합산한다 (PO 1390983).
      2) 분할 출고(split ship)로 Hand Over가 서로 다른 행 -> 별도 배송분으로
         남겨 my_po_line에 각자의 Hand Over를 붙인다 (PO 1390937).
    """
    blocks: dict[str, dict[str, list[dict]]] = {}
    document = "\n\f\n".join(pages)
    block_re = re.compile(
        r"(?P<kind>PRE|BULK) Class Style No\. Color Design Color.*?"
        r"(?=(?:\n(?:PRE|BULK) Class Style No\. Color|\nCOLOR SUMMARY|\Z))",
        re.DOTALL,
    )
    for match in block_re.finditer(document):
        block = match.group(0)
        color_m = re.search(r"\n(?:PACK\s+)?\d+\s+\d+\s+(\d+)\s+", block)
        if not color_m:
            continue
        qty_m = re.search(
            r"\nQty/Size(?!:)\s+((?:\d[\d,]*\s+){1,}\d[\d,]*)",
            block,
        )
        if not qty_m:
            continue
        quantities = _number_tokens(qty_m.group(1))
        if not quantities:
            continue

        kind = "PREPACK" if match.group("kind") == "PRE" else "BULK"
        total_line_qty = None
        if kind == "PREPACK":
            line_m = re.search(
                r"\d\d/\d\d/\d\d\s+\d\d/\d\d/\d\d\s+\d+\s+([\d,]+\.\d\d)",
                block,
            )
        else:
            line_m = re.search(
                r"\d\d/\d\d/\d\d\s+\d\d/\d\d/\d\d\s+([\d,]+\.\d\d)\s+\S+",
                block,
            )
        if line_m:
            total_line_qty = _clean_num(line_m.group(1))

        ho_m = _HAND_OVER_RE.search(block)
        hand_over = (
            datetime.strptime(ho_m.group(2), "%m/%d/%y").strftime("%Y-%m-%d")
            if ho_m
            else None
        )
        label_pairs = _extract_block_size_pairs(block)

        color_blocks = blocks.setdefault(color_m.group(1), {})
        kind_blocks = color_blocks.setdefault(kind, [])

        merged = False
        for existing in kind_blocks:
            existing_pairs = existing.get("pairs")
            if label_pairs is not None and existing_pairs is not None:
                same_shape = [lbl for lbl, _ in existing_pairs] == [
                    lbl for lbl, _ in label_pairs
                ]
            else:
                same_shape = len(existing["quantities"]) == len(quantities)
            if same_shape and existing.get("hand_over") == hand_over:
                existing["quantities"] = [
                    previous + current
                    for previous, current in zip(existing["quantities"], quantities)
                ]
                if existing_pairs is not None and label_pairs is not None:
                    existing["pairs"] = [
                        (lbl, prev_q + cur_q)
                        for (lbl, prev_q), (_, cur_q) in zip(existing_pairs, label_pairs)
                    ]
                if total_line_qty is not None:
                    existing["total_line_qty"] = (
                        (existing["total_line_qty"] or 0) + total_line_qty
                    )
                merged = True
                break
        if not merged:
            kind_blocks.append({
                "quantities": quantities,
                "pairs": label_pairs,
                "total_line_qty": total_line_qty,
                "hand_over": hand_over,
            })
    return blocks


def _parse_color_segment(
    m: re.Match, search_text: str, warnings: list[str]
) -> tuple[str, str, str, int | None, list[tuple[str, int]]]:
    """Color 1개 구간에서 (color_code, color_name, design_color, total_color_qty,
    [(raw_size_label, qty), ...]) 를 뽑는다.

    핵심 아이디어: 사이즈 라벨과 수량을 "같은 줄"이라고 가정하지 않는다.
    대신 (1) Total Color Qty 값을 먼저 떼어내 마스킹하고,
         (2) 구간 전체에서 '기준 국가'(가장 먼저 나온 US/EU/CA/MX 라벨의 국가)의
             라벨을 순서대로 전부 모으고,
         (3) 10자리 SKU 코드가 아닌 나머지 숫자 토큰을 순서대로 전부 모아
             라벨 개수와 zip 한다.
    이렇게 하면 Tall처럼 SKU 줄 뒤로 줄바꿈되어 이어지는 라벨/수량도 그대로
    이어 붙어 잡힌다 (기존 정규식은 "Qty/Size:" 뒤 첫 줄에서 멈췄음).
    """
    color_code = m.group(1)
    color_name = m.group(2).strip()
    design_color = m.group(3).strip()

    # Total Color Qty: "Total Color Qty" 키워드 뒤, "Qty/Size:" 앞에 오는 마지막 숫자.
    # (단일 라벨 표기든, 국가 이중 표기든 이 위치는 항상 동일하다.)
    total_color_qty = None
    m_total = re.search(r"Total Color Qty(.*?)([\d,]+)\s*\n\s*Qty/Size:", search_text, re.DOTALL)
    if m_total:
        total_color_qty = _clean_num(m_total.group(2))
        s, e = m_total.span(2)
        search_text = search_text[:s] + " " * (e - s) + search_text[e:]
    else:
        warnings.append(f"Color {color_code}: Total Color Qty 숫자를 못 찾음.")

    # 기준 국가 = 구간에서 가장 먼저 등장하는 사이즈 라벨의 국가.
    # (US/CA/MX/EU 중 어떤 걸 기준으로 잡아도 SIZE_MAP에서 동일 표준명으로
    #  수렴하므로, 중복 표기(이중 라벨) 중 하나만 쓰면 충분하다.)
    first_label_m = re.search(RAW_SIZE_LABEL_RE, search_text)
    if not first_label_m:
        warnings.append(f"Color {color_code}: 사이즈 라벨을 찾지 못함.")
        return color_code, color_name, design_color, total_color_qty, []

    country = first_label_m.group(0).split("-")[0].strip()
    labels = re.findall(rf"{re.escape(country)}\s*-\s*[A-Za-z]+(?:\s+Tall)?", search_text)

    # 수량은 COLOR SUMMARY의 Qty/Size 블록 안에서만 읽는다. 구간 뒤쪽에는
    # PDF footer의 PO 번호/페이지 번호가 붙을 수 있으므로 전체 구간의 숫자를
    # 수량으로 취급하면 안 된다.
    qty_section_m = re.search(
        r"Qty/Size:\s*(.*?)(?:\nThe terms and conditions|\nPURCHASE ORDER|$)",
        search_text,
        re.DOTALL,
    )
    if not qty_section_m:
        warnings.append(f"Color {color_code}: Qty/Size 블록을 찾지 못함.")
        return color_code, color_name, design_color, total_color_qty, []

    # PDF 추출 결과의 천 단위 쉼표를 보존한다. `1,086`을 `1`, `086`으로
    # 나누면 사이즈 라벨과 수량의 개수가 어긋나므로 하나의 숫자 토큰으로 읽는다.
    qtys = _number_tokens(qty_section_m.group(1))

    if len(labels) != len(qtys):
        warnings.append(
            f"Color {color_code}: 사이즈 라벨({len(labels)}개)과 수량({len(qtys)}개) "
            f"개수 불일치 — 페이지 경계에 걸쳐 있을 수 있음. LLM 재파싱 대상."
        )
        return color_code, color_name, design_color, total_color_qty, []

    return color_code, color_name, design_color, total_color_qty, list(zip(labels, qtys))


def parse_line_page(
    page_text: str,
    header: PoHeader,
    context: dict | None = None,
) -> tuple[list[PoLine], list[str], list[str]]:
    """PO ROW + COLOR SUMMARY 1개 페이지(여러 Color 포함 가능) 파싱.

    반환: (lines, critical_warnings, info_notes)
      - critical_warnings: 사이즈/수량 추출 자체가 실패한 경우 (FAIL 사유)
      - info_notes: Sub Channel 등 부가정보만 누락된 경우 (WARN 사유, Size/Qty에는 영향 없음)
    """
    warnings: list[str] = []
    info_notes: list[str] = []
    context = context if context is not None else {}
    pack_blocks = context.get("pack_blocks", {})

    extraction_sub_channel = (
        "EXT"
        if (
            header.selling_channel
            and header.selling_channel.strip().upper() == "STORE"
            and header.delivery_raw
            and "EXTRACTION" in header.delivery_raw.upper()
        )
        else None
    )
    if extraction_sub_channel and "sub_channel" not in context:
        context["sub_channel"] = extraction_sub_channel

    style_m = re.search(r"Style:\s*(\d+)\s*-\s*(.+)", page_text)
    if style_m:
        header.style_no = style_m.group(1)

    fac_m = re.search(r"Factory:\s*(\d+)\s*-\s*(.+)", page_text)
    if fac_m:
        header.factory_code = fac_m.group(1)
        header.factory_name = _normalize_factory_name(fac_m.group(2))

    store_matches = list(_STORE_TYPE_RE.finditer(page_text))
    store_m = store_matches[0] if store_matches else None
    if store_m:
        context["sub_channel"] = store_m.group(4)
        context["total_line_qty"] = _clean_num(store_m.group(3))
    sub_channel = context.get("sub_channel")
    total_line_qty = context.get("total_line_qty")
    is_prepack = bool(re.search(
        r"Start Ship Hand Over PPK Qty Total Line Qty PrePack SKU",
        page_text,
    ))
    has_store_header = "Start Ship Hand Over Total Line Qty Store Type Code" in page_text
    if not store_m and has_store_header and not is_prepack:
        info_notes.append("Store Type Code(Sub Channel)를 찾지 못한 페이지가 있습니다.")

    pack_m = re.search(
        r"^(?:(PRE)\s+Class|(?:PREPACK|BULK)\b)",
        page_text,
        re.MULTILINE,
    )
    if pack_m:
        context["pack_type"] = "PREPACK" if pack_m.group(1) == "PRE" else pack_m.group(0).split()[0]
    pack_type = context.get("pack_type", "")

    fob_m = re.search(r"First Cost:\s*\$\s*([\d.]+)", page_text)
    if fob_m:
        context["fob"] = float(fob_m.group(1))
    fob = context.get("fob")

    lines: list[PoLine] = []
    for m, segment_text in _extract_color_segments(page_text):
        color_code, color_name, design_color, total_color_qty, pairs = _parse_color_segment(
            m, segment_text, warnings
        )
        if total_color_qty is None or not pairs:
            continue

        color_pack_blocks = pack_blocks.get(color_code, {})
        color_size_totals = dict(pairs)
        line_specs = []  # (pack_type, hand_over, [(lbl, qty), ...], total_line_qty)
        block_sum: dict[str, int] = {}
        for kind in ("PREPACK", "BULK"):
            for sub in color_pack_blocks.get(kind, []):
                sub_pairs = sub.get("pairs")
                if sub_pairs is None:
                    continue
                for lbl, q in sub_pairs:
                    block_sum[lbl] = block_sum.get(lbl, 0) + q
                line_specs.append((kind, sub.get("hand_over"), sub_pairs, sub.get("total_line_qty")))

        # 블록(PO ROW) 단위 라벨/수량이 COLOR SUMMARY 합계와 정확히 일치할 때만
        # 사용한다 — 분할 출고(Hand Over가 다른 여러 PO ROW) 등 복잡한 케이스를
        # 정확히 반영하되, 어긋나면 안전하게 COLOR SUMMARY 기준 단일 라인으로
        # 폴백해 수량 정확성을 항상 보장한다.
        use_block_specs = bool(line_specs) and block_sum == color_size_totals
        if not use_block_specs:
            if color_pack_blocks:
                info_notes.append(
                    f"Color {color_code}: PO ROW 블록별 Hand Over/Pack 구분에 실패해 "
                    "대표 Hand Over로 처리했습니다 (Size 합계는 정상)."
                )
            line_specs = [(pack_type, header.hand_over, pairs, total_line_qty)]

        for line_pack_type, line_hand_over, size_pairs, line_total in line_specs:
            for lbl, q in size_pairs:
                std_size = SIZE_MAP.get(lbl)
                if std_size is None:
                    warnings.append(f"미매핑 사이즈 라벨 '{lbl}' 발견 (SIZE_MAP에 추가 필요).")
                    std_size = lbl
                lines.append(PoLine(
                    color_code=color_code,
                    color_name=color_name,
                    design_color=design_color,
                    sub_channel=sub_channel,
                    pack_type=line_pack_type,
                    fob=fob,
                    raw_size_label=lbl,
                    size_code=std_size,
                    qty=q,
                    total_color_qty=total_color_qty,
                    total_line_qty=line_total,
                    hand_over=line_hand_over or header.hand_over,
                ))

    # If the page starts the next product after its own COLOR SUMMARY, carry
    # that product's metadata to a continuation page.
    if store_matches:
        last_store = store_matches[-1]
        context["sub_channel"] = last_store.group(4)
        context["total_line_qty"] = _clean_num(last_store.group(3))

    return lines, warnings, info_notes


def parse_po_pdf(pdf_bytes: bytes, filename: str = "") -> ParsedPo:
    """PO PDF 1건 전체 파싱 (헤더 + 모든 Color/Size 라인)."""
    pages = extract_pages_text(pdf_bytes)
    header = parse_header(pages[0])
    header.hand_over, hand_over_note = _extract_hand_over(pages)
    if header.hand_over is None:
        raise PoParseError(
            f"{filename}: Hand Over 날짜를 찾지 못했습니다 — 납기준수일 확인이 필요합니다."
        )

    all_lines: list[PoLine] = []
    all_warnings: list[str] = []
    all_info_notes: list[str] = []
    if hand_over_note:
        all_info_notes.append(hand_over_note)
    context: dict = {}
    context["pack_blocks"] = _extract_pack_blocks(pages)
    for page_text in pages[1:]:
        if "COLOR SUMMARY" not in page_text:
            continue
        lines, warnings, info_notes = parse_line_page(page_text, header, context)
        all_lines.extend(lines)
        all_warnings.extend(warnings)
        all_info_notes.extend(info_notes)

    if not all_lines:
        raise PoParseError(
            f"{filename}: COLOR SUMMARY 라인을 하나도 못 찾았습니다 — 포맷 확인 필요."
        )

    return ParsedPo(header=header, lines=all_lines, warnings=all_warnings, info_notes=all_info_notes)
