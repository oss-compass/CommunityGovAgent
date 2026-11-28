"""规范性检查工具：按解析、核对、推荐、评论四个模块完成一次检查。"""

import json
import logging
import time
from typing import Any

import yaml

from common.errors import ProcessingError
from issue_gov.checks.base import COMPLIANCE, is_default_checks
import issue_gov.checks.tools.compliance.assess as compliance_assess
import issue_gov.checks.tools.compliance.check as compliance_check
import issue_gov.checks.tools.compliance.comment as compliance_comment
import issue_gov.checks.tools.compliance.parse as compliance_parse
import issue_gov.checks.tools.compliance.recommend as compliance_recommend

LOG = logging.getLogger("issue-gov-webhook")


def should_publish_comment(summary: dict, *, previously_commented: bool = False) -> bool:
    """根据分析摘要决定是否允许新增或更新评论。

    Args:
        summary: 分析摘要，包含 status 和 recommendation_status。
        previously_commented: 此前是否已有检查评论或待确认的评论发送记录。
    Returns:
        模板配置故障始终为 False；通过检查时仅在此前有评论时为 True；未通过时为 True。
    """
    if summary.get("status") == "template_error":
        return False
    # 通过检查时，若此前已有评论，则通知“检查通过”，否则不新增评论
    if summary.get("status") == "pass" or (
            summary.get("status") == "no_match" and summary.get("recommendation_status") == "not_applicable"):
        return previously_commented
    # 检查未通过，需要发评论
    return True


def _call_model(operation, agent_api_key: str, model: str, base_url: str,
                agent_api_keys: tuple[str, ...] | None, retries: int, interval: float):
    """调用模型。未提供密钥列表时只调用一次；提供时按密钥轮换。

    operation 接收当前密钥、模型名和接口地址。鉴权失败立即换下一个密钥。
    超时和进程启动失败按间隔重试当前密钥，用尽后再切换。结果格式不对只重试调用，
    不计入密钥耗尽。配置缺失立即失败。密钥全部失败后标记 keys_exhausted。
    """
    if not agent_api_keys:
        return operation(agent_api_key, model, base_url)
    last: ProcessingError | None = None
    invalid_attempts = 0
    for index, key in enumerate(agent_api_keys, start=1):
        attempt = 0
        while True:
            try:
                return operation(key, model, base_url)
            except ProcessingError as exc:
                if "未配置" in str(exc):
                    raise
                last = exc
                if exc.failure == "invalid_output":
                    invalid_attempts += 1
                    if invalid_attempts > retries:
                        raise ProcessingError(
                            "模型未返回有效的推荐结果，未能完成模板推荐", retryable=True) from None
                    time.sleep(interval)
                    continue
                if exc.failure == "auth":
                    break
                if attempt < retries:
                    time.sleep(interval)
                    attempt += 1
                    continue
                break
        if index < len(agent_api_keys):
            LOG.warning("模型密钥连续失败，切换下一个密钥：序号=%s/%s，原因=%s", index, len(agent_api_keys), last)
    raise ProcessingError("全部模型密钥均调用失败", retryable=False, keys_exhausted=True) from None


def _recommend(
    title: str, body: str, templates: list[dict[str, Any]],
    agent_api_key: str, model: str, base_url: str,
    agent_api_keys: tuple[str, ...] | None, retries: int, interval: float,
) -> dict[str, Any]:
    """调用模板推荐。保留给仍直接依赖推荐入口的测试和调用方。"""
    return _call_model(
        lambda key, model_name, url: compliance_recommend.recommend_template(
            title, body, templates, key, model_name, url),
        agent_api_key, model, base_url, agent_api_keys, retries, interval)


def _suggestion(problems: list[dict[str, str]], extra: dict[str, str] | None,
                maintainers: tuple[str, ...]) -> dict[str, Any] | None:
    """合并模板配置问题和维护者建议。没有内容时不入库。"""
    reasons = [f"{item['source']}：{item['message']}" for item in problems]
    suggestions = ["请修复上述仓库模板配置问题。"] if problems else []
    if extra:
        if extra.get("reason"):
            reasons.append(extra["reason"])
        if extra.get("suggestion"):
            suggestions.append(extra["suggestion"])
    if not reasons:
        return None
    return {"reason": "\n".join(reasons), "suggestion": "\n".join(suggestions),
            "mtr": json.dumps(maintainers, ensure_ascii=False)}


