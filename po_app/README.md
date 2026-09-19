# PO 검증 대시보드 (Streamlit 스캐폴드)

`PO_PARSING_DESIGN.md` 설계문서 10번(검증 UI/CFM)·14번(개발순서)을 그대로 구현한
로컬 Python 앱입니다.

## 아키텍처 요약

```
app/
  app.py                 <- Streamlit UI (업로드 → 대시보드 → CFM → 저장)
  core/
    constants.py         <- common.py 성격: Channel/Flow/SIZE_MAP, 임계값
    parser.py            <- 1차 파싱 (정규식, "빠른 경로")
    validator.py          <- 3단 자기검증 + 업차지(L 포함 이상) 체크
    llm_fallback.py       <- 정규식 실패 시에만 쓰는 LLM 재파싱 훅 (TODO, 스캐폴드)
    storage.py            <- SQLite: my_po_header / my_po_line
requirements.txt
```

## 왜 "정규식 우선 + LLM은 예외처리용"인가

설계문서는 LLM 파싱을 채택했지만, 실제 표본(15~82건)을 정규식으로 파싱한 결과
3단 자기검증을 100% 통과했습니다. AEO PO는 격자(box) 양식이 고정적이라
정규식만으로도 충분히 정밀합니다.

- 포맷이 흔들리면 → 자기검증(9번)이 즉시 잡아냄 (Size합≠Total Color Qty 등)
- 검증 실패 페이지만 `llm_fallback.py`로 넘겨 재파싱 (비용/속도 최적화)
- `llm_fallback.py`는 현재 **스캐폴드 상태**입니다. 운영 투입 전 `reparse_page_with_llm()`
  안의 TODO(Anthropic API 호출)를 구현하고, `ANTHROPIC_API_KEY` 환경변수를 설정하세요.

## 실행 방법

```bash
cd po_app
pip install -r requirements.txt
streamlit run app/app.py
```

브라우저가 자동으로 열리며 (기본 http://localhost:8501), PO PDF를 업로드하면
또는 사이드바의 네트워크 PO 폴더 검색 기능으로 PDF를 선택하면:

1. 정규식 파싱 (Header + Color×Size Line)
2. 자기검증 3단 (Size합 / Color합 / OrderSummary) → 🟢/🟡/🔴 표시
3. Color×Size 표 + 업차지(L 포함 이상 비율) 표, 불일치 행 빨간색 하이라이트
4. 파싱·검증이 끝나면 자동 저장 (`validated=0`인 검증 실패 PO도 저장)
5. 같은 PO의 PDF가 변경되면 최신 데이터로 덮어쓰고, 오래된 PDF는 저장하지 않으며 원본 경로·파일 수정일시·저장일시를 이력에 표시

화면의 `현재 화면 PO만 Excel로 다운로드` 버튼은 현재 선택·검증된 PO만
`current_po.xlsx`로 다운로드합니다. 저장된 PO 이력 하단의
`전체 누적 PO를 Excel로 다운로드` 버튼은 지금까지 저장된 최신 PO 전체를
`MMDDYY_TOTAL_PO.xlsx` 형식으로 다운로드합니다. 두 파일 모두 `PO_Header`, `PO_Line`,
`PO_Validation`, `Upcharge_Review` 시트를 포함합니다.
`PO_Header`에는 PO의 Hand Over(포워더 전달 납기준수일)가 `total_order_units` 다음
컬럼에 `mm/dd/yy` 형식으로 저장됩니다. Hand Over 날짜가 없는 PDF는 저장하지 않고
확인 오류로 표시합니다. PO 내 Color/pack별 Hand Over 날짜가 서로 다르면
PO 단일 Header로 저장할 수 없으므로 별도 확인이 필요합니다.

### 네트워크 PO 폴더에서 추가

사이드바의 **PO 폴더 경로**에 기본값으로 아래 경로가 입력되어 있습니다.

```text
\\Diskstation\영업4부\▶▶AEO MENS CUT&SEW◀◀\PO
```

`🔎 네트워크 폴더 검색`을 누르면 하위 폴더의 PDF를 최근 수정 순으로 검색합니다.
검색 결과에서 처리할 파일을 선택하면 업로드한 PDF와 동일하게 파싱·검증할 수 있습니다.
네트워크 공유에 접근할 수 있는 Windows 계정으로 Streamlit을 실행해야 합니다.

## 알아둘 점 / 다음 단계

- 이 스캐폴드는 **오늘 Claude Project 창에서 검증한 파싱 로직을 그대로 이식**한 것입니다.
  실제 PDF(zip이 아닌 순수 PDF 바이너리)에서는 `pdfplumber`의 텍스트 추출 결과가
  이번 프로젝트 파일과 미세하게 다를 수 있으므로, 처음 몇 건은 반드시 실제 PDF로
  자기검증 결과를 재확인하세요.
- `llm_fallback.py`는 아직 실제 API 호출이 연결되지 않았습니다 — 지금 상태로는
  정규식이 실패하면 경고만 뜨고 해당 Color/페이지는 라인에서 빠집니다.
- 운영 규모가 커지면 `storage.py`만 Postgres 등으로 교체하면 되고, 나머지 코드는
  영향받지 않도록 인터페이스(`save_po`, `load_all_headers`, `load_lines`)를
  계약(contract)으로 유지했습니다.
- L 포함 이상(업차지) 임계값은 사이드바에서 30~70% 범위로 즉시 조정 가능합니다
  (기본값 50%, 설계문서 원안은 51%).
