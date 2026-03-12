"""GitHub Issues 피드백 루프."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse


async def collect_github_issues(run_id: str) -> list[dict]:
    """gh issue list로 현재 run_id 관련 open 피드백 이슈 수집.

    Returns:
        [{number, title, body, labels, question_index, feedback_type}]
    """
    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "list",
        "--label", "feedback",
        "--state", "open",
        "--json", "number,title,body,labels",
        "--limit", "50",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []

    try:
        issues = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []

    results: list[dict] = []
    for issue in issues:
        body = issue.get("body", "")
        # run_id 매칭
        if run_id not in body:
            continue

        # 콘텐츠 인덱스 파싱: "콘텐츠 인덱스: N"
        idx_match = re.search(r"콘텐츠\s*인덱스\s*:\s*(\d+)", body)
        question_index = int(idx_match.group(1)) if idx_match else -1

        # 피드백 유형 분류 (labels에서)
        label_names = [lb.get("name", "") for lb in issue.get("labels", [])]
        if "feedback/seed-change" in label_names:
            feedback_type = "seed_change"
        elif "feedback/meta-change" in label_names:
            feedback_type = "meta_change"
        else:
            feedback_type = "other"

        results.append({
            "number": issue["number"],
            "title": issue["title"],
            "body": body,
            "labels": label_names,
            "question_index": question_index,
            "feedback_type": feedback_type,
        })

    return results


async def close_github_issue(number: int, comment: str) -> bool:
    """gh issue close {number} --comment {comment}."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "close", str(number),
        "--comment", comment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


def build_issue_url(
    owner: str,
    repo: str,
    keyword: str,
    publish_date: str,
    run_id: str,
    index: int,
) -> str:
    """GitHub Issue 생성 URL 조립 (대시보드 링크용)."""
    title = f"[피드백] {keyword} — {publish_date}"
    body = (
        f"## 콘텐츠 피드백\n\n"
        f"- **키워드**: {keyword}\n"
        f"- **발행일**: {publish_date}\n"
        f"- **Run ID**: {run_id}\n"
        f"- **콘텐츠 인덱스**: {index}\n\n"
        f"### 피드백 유형\n\n"
        f"- [ ] 시드 키워드 변경 (seed-change)\n"
        f"- [ ] 메타 정보 변경 (meta-change)\n"
        f"- [ ] 기타\n\n"
        f"### 상세 내용\n\n"
        f"여기에 피드백을 작성해 주세요.\n"
    )
    params = urllib.parse.urlencode({
        "title": title,
        "body": body,
        "labels": "feedback",
    })
    return f"https://github.com/{owner}/{repo}/issues/new?{params}"


async def get_repo_info() -> tuple[str, str]:
    """git remote에서 owner/repo 추출.

    Returns:
        (owner, repo) 또는 ("", "") on failure.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "remote", "get-url", "origin",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return ("", "")

    remote_url = stdout.decode(errors="replace").strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote_url)
    if not m:
        return ("", "")
    return (m.group(1), m.group(2))
