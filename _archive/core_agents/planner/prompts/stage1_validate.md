# Stage 1 — 카테고리 매핑 검증

## 역할

1차 매핑 결과를 검토하여, 카테고리와 파생 질문이 적합하게 연결되었는지 검증하고 오류를 교정한다.

## 입력

- `categories`: 사용자 입력 카테고리 목록 — **이 원문이 판단의 유일한 기준이다**
- `clusters`: 클러스터 요약 (cluster_id, representative_keyword, paa_questions, naver_top_titles)
- `mappings`: 1차 매핑 결과 (cluster_id, category, derived_question)

## 검증 기준

각 매핑에 대해 아래 3가지 기준을 순서대로 확인한다.

### 기준 1: 도메인 명백 불일치 (reassign 유일 근거)

`derived_question`의 주제 도메인이 배정된 `category`의 도메인과 **명백히** 다른가?

- 판단 방법: `category` 원문에 등장하는 핵심 도메인 명사(예: "ERP", "앱 개발", "견적")가 `derived_question`에 전혀 없고, 다른 카테고리의 도메인과 더 명확하게 일치하면 **reassign**이다.
- **경계 케이스(어느 쪽으로도 볼 수 있는 경우)는 reassign하지 않는다.** 기준 1 통과로 처리한다.

### 기준 2: 독자 의도 연결

`derived_question`이 `category` 원문을 읽는 독자가 다음에 자연스럽게 찾아볼 만한 질문인가?

- `category` 원문 자체를 판단 기준으로 삼는다. 외부 예시나 사전 정의된 적합 주제 목록은 없다.
- 기준 1을 통과했지만 독자 의도와 다소 동떨어진 경우 → **rewrite** (카테고리 유지, 질문만 교정)
- **기준 2 실패만으로는 reassign하지 않는다.**

### 기준 3: 같은 카테고리 내 각도 중복

같은 `category`에 배정된 `derived_question`들 중 실질적으로 동일한 각도(주제+관점)가 2개 이상 있는가?

- 예: "외주 개발 위험 요소와 예방책"과 "외주 개발 손실 원인과 예방책" → 각도가 거의 동일 → 하나를 **rewrite**
- 각도가 중복되면 카테고리는 유지하고 질문만 다른 하위 주제로 교정한다.

## 처리 규칙

| 상황 | verdict | 필수 필드 |
|---|---|---|
| 3가지 기준 모두 통과 | `ok` | 없음 |
| **기준 1 실패** — 더 적합한 카테고리 있음 | `reassign` | corrected_category, corrected_question |
| **기준 1 실패** — 적합한 카테고리 없음 | `reassign` | corrected_category: "unassigned", corrected_question |
| 기준 2 또는 3 실패 | `rewrite` | corrected_question |

- `unassigned` 카테고리로 배정된 클러스터는 검증 대상에서 제외하고 verdict: `ok`로 처리한다.
- reassign 시 `corrected_category`는 반드시 입력 `categories` 원문 또는 `"unassigned"`만 허용한다.

## 출력 형식

반드시 아래 JSON 객체만 반환한다. 설명, 마크다운 코드 블록 없이 JSON만 반환한다.

{
  "validations": [
    {
      "cluster_id": "<cluster_id>",
      "verdict": "ok"
    },
    {
      "cluster_id": "<cluster_id>",
      "verdict": "reassign",
      "issue": "<도메인 명백 불일치 이유를 1~2문장으로>",
      "corrected_category": "<교정 카테고리 원문 또는 unassigned>",
      "corrected_question": "<교정된 카테고리에 맞는 새 파생 질문>"
    },
    {
      "cluster_id": "<cluster_id>",
      "verdict": "rewrite",
      "issue": "<독자 의도 불일치 또는 각도 중복 이유를 1~2문장으로>",
      "corrected_question": "<같은 카테고리 안에서 다른 각도로 재작성된 파생 질문>"
    }
  ]
}

### 출력 제약

- 입력의 모든 `cluster_id`가 반드시 `validations`에 1개씩 포함되어야 한다.
- `verdict: ok`인 경우 `issue`, `corrected_category`, `corrected_question` 필드는 생략한다.
- `corrected_question`은 반드시 질문 형태로 끝나야 한다.
