"""
PO 검증 대시보드 (설계문서 10번 절차의 Streamlit 구현)

PO PDF 업로드
    |
정규식 파싱 (실패 시에만 LLM fallback 훅) -> Header + Size별 Line
    |
자동 검증 (Size합 / Color합 / OrderSummary)   <- 설계문서 9번
    |
Dashboard 경고등 (초록/빨강) + 이상 라인 하이라이트
    |
사람이 CFM(확인)
    |
save_po() -> my_po_header + my_po_line (SQLite)

실행:
    cd po_app
    pip install -r requirements.txt
    streamlit run app/app.py
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

# 이 파일(app/app.py)이 있는 폴더 자체를 sys.path 맨 앞에 둔다.
# (streamlit run app/app.py 로 실행하면 러너가 이미 이 폴더를 sys.path에 넣는데,
#  하필 폴더명이 "app"이라 `import app.core...`와 충돌한다. 그래서 패키지 이름을
#  "app.core"가 아니라 "core"로 두고, 항상 이 폴더를 기준으로 절대경로를 넣어
#  실행 위치(cwd)나 러너의 sys.path 삽입 방식에 관계없이 동일하게 동작하게 한다.)
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from core.constants import DEFAULT_UPCHARGE_THRESHOLD, SIZE_ORDER
from core.parser import PoParseError, parse_po_pdf
from core.storage import (
    load_all_headers,
    load_all_lines,
    load_all_upcharge,
    load_all_validation,
    save_po,
    search_style_summary,
)
from core.validator import upcharge_check, validate


def _format_hand_over(value: str | None) -> str | None:
    if not value:
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%m/%d/%y")
    except ValueError:
        return value


def _format_excel_fob(writer: pd.ExcelWriter) -> None:
    """Keep FOB values visible with two decimal places in exported workbooks."""
    worksheet = writer.sheets.get("PO_Line")
    if worksheet is None:
        return
    fob_column = next(
        (
            cell.column
            for cell in worksheet[1]
            if cell.value == "fob"
        ),
        None,
    )
    if fob_column is None:
        return
    for row in worksheet.iter_rows(
        min_row=2,
        min_col=fob_column,
        max_col=fob_column,
    ):
        row[0].number_format = "0.00"


st.set_page_config(page_title="PO 검증 대시보드", layout="wide")
st.markdown(
    """
    <style>
    [data-testid="stMetricValue"] {
        font-size: 1rem !important;
        font-weight: 400 !important;
        line-height: 1.25 !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 0.875rem !important;
        font-weight: 400 !important;
    }
    [data-testid="stAlert"] {
        padding: 0.45rem 1rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# 사이드바 설정
# ---------------------------------------------------------------------------
st.sidebar.title("⚙️ 설정")
threshold_pct = st.sidebar.slider(
    "업차지 검토 임계값 (L 포함 이상 사이즈 비율)",
    min_value=30, max_value=70, value=int(DEFAULT_UPCHARGE_THRESHOLD * 100), step=1,
)
threshold = threshold_pct / 100
st.sidebar.markdown("---")
st.sidebar.caption(
    "정규식 파싱이 우선 적용되며, 자기검증에 실패한 항목만 LLM 재파싱 대상으로 "
    "표시됩니다 (app/core/llm_fallback.py 참고, 현재는 스캐폴드 상태)."
)


st.title("📦 PO 검증 대시보드 — S1539 등 AEO PO")
st.caption(
    "설계 기준: PO_PARSING_DESIGN.md · 수량은 COLOR SUMMARY의 Total Color Qty만 사용, "
    "EDI 수량검증은 하지 않음 (PO 자기검증 3단)."
)

if "upload_widget_version" not in st.session_state:
    st.session_state.upload_widget_version = 0

uploaded_files = st.file_uploader(
    "PO PDF 업로드 (여러 건 동시 가능)",
    type=["pdf"],
    accept_multiple_files=True,
    key=f"uploaded_po_files_{st.session_state.upload_widget_version}",
)
st.markdown(
    """
    <style>
    [data-testid="stFileUploaderFile"] {
        display: none;
    }
    [data-testid="stFileUploaderPagination"] {
        display: none;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if uploaded_files:
    st.markdown("**선택된 PO PDF**")
    displayed_files = min(len(uploaded_files), 20)
    for row in range(5):
        columns = st.columns(4)
        for column in range(4):
            file_index = column * 5 + row
            if file_index < displayed_files:
                columns[column].caption(uploaded_files[file_index].name)
    if len(uploaded_files) > 20:
        st.warning("처음 20개만 목록에 표시되지만, 선택한 PDF는 모두 처리됩니다.")

if "results" not in st.session_state:
    st.session_state.results = {}  # po_no -> {parsed, result, df, upcharge_df}
if "confirmed" not in st.session_state:
    st.session_state.confirmed = set()
if "processed_upload_hashes" not in st.session_state:
    st.session_state.processed_upload_hashes = set()


# ---------------------------------------------------------------------------
# 파싱 + 검증
# ---------------------------------------------------------------------------
def _read_uploaded_file(file_obj) -> tuple[bytes, str, str | None]:
    return file_obj.getvalue(), file_obj.name, None


sources: list[tuple[bytes, str, str | None]] = []
if uploaded_files:
    sources.extend(_read_uploaded_file(f) for f in uploaded_files)

if sources:
    for pdf_bytes, filename, source_modified_at in sources:
        file_hash = hashlib.sha256(pdf_bytes).hexdigest()
        if file_hash in st.session_state.processed_upload_hashes:
            continue
        try:
            parsed = parse_po_pdf(pdf_bytes, filename=filename)
            result, df = validate(parsed)
            up_df = upcharge_check(df, threshold)
            header_fields = {
                "po_no": parsed.header.po_no,
                "style_no": parsed.header.style_no,
                "factory_code": parsed.header.factory_code,
                "factory_name": parsed.header.factory_name,
                "channel_type": parsed.header.channel_type,
                "selling_channel": parsed.header.selling_channel,
                "flow_type": parsed.header.flow_type,
                "floorset": parsed.header.floorset,
                "total_order_units": parsed.header.total_order_units,
                "hand_over": parsed.header.hand_over,
            }
            save_status = save_po(
                df_line=df,
                header_fields=header_fields,
                validated=(result.status == "OK"),
                confirmed_by="auto",
                source_path=filename,
                source_modified_at=source_modified_at,
                file_hash=file_hash,
                validation_result=result,
                upcharge_df=up_df,
            )
            st.session_state.results[parsed.header.po_no] = {
                "parsed": parsed, "result": result, "df": df,
                "upcharge_df": up_df, "filename": Path(filename).name,
                "save_status": save_status,
                "source_modified_at": source_modified_at,
            }
            st.session_state.processed_upload_hashes.add(file_hash)
        except PoParseError as e:
            message = str(e)
            prefix = f"{Path(filename).name}: "
            if message.startswith(prefix):
                message = message[len(prefix):]
            st.error(f"❌ {Path(filename).name}: {message}")
            st.session_state.processed_upload_hashes.add(file_hash)

has_results = bool(st.session_state.results)
if not has_results:
    st.info("PO PDF를 업로드하면 자동으로 파싱·검증됩니다.")


# ---------------------------------------------------------------------------
# 상단 요약 대시보드 (경고등)
# ---------------------------------------------------------------------------
if has_results:
    def _reset_po_upload():
        st.session_state.results = {}
        st.session_state.confirmed = set()
        st.session_state.processed_upload_hashes = set()
        st.session_state.upload_widget_version += 1


    st.button("초기화", key="reset_po_upload", on_click=_reset_po_upload)
    st.subheader("1️⃣ 검증 요약")

    summary_rows = []
    for po_no, item in st.session_state.results.items():
        r = item["result"]
        icon = {"OK": "🟢", "WARN": "🟡", "FAIL": "🔴"}[r.status]
        size_level_ok = all(c.ok for c in r.color_checks) if r.color_checks else False
        summary_rows.append({
            "상태": icon,
            "PO번호": po_no,
            "데이터 상태": {
                "saved": "최신 저장",
                "unchanged": "최신 유지",
                "old": "이전 파일 - 저장 안 함",
            }.get(item.get("save_status"), "확인 필요"),
            "PDF 수정일시": item.get("source_modified_at"),
            "Style": item["parsed"].header.style_no,
            "HO": _format_hand_over(item["parsed"].header.hand_over),
            "Color 수": len(r.color_checks),
            "COLOR SUMMARY 합": r.sum_of_colors,
            "TOTAL ORDER SUMMARY": r.order_units,
            "Size별합계일치": "OK" if size_level_ok else "불일치",
            "PO전체합계일치": "OK" if r.color_sum_vs_order_ok else "불일치",
            "확인(CFM) 완료": "✅" if po_no in st.session_state.confirmed else "",
            "파일명": item["filename"],
        })

    summary_df = pd.DataFrame(summary_rows)


    def _highlight_status(row):
        # 기본 바탕은 검은색 + 흰 글씨, 상태(🟡/🔴)만 눈에 띄게 색 강조
        status_bg = {"🟢": "#000000", "🟡": "#7A5B00", "🔴": "#7A0010"}.get(row["상태"], "#000000")
        return [f"background-color: {status_bg}; color: #FFFFFF;"] * len(row)


    st.dataframe(
        summary_df.style.set_table_styles(
            [{"selector": "th", "props": [("text-align", "center")]}]
        ).set_properties(
            **{"background-color": "#000000", "color": "#FFFFFF", "text-align": "center"}
        ).format(
            {
                "COLOR SUMMARY 합": "{:,.0f}",
                "TOTAL ORDER SUMMARY": "{:,.0f}",
            },
            na_rep="",
        )
                         .set_properties(
                             subset=["COLOR SUMMARY 합", "TOTAL ORDER SUMMARY"],
                             **{"text-align": "right"},
                         )
                         .set_properties(
                             subset=[
                                 "Style",
                                 "HO",
                                 "Color 수",
                                 "Size별합계일치",
                                 "PO전체합계일치",
                             ],
                             **{"text-align": "center"},
                         )
                         .apply(_highlight_status, axis=1),
        hide_index=True,
        use_container_width=True,
    )

    n_fail = sum(1 for it in st.session_state.results.values() if it["result"].status != "OK")
    if n_fail:
        st.warning(f"🔴 {n_fail}건이 자기검증 실패/경고 상태입니다. 아래에서 상세 확인하세요.")
    else:
        st.success("🟢 전체 PO가 자기검증을 통과했습니다.")

    # 전체 PO 통합 Color×Size CSV 다운로드 (PO별이 아니라 업로드된 전체 PO를 한 파일로)
    all_lines_df = pd.concat(
        [item["df"].assign(파일명=item["filename"]) for item in st.session_state.results.values()],
        ignore_index=True,
    )
    st.download_button(
        "📥 전체 PO 통합 Color×Size CSV 다운로드",
        data=all_lines_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="ALL_PO_color_size.csv",
        mime="text/csv",
        key="dl_all_po",
    )

    current_validation_rows = []
    current_upcharge_frames = []
    for po_no, item in st.session_state.results.items():
        parsed = item["parsed"]
        result = item["result"]
        for check in result.color_checks:
            current_validation_rows.append({
                "po_no": po_no,
                "color_code": check.color_code,
                "color_name": check.color_name,
                "size_sum": check.size_sum,
                "total_color_qty": check.total_color_qty,
                "size_check_ok": check.ok,
                "color_sum_vs_order_ok": result.color_sum_vs_order_ok,
                "sum_of_colors": result.sum_of_colors,
                "order_units": result.order_units,
                "status": result.status,
                "parse_warnings": "\n".join(result.parse_warnings),
                "info_notes": "\n".join(result.info_notes),
            })
        if not item["upcharge_df"].empty:
            current_upcharge_frames.append(item["upcharge_df"])

    current_headers = pd.DataFrame([
        {
            "po_no": po_no,
            "style_no": item["parsed"].header.style_no,
            "factory_code": item["parsed"].header.factory_code,
            "factory_name": item["parsed"].header.factory_name,
            "channel_type": item["parsed"].header.channel_type,
            "selling_channel": item["parsed"].header.selling_channel,
            "flow_type": item["parsed"].header.flow_type,
            "floorset": item["parsed"].header.floorset,
            "hand_over": _format_hand_over(item["parsed"].header.hand_over),
            "total_order_units": item["parsed"].header.total_order_units,
            "validation_status": item["result"].status,
            "filename": item["filename"],
            "save_status": item.get("save_status"),
            "source_modified_at": item.get("source_modified_at"),
        }
        for po_no, item in st.session_state.results.items()
    ])
    current_lines = pd.concat(
        [
            item["df"].assign(filename=item["filename"])
            for item in st.session_state.results.values()
        ],
        ignore_index=True,
    )
    current_validation = pd.DataFrame(current_validation_rows)
    current_upcharge = (
        pd.concat(current_upcharge_frames, ignore_index=True)
        if current_upcharge_frames else pd.DataFrame()
    )
    current_excel = BytesIO()
    with pd.ExcelWriter(current_excel, engine="openpyxl") as writer:
        current_headers.to_excel(writer, sheet_name="PO_Header", index=False)
        current_lines.to_excel(writer, sheet_name="PO_Line", index=False)
        current_validation.to_excel(writer, sheet_name="PO_Validation", index=False)
        current_upcharge.to_excel(writer, sheet_name="Upcharge_Review", index=False)
        _format_excel_fob(writer)

    # ---------------------------------------------------------------------------
    # PO별 상세 + CFM
    # ---------------------------------------------------------------------------
    st.subheader("2️⃣ PO별 상세 확인 및 CFM")

    for po_no, item in st.session_state.results.items():
        parsed, result, df, up_df = item["parsed"], item["result"], item["df"], item["upcharge_df"]
        icon = {"OK": "🟢", "WARN": "🟡", "FAIL": "🔴"}[result.status]

        with st.expander(f"{icon} PO {po_no} — {parsed.header.style_no or '?'} "
                          f"({parsed.header.selling_channel})"):

            col1, col2 = st.columns([2, 1])

            with col1:
                st.markdown("**Color × Size 수량 (Line 레벨)**")
                pivot = df.pivot_table(
                    index=["color_code", "color_name"], columns="size_code",
                    values="qty", aggfunc="sum", fill_value=0,
                )
                ordered_cols = [c for c in SIZE_ORDER if c in pivot.columns]
                pivot = pivot[ordered_cols]
                pivot["합계(계산)"] = pivot.sum(axis=1)

                # 원본 Total Color Qty 대조 + 하이라이트
                orig_totals = df.drop_duplicates("color_code").set_index("color_code")["total_color_qty"]
                pivot_reset = pivot.reset_index()
                pivot_reset["TotalColorQty(원본)"] = pivot_reset["color_code"].map(orig_totals)
                pivot_reset["일치"] = pivot_reset["합계(계산)"] == pivot_reset["TotalColorQty(원본)"]

                def _hl(row):
                    bg = "#000000" if row["일치"] else "#7A0010"
                    return [f"background-color: {bg}; color: #FFFFFF;"] * len(row)

                st.dataframe(
                    pivot_reset.style.set_properties(**{"background-color": "#000000", "color": "#FFFFFF"})
                                      .apply(_hl, axis=1),
                    use_container_width=True, hide_index=True,
                )

                if df["hand_over"].nunique(dropna=True) > 1:
                    st.markdown("**🟡 분할 출고(Hand Over) — Color × 배송분별 수량**")
                    ho_pivot = df.groupby(
                        ["color_code", "color_name", "hand_over"], as_index=False
                    )["qty"].sum()
                    ho_pivot["hand_over"] = ho_pivot["hand_over"].map(_format_hand_over)
                    ho_pivot = ho_pivot.rename(columns={
                        "color_code": "Color Code",
                        "color_name": "Color Name",
                        "hand_over": "HO",
                        "qty": "수량",
                    }).sort_values(["Color Code", "HO"])
                    st.dataframe(ho_pivot, use_container_width=True, hide_index=True)

            with col2:
                size_level_ok = all(c.ok for c in result.color_checks) if result.color_checks else False
                validation_summary, validation_checks = st.columns([1, 1])
                with validation_summary:
                    st.markdown("**PO 자기검증**")
                    st.metric("COLOR SUMMARY 합계", result.sum_of_colors)
                    st.metric("TOTAL ORDER SUMMARY", result.order_units)
                with validation_checks:
                    st.markdown("&nbsp;", unsafe_allow_html=True)
                    st.metric(
                        "Size별 합계 일치 (Color별)",
                        "OK ✅" if size_level_ok else "불일치 ❌",
                    )
                    st.metric(
                        "PO 전체 합계 일치 (Order Summary)",
                        "OK ✅" if result.color_sum_vs_order_ok else "불일치 ❌",
                    )

                if result.parse_warnings:
                    st.markdown("**🔴 파싱 실패 (FAIL — Size/Qty 추출 자체가 안 됨, LLM 재파싱 필요)**")
                    for w in result.parse_warnings:
                        st.write(f"- {w}")
                if result.info_notes:
                    st.markdown("**🟡 참고 (WARN — Size/Qty 정확성과는 무관)**")
                    for w in result.info_notes:
                        st.write(f"- {w}")

            st.markdown(f"**업차지 검토 (L 포함 이상 사이즈 비율 > {threshold_pct}%)**")
            override = False
            upcharge_table, cfm_action = st.columns([5, 1])
            if not up_df.empty:
                flagged = up_df[up_df["needs_upcharge_review"]]
                up_show = up_df.copy()
                up_show["over_l_ratio"] = (up_show["over_l_ratio"] * 100).round(1).astype(str) + "%"

                def _hl_up(row):
                    is_flag = row["needs_upcharge_review"]
                    bg = "#000000"
                    fg = "#FFFFFF"
                    if is_flag:
                        return [f"background-color: {bg}; color: {fg};" for _ in row]
                    return [f"background-color: {bg}; color: {fg};" for _ in row]

                with upcharge_table:
                    st.dataframe(
                        up_show.style.apply(_hl_up, axis=1),
                        use_container_width=True, hide_index=True,
                    )
                if len(flagged):
                    with upcharge_table:
                        st.warning(
                            f"⚠️ {len(flagged)}개 Color가 업차지 검토 대상입니다 "
                            f"({threshold_pct}% 초과)."
                        )
            else:
                with upcharge_table:
                    st.write("데이터 없음")

            with cfm_action:
                save_labels = {
                    "saved": "자동 저장 완료",
                    "unchanged": "최신 데이터 유지",
                    "old": "이전 파일 - 저장 안 함",
                }
                st.caption(save_labels.get(item.get("save_status"), "저장 상태 확인"))


# ---------------------------------------------------------------------------
# 저장된 PO 조회 (my_po_header) / Style·HO 검색
# ---------------------------------------------------------------------------
tab_history, tab_search = st.tabs(["3️⃣ 저장된 PO 이력", "🔍 Style/HO 검색 (TEST)"])

with tab_history:
    try:
        headers_df = load_all_headers()
        if headers_df.empty:
            st.caption("아직 저장된 PO가 없습니다.")
        else:
            headers_display_df = headers_df.copy()
            headers_display_df["hand_over"] = pd.to_datetime(
                headers_display_df["hand_over"], format="%Y-%m-%d", errors="coerce"
            )
            st.dataframe(
                headers_display_df.style.set_table_styles(
                    [{"selector": "th", "props": [("text-align", "center")]}]
                ).set_properties(
                    **{"text-align": "center"}
                ).set_properties(
                    subset=["total_order_units"],
                    **{"text-align": "right"},
                ),
                use_container_width=True,
            )
            lines_df = load_all_lines()
            validation_df = load_all_validation()
            upcharge_df = load_all_upcharge()
            headers_excel_df = headers_df.copy()
            headers_excel_df["hand_over"] = headers_excel_df["hand_over"].map(
                _format_hand_over
            )
            excel_buffer = BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                headers_excel_df.to_excel(writer, sheet_name="PO_Header", index=False)
                lines_df.to_excel(writer, sheet_name="PO_Line", index=False)
                validation_df.to_excel(writer, sheet_name="PO_Validation", index=False)
                upcharge_df.to_excel(writer, sheet_name="Upcharge_Review", index=False)
                _format_excel_fob(writer)
            if has_results:
                current_download, saved_download = st.columns(2)
            else:
                saved_download = st.container()
            if has_results:
                with current_download:
                    st.download_button(
                        "📄 현재 화면 PO만 Excel로 다운로드",
                        data=current_excel.getvalue(),
                        file_name="current_po.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="download_current_po_excel",
                        use_container_width=True,
                    )
            with saved_download:
                st.download_button(
                    "📚 전체 누적 PO를 Excel로 다운로드",
                    data=excel_buffer.getvalue(),
                    file_name=f"{datetime.now().strftime('%m%d%y')}_TOTAL_PO.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="download_saved_po_excel",
                    use_container_width=True,
                )
    except Exception as e:  # DB 파일이 아직 없을 때 등
        st.caption(f"저장 이력 없음 ({e})")

with tab_search:
    st.caption("Style과 Hand Over 기간으로 저장된 PO를 검색해 Color·납기별 수량을 요약합니다.")
    search_style_no = st.text_input("Style 번호", key="search_style_no")
    ho_range = st.date_input(
        "HO 기간 (비워두면 전체 기간)",
        value=(),
        key="search_ho_range",
    )

    if st.button("검색", key="search_style_summary_btn", use_container_width=True):
        if not search_style_no.strip():
            st.warning("Style 번호를 입력해 주세요.")
        else:
            ho_start = ho_end = None
            if isinstance(ho_range, (list, tuple)):
                if len(ho_range) >= 1 and isinstance(ho_range[0], date):
                    ho_start = ho_range[0].strftime("%Y-%m-%d")
                if len(ho_range) >= 2 and isinstance(ho_range[1], date):
                    ho_end = ho_range[1].strftime("%Y-%m-%d")
            elif isinstance(ho_range, date):
                ho_start = ho_end = ho_range.strftime("%Y-%m-%d")

            summary_df = search_style_summary(
                search_style_no.strip(), ho_start=ho_start, ho_end=ho_end
            )
            st.session_state["style_summary_result"] = summary_df

    summary_df = st.session_state.get("style_summary_result")
    if summary_df is not None:
        if summary_df.empty:
            st.caption("검색 결과가 없습니다.")
        else:
            summary_display_df = summary_df.copy()
            summary_display_df["hand_over"] = summary_display_df["hand_over"].map(
                _format_hand_over
            )
            summary_display_df = summary_display_df.rename(
                columns={
                    "style_no": "Style",
                    "color_code": "Color Code",
                    "color_name": "Color Name",
                    "hand_over": "HO",
                    "po_count": "PO 건수",
                    "total_qty": "수량",
                }
            )
            st.dataframe(
                summary_display_df.style.set_table_styles(
                    [{"selector": "th", "props": [("text-align", "center")]}]
                ).set_properties(
                    **{"text-align": "center"}
                ).set_properties(
                    subset=["수량"],
                    **{"text-align": "right"},
                ),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                f"총 수량: {int(summary_df['total_qty'].sum()):,}  "
                f"|  PO 건수(중복포함): {int(summary_df['po_count'].sum()):,}"
            )

