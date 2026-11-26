"""模板使用、完整性与正文冲突的模型评估。程序核对模型输出后再决定结论。"""

import json
import re
from typing import Any

from issue_gov.checks.tools.compliance.parse import _fields
from issue_gov.checks.tools.compliance.recommend import invoke_model
from issue_gov.checks.tools.compliance.rules import ASSESS_PROMPT
from issue_gov.checks.tools.compliance.segment import segment_body

_FITS = {"correct", "wrong", "not_applicable", "insufficient_info"}
_FIELD_STATUS = {"answered", "partial", "absent"}
_CONFLICTS = {"version_env", "step_result", "expectation_inverted"}
_LINK = re.compile(r"https?://|@", re.IGNORECASE)


def model_configured(agent_api_key: str, model: str, base_url: str,
                     agent_api_keys: tuple[str, ...] | None = None) -> bool:
    """有密钥、模型名和接口地址时才调用模型。多个密钥时不必再填单个密钥。"""
    return bool((agent_api_key.strip() or agent_api_keys) and model.strip() and base_url.strip())


def consult(payload: dict[str, Any], agent_api_key: str, model: str, base_url: str) -> dict[str, Any]:
    """调用模型评估一次。提示词和轮次与模板推荐分开。"""
    data = json.dumps(payload, ensure_ascii=False)
    return invoke_model(
        ASSESS_PROMPT.read_text(encoding="utf-8"), data, agent_api_key, model, base_url,
        max_turns="1", purpose="内容评估")


def current_template(program: dict[str, Any]) -> str | None:
    """程序已经唯一认定的模板。多个候选或没有匹配时返回 None。"""
    matched = list(program.get("matched_templates") or [])
    if len(matched) == 1:
        return matched[0]
    if program.get("status") == "pass":
        complete = [item["template"] for item in program.get("checks") or []
                    if not item.get("problems") and not item.get("limitation")]
        if len(complete) == 1:
            return complete[0]
    return None


def _check_for(program: dict[str, Any], name: str) -> dict[str, Any]:
    for item in program.get("checks") or []:
        if item.get("template") == name:
            return item
    return {"template": name, "problems": []}


