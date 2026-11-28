"""按检查任务列表调用对应工具，并把多项结果收成消费者沿用的一份分析结果。"""

from dataclasses import dataclass
from typing import Any

from common.errors import ProcessingError
from issue_gov.checks.base import parse_checks
from issue_gov.checks.registry import run_tasks
from issue_gov.checks.tools.compliance.run import analyze_issue, should_publish_comment

__all__ = ["analyze_issue", "run_checks", "should_publish_comment"]


@dataclass(frozen=True)
class CheckContext:
    """一次 Issue 读取后，各检查工具都能用的公共输入。不含某个工具的私有参数。"""

    title: str
    body: str
    template_files: list[dict[str, str]]
    agent_api_key: str
    model: str
    base_url: str
    repository: str
    number: int
    maintainers: tuple[str, ...]
    previously_commented: bool
    agent_api_keys: tuple[str, ...] | None
    llm_key_retries: int
    llm_key_retry_interval: float
    prior_snapshot: dict[str, Any] | None = None


def run_checks(
    title: str, body: str, template_files: list[dict[str, str]],
    agent_api_key: str, model: str = "", base_url: str = "", *,
    repository: str, number: int, maintainers: tuple[str, ...] = (),
    previously_commented: bool = False,
    agent_api_keys: tuple[str, ...] | None = None,
    llm_key_retries: int = 2,
    llm_key_retry_interval: float = 10,
    checks: Any = None,
    prior_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行消息中的检查列表。未带列表时只做规范性检查。

    只有一项时原样返回该工具的结果，和原先的单次分析相同。
    多项时按顺序拼接评论；任一项要求发布，则整体发布。
    """
    try:
        tasks = parse_checks(checks)
    except ValueError as exc:
        raise ProcessingError(str(exc), retryable=False) from None
    context = CheckContext(
        title, body, template_files, agent_api_key, model, base_url,
        repository, number, maintainers, previously_commented,
        agent_api_keys, llm_key_retries, llm_key_retry_interval,
        prior_snapshot,
    )
    return _combine(run_tasks(context, tasks))


def _combine(results: list[dict[str, Any]]) -> dict[str, Any]:
    """单项结果保持原结构。多项结果把各检查摘要放进 summary.checks。"""
    if len(results) == 1:
        return results[0]
    summaries = [item["summary"] for item in results]
    comments = [item["comment"] for item in results if item.get("comment")]
    suggestion = next((item["maintainer_suggestion"] for item in results if item.get("maintainer_suggestion")), None)
    recommendation = next((item.get("recommendation_status") for item in summaries if item.get("recommendation_status")), None)
    return {
        "summary": {"status": summaries[0].get("status"), "recommendation_status": recommendation, "checks": summaries},
        "comment": "\n\n".join(comments),
        "publish_comment": any(item.get("publish_comment") for item in results),
        "maintainer_suggestion": suggestion,
    }
