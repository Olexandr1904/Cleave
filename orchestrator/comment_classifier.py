"""Comment classification — sends PR comments to the responder agent for judgment."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

VALID_CLASSIFICATIONS = {"AUTO_FIX", "AUTO_REJECT", "ESCALATE"}
VALID_VERDICTS = {"Valid", "Not valid"}


@dataclass
class ClassifiedComment:
    comment_id: int
    classification: str  # AUTO_FIX | AUTO_REJECT | ESCALATE
    reason: str
    suggested_fix: str = ""
    verdict: str = "Unsure"
    author: str = ""
    file: str = ""
    line: int | None = None
    body: str = ""
    code_context: str = ""


def parse_classifications(raw_output: str) -> list[ClassifiedComment]:
    """Parse agent's structured JSON output into ClassifiedComment list.

    If parsing fails or fields are missing, defaults to ESCALATE.
    """
    # Try to extract JSON from the output (agent might include text around it)
    text = raw_output.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse classification JSON")
        return []

    if not isinstance(data, list):
        logger.warning("Classification output is not a list")
        return []

    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        classification = item.get("classification", "ESCALATE")
        if classification not in VALID_CLASSIFICATIONS:
            classification = "ESCALATE"
        verdict = item.get("verdict", "")
        if verdict not in VALID_VERDICTS:
            logger.warning(
                "Comment %s missing or invalid verdict %r — defaulting to Unsure",
                item.get("comment_id"), verdict,
            )
            verdict = "Unsure"
        results.append(ClassifiedComment(
            comment_id=item.get("comment_id", 0),
            classification=classification,
            reason=item.get("reason", "classification missing"),
            suggested_fix=item.get("suggested_fix", ""),
            verdict=verdict,
        ))
    return results


async def classify_comments(
    comments: list[Any],
    workspace: Any,
    agent_runtime: Any,
) -> list[ClassifiedComment]:
    """Run the pr-comment-responder agent to classify PR comments.

    Returns ClassifiedComment list with original comment data attached.
    """
    comment_data = []
    for c in comments:
        comment_data.append({
            "comment_id": c.id,
            "author": c.author,
            "file": c.path,
            "line": c.line,
            "body": c.body,
        })

    result = await agent_runtime.execute(
        agent_id="pr-comment-responder-agent",
        workspace=workspace,
        extra_context={"pr_comments_json": json.dumps(comment_data, indent=2)},
    )

    if not result.success:
        logger.error("PR comment responder failed: %s", result.error)
        return [
            ClassifiedComment(
                comment_id=c.id, classification="ESCALATE",
                reason="Agent failed", author=c.author,
                file=c.path, line=c.line, body=c.body,
            )
            for c in comments
        ]

    classified = parse_classifications(result.output)

    # Attach original comment data
    comment_map = {c.id: c for c in comments}
    for cc in classified:
        orig = comment_map.get(cc.comment_id)
        if orig:
            cc.author = orig.author
            cc.file = orig.path
            cc.line = orig.line
            cc.body = orig.body

    return classified