def _legacy_result(program: dict[str, Any], templates: list[dict[str, Any]], problems: list[dict[str, str]],
                   maintainers: tuple[str, ...], previously_commented: bool) -> dict[str, Any]:
    """模型不可用时沿用程序结论。没有可用模板时无法核对正文矛盾，标签记为通过。"""
    if program["status"] == "template_error":
        summary = {"status": "template_error", "recommendation_status": None, "label_verdict": "passed"}
        return {"summary": summary, "comment": "", "publish_comment": False,
                "maintainer_suggestion": _suggestion(problems, None, maintainers)}
    if program["status"] == "no_match":
        summary = {"status": "no_match", "recommendation_status": None, "label_verdict": "failed"}
        comment = compliance_comment.format_decision(
            {"comment_kind": "unmatched"}, templates=templates)
        return {"summary": summary, "comment": comment, "publish_comment": True,
                "maintainer_suggestion": _suggestion(problems, None, maintainers)}
    summary = {"status": program["status"], "recommendation_status": None}
    publish_comment = should_publish_comment(summary, previously_commented=previously_commented)
    comment = compliance_comment.format_compliance_check(program, templates=templates) if publish_comment else ""
    return {"summary": summary, "comment": comment, "publish_comment": publish_comment,
            "maintainer_suggestion": _suggestion(problems, None, maintainers)}


def _ask_model(payload: dict[str, Any], agent_api_key: str, model: str, base_url: str,
               agent_api_keys: tuple[str, ...] | None, retries: int, interval: float) -> dict[str, Any]:
    """调用评估。超时、鉴权失败和无效输出沿用密钥轮换与任务重试。"""
    return _call_model(
        lambda key, model_name, url: compliance_assess.consult(payload, key, model_name, url),
        agent_api_key, model, base_url, agent_api_keys, retries, interval)