def _template_by_name(templates: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((item for item in templates if item.get("name") == name), None)


def pending_fields(template: dict[str, Any], check: dict[str, Any]) -> list[dict[str, Any]]:
    """程序尚未确认已填写的必填文本字段。下拉框和复选框留给结构检查。"""
    reasons = {item["field"]: item["reason"] for item in check.get("problems") or []}
    pending = []
    for field in _fields(template):
        if field["type"] in {"dropdown", "checkboxes"}:
            continue
        if not field.get("validations", {}).get("required"):
            continue
        label = field["attributes"]["label"].strip()
        if reasons.get(label) in {"missing_section", "empty_section"}:
            pending.append({
                "label": label,
                "description": field["attributes"].get("description") or "",
            })
    return pending


def structure_problems(check: dict[str, Any]) -> list[dict[str, str]]:
    """只保留会单独造成失败的结构问题：非法选项和未勾选的必选项。"""
    return [{"field": item["field"], "message": item["message"]}
            for item in check.get("problems") or []
            if item.get("reason") in {"invalid_option", "unchecked_required"}]


def _plain(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > limit or _LINK.search(text):
        return None
    return text


def _segment_ids(segments: list[dict[str, str]]) -> set[str]:
    return {item["id"] for item in segments}


def _clean_model(raw: dict[str, Any] | None, names: set[str], segments: list[dict[str, str]],
                 *, conflicts_only: bool) -> dict[str, Any] | None:
    """丢掉引用不存在、字段越界或文本不合法的模型结论。整份无法识别时返回 None。"""
    if not isinstance(raw, dict):
        return None
    ids = _segment_ids(segments)
    conflicts = []
    for item in raw.get("conflicts") or []:
        if not isinstance(item, dict):
            continue
        left, right = item.get("left"), item.get("right")
        note = _plain(item.get("note"), 200)
        if item.get("kind") not in _CONFLICTS or left == right or left not in ids or right not in ids or note is None:
            continue
        conflicts.append({"kind": item["kind"], "left": left, "right": right, "note": note})
    if conflicts_only:
        if not isinstance(raw.get("conflicts"), list):
            return None
        return {"conflicts": conflicts}
    fit = raw.get("template_fit")
    if fit not in _FITS:
        return None
    template = raw.get("template")
    if template is not None and template not in names:
        template = None
    reason = _plain(raw.get("reason"), 500)
    maintainer = _plain(raw.get("maintainer"), 500)
    if fit == "not_applicable" and maintainer is None:
        return None
    if reason is None:
        return None
    if fit == "wrong" and template is None:
        return None
    if fit == "correct" and template is None:
        return None
    fields = []
    seen: set[str] = set()
    for item in raw.get("fields") or []:
        if not isinstance(item, dict) or item.get("status") not in _FIELD_STATUS:
            continue
        label = item.get("field")
        if not isinstance(label, str) or not label.strip():
            continue
        label = label.strip()
        if label in seen:
            continue
        evidence = [seg for seg in item.get("evidence") or [] if seg in ids]
        ask = _plain(item.get("ask"), 200)
        status = item["status"]
        if status != "absent" and not evidence:
            # 没有可核对证据的 answered 和 partial 都不能算作已提供
            status = "absent"
        seen.add(label)
        fields.append({"field": label, "status": status, "evidence": evidence, "ask": ask})
    return {"template_fit": fit, "template": template, "reason": reason, "maintainer": maintainer,
            "fields": fields, "conflicts": conflicts}


def _content_gaps(template: dict[str, Any], check: dict[str, Any], fields: list[dict[str, Any]]) -> list[dict[str, str]]:
    """必填文本字段以模型结论为准；模型没有提到的待定字段视为未提供。"""
    by_label = {item["field"]: item for item in fields if isinstance(item.get("field"), str)}
    gaps = []
    for pending in pending_fields(template, check):
        found = by_label.get(pending["label"])
        if found is None or found["status"] != "answered":
            ask = (found or {}).get("ask") or "请补充具体内容"
            gaps.append({"field": pending["label"], "message": ask})
    return gaps


def _result(*, status: str, recommendation_status: Any, label_verdict: str, comment_kind: str,
            comment_parts: dict[str, Any], maintainer: dict[str, Any] | None,
            assessment: dict[str, Any], recommendation: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "recommendation_status": recommendation_status,
        "label_verdict": label_verdict,
        "comment_kind": comment_kind,
        "comment_parts": comment_parts,
        "maintainer": maintainer,
        "assessment": assessment,
        "recommendation": recommendation,
    }


def decide_review(
    program: dict[str, Any] | None, templates: list[dict[str, Any]], model: dict[str, Any] | None,
    segments: list[dict[str, str]], template_problems: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """把程序结果和模型输出收成评论、标签和 status 都能用的一份决定。

    输出无法识别时返回 fallback，由调用方按可重试错误交回任务队列。
    模板用错、没有适用模板或信息不足时，不再采用完整性和冲突结论。
    """
    problems = template_problems or []
    names = {item["name"] for item in templates}
    if program is None:
        cleaned = _clean_model(model, names, segments, conflicts_only=True)
        if cleaned is None:
            return {"fallback": "correctness", "conflicts": []}
        return {"fallback": None, "conflicts": cleaned["conflicts"], "template_problems": problems}
    cleaned = _clean_model(model, names, segments, conflicts_only=False)
    if cleaned is None:
        return {"fallback": "program"}
    current = current_template(program)
    fit = cleaned["template_fit"]
    chosen = cleaned["template"]
    if fit == "correct" and current is not None:
        chosen = current
    assessment = {"template_fit": fit, "template": chosen, "fallback": None}
    if fit == "wrong" and current is not None and chosen == current:
        return {"fallback": "program"}
    if fit == "wrong":
        return _result(
            status="no_match", recommendation_status="recommended", label_verdict="failed",
            comment_kind="template", comment_parts={"reason": cleaned["reason"], "template": chosen},
            maintainer=None, assessment=assessment,
            recommendation={"status": "recommended", "template": chosen, "reason": cleaned["reason"],
                            "maintainer": None})
    if fit == "not_applicable":
        return _result(
            status="no_match", recommendation_status="not_applicable", label_verdict="passed",
            comment_kind="none", comment_parts={},
            maintainer={"reason": cleaned["reason"] or "", "suggestion": cleaned["maintainer"]},
            assessment=assessment,
            recommendation={"status": "not_applicable", "template": None, "reason": cleaned["reason"],
                            "maintainer": cleaned["maintainer"]})
    if fit == "insufficient_info":
        return _result(
            status="no_match", recommendation_status="insufficient_info", label_verdict="failed",
            comment_kind="insufficient", comment_parts={"reason": cleaned["reason"]},
            maintainer=None, assessment=assessment,
            recommendation={"status": "insufficient_info", "template": None, "reason": cleaned["reason"],
                            "maintainer": None})
    template = _template_by_name(templates, chosen)
    check = _check_for(program, chosen)
    if template is None:
        return {"fallback": "program"}
    gaps = _content_gaps(template, check, cleaned["fields"])
    structural = structure_problems(check)
    conflicts = cleaned["conflicts"]
    assessment.update({"fields": cleaned["fields"], "conflicts": conflicts, "content_gaps": gaps,
                       "structure": structural})
    if not gaps and not structural and not conflicts:
        return _result(
            status="pass", recommendation_status=None, label_verdict="passed",
            comment_kind="pass", comment_parts={}, maintainer=None, assessment=assessment)
    return _result(
        status="partial_match", recommendation_status=None, label_verdict="failed",
        comment_kind="issues",
        comment_parts={"template": chosen, "structure": structural, "content": gaps, "conflicts": conflicts},
        maintainer=None, assessment=assessment)


def review_payload(title: str, body: str, templates: list[dict[str, Any]], program: dict[str, Any] | None) -> dict[str, Any]:
    """整理给模型的输入。只包含字段说明和正文片段，不包含整份模板原文。"""
    segments = segment_body(body)
    if program is None:
        return {"mode": "conflicts", "issue": {"title": title, "segments": segments}}
    current = current_template(program)
    selected = _template_by_name(templates, current) if current else None
    pending = pending_fields(selected, _check_for(program, current)) if selected is not None and current else []
    described = []
    for template in templates:
        described.append({
            "name": template["name"],
            "description": template.get("description") or "",
            "title": template.get("title") or "",
            "fields": [{"label": field["attributes"]["label"].strip(),
                        "description": field["attributes"].get("description") or "",
                        "required": bool(field.get("validations", {}).get("required")),
                        "type": field["type"]}
                       for field in _fields(template)],
        })
    return {
        "mode": "review",
        "issue": {"title": title, "segments": segments},
        "current_template": current,
        "program_status": program.get("status"),
        "pending_fields": pending,
        "templates": described,
    }

