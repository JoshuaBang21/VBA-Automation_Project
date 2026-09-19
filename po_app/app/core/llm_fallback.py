"""
LLM 재파싱 Fallback (설계문서가 원래 채택한 방식) — 정규식이 실패했을 때만 호출.

비용/속도 최적화 원칙
---------------------
82건 표본에서 정규식+자기검증이 100% 통과했으므로, 매 PDF마다 LLM을 부르는 대신
"자기검증 실패(FAIL) 페이지"만 LLM에 넘긴다. 실패는 보통 0~2건/배치 수준일 것으로
예상되며, 이 경우에만 API 비용이 발생한다.

이 파일은 실제 운영 투입 전 채워야 하는 "확장 지점(extension point)"이다.
- API 키는 st.secrets 또는 환경변수(ANTHROPIC_API_KEY)로 주입
- 모델 출력은 반드시 JSON 스키마로 강제 (아래 PROMPT 참고)
- 반환된 JSON은 parser.py의 PoLine과 동일한 필드로 매핑해서 사용
"""
from __future__ import annotations

import json
import os

from .parser import PoLine

SYSTEM_PROMPT = """당신은 AEO(American Eagle Outfitters) Purchase Order PDF를 파싱하는 전문가입니다.
아래 페이지 텍스트에서 COLOR SUMMARY 블록을 찾아 각 사이즈별 수량을 추출하세요.

규칙:
- 반드시 COLOR SUMMARY의 "Total Color Qty"를 사용하세요 (PO ROW의 Total Line Qty 아님).
- Size 라벨은 국가 표기(US-/EU-/CA-/MX-)를 포함해 그대로 추출하세요.
- 출력은 JSON 배열만. 다른 텍스트, 설명, 코드블록 표시(```) 없이 순수 JSON만 반환하세요.

출력 스키마 (예시):
[
  {"color_code": "001", "color_name": "BLACK", "design_color": "BOLD BLACK C/O",
   "sizes": [{"label": "US - XS", "qty": 50}, {"label": "US - S", "qty": 119}],
   "total_color_qty": 1421}
]
"""


class LlmFallbackNotConfigured(Exception):
    """API 키가 없거나 모델 호출이 아직 구현되지 않았을 때."""


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def reparse_page_with_llm(page_text: str) -> list[PoLine]:
    """정규식 실패 페이지 1개를 LLM으로 재파싱.

    TODO(운영 투입 전 구현):
        from anthropic import Anthropic
        client = Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": page_text}],
        )
        raw = resp.content[0].text
        data = json.loads(raw)
        # data -> PoLine 리스트로 변환하는 로직 추가

    지금은 스캐폴드 단계이므로 미구현 상태를 명시적으로 알린다.
    """
    if not is_configured():
        raise LlmFallbackNotConfigured(
            "ANTHROPIC_API_KEY가 설정되지 않았습니다. LLM 재파싱을 쓰려면 "
            "환경변수를 설정하고 이 함수 본문(TODO)을 구현하세요."
        )
    raise NotImplementedError(
        "reparse_page_with_llm()은 아직 실제 API 호출이 연결되지 않은 스캐폴드입니다. "
        "docstring의 TODO를 참고해 구현하세요."
    )