def analyze_issue(
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
    """执行规范性检查，生成结果摘要与可直接发布的完整评论。

    checks 仅在调度器把默认规范性检查交给本工具时出现。其他类别由 run_checks 分发。
    """
    if not is_default_checks(checks):
        raise ProcessingError("规范性检查不负责其他检查类别", retryable=False)
    templates = []
    template_problems = []
    if not template_files:
        template_problems.append({"source": repository,
                                  "message": "未找到可用的 YAML 或 Markdown Issue 模板，请添加模板或检查组织公共模板配置。"})
    for file in template_files:
        try:
            templates.append(compliance_parse.parse_template(file["content"], file["filename"]))
        except yaml.YAMLError:
            template_problems.append({"source": file["source"],
                                      "message": "YAML 语法无效，请检查缩进、引号、括号及标签。"})
        except ValueError as exc:
            template_problems.append({"source": file["source"], "message": str(exc)})
    configured = compliance_assess.model_configured(
        agent_api_key, model, base_url, agent_api_keys)
    if not templates:
        if not configured:
            return _legacy_result({"status": "template_error"}, [], template_problems, maintainers, previously_commented)
        payload = compliance_assess.review_payload(title, body, [], None)
        raw = _ask_model(payload, agent_api_key, model, base_url, agent_api_keys,
                         llm_key_retries, llm_key_retry_interval)
        reviewed = compliance_assess.decide_review(
            None, [], raw, payload["issue"]["segments"], template_problems)
        if reviewed.get("fallback"):
            raise ProcessingError("模型未返回有效的评估结果，未能完成内容评估。", failure="invalid_output")
        suggestion = _suggestion(template_problems, None, maintainers)
        if not reviewed.get("conflicts"):
            summary = {"status": "template_error", "recommendation_status": None, "label_verdict": "passed",
                       "assessment": {"correctness": "pass", "fallback": None}}
            return {"summary": summary, "comment": "", "publish_comment": False,
                    "maintainer_suggestion": suggestion}
        decision = {"comment_kind": "conflict", "comment_parts": {"conflicts": reviewed["conflicts"]}}
        summary = {"status": "template_error", "recommendation_status": None, "label_verdict": "failed",
                   "assessment": {"correctness": "conflict", "conflicts": reviewed["conflicts"], "fallback": None}}
        return {"summary": summary, "comment": compliance_comment.format_decision(decision),
                "publish_comment": True, "maintainer_suggestion": suggestion}

    program = compliance_check.check_issue(title, body, templates)
    if not configured:
        return _legacy_result(program, templates, template_problems, maintainers, previously_commented)
    payload = compliance_assess.review_payload(title, body, templates, program)
    segments = payload["issue"]["segments"]
    chosen = compliance_assess.current_template(program)
    chosen_template = compliance_assess._template_by_name(templates, chosen) if chosen else None
    prior = prior_snapshot if isinstance(prior_snapshot, dict) else None
    reuse = (prior is not None and chosen_template is not None
             and prior.get("body") == body and prior.get("title") == title
             and prior.get("template") == chosen
             and compliance_assess.sections_unchanged(prior, chosen_template, body))
    if reuse:
        raw = {"template_fit": "correct", "template": chosen, "reason": "必填段落与上次相同",
               "maintainer": None,
               "fields": compliance_assess.fields_from_snapshot(prior, chosen_template, body, segments),
               "conflicts": compliance_assess._remap_conflicts(prior, segments)}
    else:
        raw = _ask_model(payload, agent_api_key, model, base_url, agent_api_keys,
                         llm_key_retries, llm_key_retry_interval)
        stable_template = compliance_assess._template_by_name(templates, raw.get("template") if isinstance(raw, dict) else None)
        raw = compliance_assess.overlay_unchanged_fields(raw, prior, stable_template, body, segments)
    decision = compliance_assess.decide_review(
        program, templates, raw, segments, template_problems)
    if decision.get("fallback"):
        raise ProcessingError("模型未返回有效的评估结果，未能完成内容评估。", failure="invalid_output")
    kind = decision["comment_kind"]
    passed_notice = decision["label_verdict"] == "passed" and kind in {"pass", "none"}
    publish_comment = (passed_notice and previously_commented) or kind in {
        "template", "insufficient", "issues", "conflict"}
    if passed_notice and previously_commented:
        comment = compliance_comment.format_decision({**decision, "comment_kind": "pass"}, templates=templates)
    elif publish_comment:
        comment = compliance_comment.format_decision(decision, templates=templates)
    else:
        comment = ""
    summary = {"status": decision["status"], "recommendation_status": decision["recommendation_status"],
               "label_verdict": decision["label_verdict"], "assessment": decision["assessment"]}
    snapshot_template = compliance_assess._template_by_name(templates, decision["assessment"].get("template"))
    snapshot = compliance_assess.build_snapshot(
        snapshot_template, body, decision["assessment"], payload["issue"]["segments"], title=title)
    if snapshot is not None:
        summary["assessment_snapshot"] = snapshot
    return {"summary": summary, "comment": comment, "publish_comment": publish_comment,
            "maintainer_suggestion": _suggestion(template_problems, decision.get("maintainer"), maintainers)}


class ComplianceTool:
    """规范性检查工具。参数必须为空；具体步骤在同目录的解析、核对、推荐和评论模块中。"""

    kind = COMPLIANCE

    def run(self, context, parameters: dict) -> dict:
        if parameters:
            raise ProcessingError("规范性检查暂不接受参数", retryable=False)
        return analyze_issue(
            context.title, context.body, context.template_files,
            context.agent_api_key, context.model, context.base_url,
            repository=context.repository, number=context.number,
            maintainers=context.maintainers, previously_commented=context.previously_commented,
            agent_api_keys=context.agent_api_keys, llm_key_retries=context.llm_key_retries,
            llm_key_retry_interval=context.llm_key_retry_interval,
            prior_snapshot=context.prior_snapshot,
        )
