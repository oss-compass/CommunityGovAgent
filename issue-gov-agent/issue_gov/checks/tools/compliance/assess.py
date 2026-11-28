"""模板使用、完整性与正文冲突的模型评估。模型复核程序的文本结论，程序保留封闭规则。"""

import json
import re
from typing import Any

from issue_gov.checks.tools.compliance.parse import _fields, _section_forms, _section_key, _sections
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


def _filled_templates(program: dict[str, Any]) -> list[str]:
    """程序认为已经按模板写全的名称。没有问题、也没有无法检查的限制。"""
    return [item["template"] for item in program.get("checks") or []
            if item.get("template") and not item.get("problems") and not item.get("limitation")]


def current_template(program: dict[str, Any]) -> str | None:
    """程序已经唯一认定的模板。多个候选或没有匹配时返回 None。"""
    matched = list(program.get("matched_templates") or [])
    if len(matched) == 1:
        return matched[0]
    if program.get("status") == "pass":
        complete = _filled_templates(program)
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


def text_fields(template: dict[str, Any], check: dict[str, Any]) -> list[dict[str, Any]]:
    """必填文本字段及程序结论。下拉框和复选框留给结构检查，不交给模型改判。"""
    reasons = {item["field"]: item["reason"] for item in check.get("problems") or []}
    items = []
    for field in _fields(template):
        if field["type"] in {"dropdown", "checkboxes"}:
            continue
        if not field.get("validations", {}).get("required"):
            continue
        label = field["attributes"]["label"].strip()
        reason = reasons.get(label)
        if reason == "missing_section":
            program = "missing"
        elif reason == "empty_section":
            program = "empty"
        else:
            program = "filled"
        items.append({
            "label": label,
            "description": field["attributes"].get("description") or "",
            "program": program,
        })
    return items


def pending_fields(template: dict[str, Any], check: dict[str, Any]) -> list[dict[str, Any]]:
    """程序尚未确认已填写的必填文本字段。"""
    return [item for item in text_fields(template, check) if item["program"] != "filled"]


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
    supplied_conflicts = raw.get("conflicts")
    if not isinstance(supplied_conflicts, list):
        return None
    conflicts = []
    for item in supplied_conflicts:
        if not isinstance(item, dict):
            continue
        left, right = item.get("left"), item.get("right")
        note = _plain(item.get("note"), 200)
        if item.get("kind") not in _CONFLICTS or left == right or left not in ids or right not in ids or note is None:
            continue
        conflicts.append({"kind": item["kind"], "left": left, "right": right, "note": note})
    if conflicts_only:
        return {"conflicts": conflicts}
    fit = raw.get("template_fit")
    if fit not in _FITS:
        return None
    supplied_template = raw.get("template")
    template = supplied_template if supplied_template in names else None
    reason = _plain(raw.get("reason"), 500)
    maintainer = _plain(raw.get("maintainer"), 500)
    if fit == "not_applicable" and maintainer is None:
        return None
    if reason is None:
        return None
    if fit == "correct" and template is None:
        return None
    if fit == "wrong" and supplied_template is not None and template is None:
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
        # 旧输出里的 partial 表示“提到了但觉得不够”。有证据就视为已经提供，避免同一段文字在通过和不通过之间摆动。
        if status == "partial":
            status = "answered"
        if status != "absent" and not evidence:
            # 没有可核对证据的已回答不能算作已提供
            status = "absent"
        seen.add(label)
        fields.append({"field": label, "status": status, "evidence": evidence, "ask": ask})
    return {"template_fit": fit, "template": template, "reason": reason, "maintainer": maintainer,
            "fields": fields, "conflicts": conflicts}


def _review_of(pending_label: str, fields: list[dict[str, Any]]) -> dict[str, Any] | None:
    """把模型对程序待定字段的复核挂回去。全称优先，并忽略末尾的必填、选填提示。"""
    exact = [item for item in fields if item.get("field") == pending_label]
    if exact:
        return exact[0]
    key = _section_key(pending_label)
    for item in fields:
        field = item.get("field")
        if isinstance(field, str) and _section_key(field) == key:
            return item
    return None


def _norm_text(value: str) -> str:
    return " ".join(value.split())


def required_sections(template: dict[str, Any], body: str) -> dict[str, str]:
    """必填文本字段对应的章节正文。没有该章节时为空字符串。"""
    labels = [field["attributes"]["label"] for field in _fields(template)
              if field["type"] not in {"dropdown", "checkboxes"}
              and field.get("validations", {}).get("required")]
    field_keys = {_section_key(label) for label in labels}
    candidates: dict[str, set[str]] = {}
    for key in field_keys:
        for form in _section_forms(key):
            candidates.setdefault(form, set()).add(key)
    names = {form: next(iter(targets)) for form, targets in candidates.items() if len(targets) == 1}
    names.update({key: key for key in field_keys})
    found = _sections(body, normalize_hints=True, field_keys=field_keys, field_names=names)
    return {key: found.get(key, "") for key in field_keys}


def _ids_in_section(segments: list[dict[str, str]], section_text: str) -> list[str]:
    wanted = _norm_text(section_text)
    if not wanted:
        return []
    return [item["id"] for item in segments
            if _norm_text(item.get("text") or "") and _norm_text(item["text"]) in wanted]


def build_snapshot(template: dict[str, Any] | None, body: str, assessment: dict[str, Any] | None,
                   segments: list[dict[str, str]]) -> dict[str, Any] | None:
    """记下本次模板、必填段落正文和字段结论，供下次沿用未改动的项。"""
    if not template or not assessment or not assessment.get("template"):
        return None
    sections = required_sections(template, body)
    by_id = {item["id"]: item.get("text") or "" for item in segments}
    fields = {}
    for item in assessment.get("fields") or []:
        key = _section_key(item.get("field") or "")
        if key not in sections or item.get("status") not in {"answered", "absent"}:
            continue
        fields[key] = {"field": item["field"], "status": item["status"], "ask": item.get("ask")}
    conflicts = []
    for item in assessment.get("conflicts") or []:
        left, right = by_id.get(item.get("left")), by_id.get(item.get("right"))
        if left and right and item.get("kind") in _CONFLICTS and item.get("note"):
            conflicts.append({"kind": item["kind"], "left_text": left, "right_text": right, "note": item["note"]})
    return {"template": assessment["template"], "body": body, "sections": sections, "fields": fields, "conflicts": conflicts}


def sections_unchanged(snapshot: dict[str, Any] | None, template: dict[str, Any], body: str) -> bool:
    """上次认定的模板下，每个必填段落正文都与这次相同。"""
    if not snapshot or snapshot.get("template") != template.get("name"):
        return False
    current = required_sections(template, body)
    previous = snapshot.get("sections") or {}
    return all(current.get(key, "") == previous.get(key, "") for key in current)


def _remap_conflicts(snapshot: dict[str, Any], segments: list[dict[str, str]]) -> list[dict[str, str]]:
    def locate(text: str) -> str | None:
        wanted = _norm_text(text)
        hits = [item["id"] for item in segments if wanted and _norm_text(item.get("text") or "") == wanted]
        return hits[0] if len(hits) == 1 else None

    conflicts = []
    for item in snapshot.get("conflicts") or []:
        left, right = locate(item.get("left_text") or ""), locate(item.get("right_text") or "")
        note = item.get("note")
        if item.get("kind") in _CONFLICTS and left and right and left != right and isinstance(note, str) and note.strip():
            conflicts.append({"kind": item["kind"], "left": left, "right": right, "note": note.strip()})
    return conflicts


def fields_from_snapshot(snapshot: dict[str, Any], template: dict[str, Any], body: str,
                         segments: list[dict[str, str]]) -> list[dict[str, Any]]:
    """把上次字段结论改写成当前正文片段编号。段落有改动的项不在这里。"""
    current = required_sections(template, body)
    previous = snapshot.get("sections") or {}
    fields = []
    for key, item in (snapshot.get("fields") or {}).items():
        if current.get(key, "") != previous.get(key, ""):
            continue
        evidence = _ids_in_section(segments, current.get(key, ""))
        status = item.get("status")
        if status == "answered" and not evidence:
            continue
        if status not in {"answered", "absent"}:
            continue
        fields.append({"field": item["field"], "status": status,
                       "evidence": evidence if status == "answered" else [], "ask": item.get("ask")})
    return fields


def overlay_unchanged_fields(raw: dict[str, Any], snapshot: dict[str, Any] | None,
                             template: dict[str, Any] | None, body: str,
                             segments: list[dict[str, str]]) -> dict[str, Any]:
    """模板未变时，用上次结论盖住没改过的必填项。改过的项保留本次模型结论。"""
    if not snapshot or not template or not isinstance(raw, dict):
        return raw
    if raw.get("template_fit") != "correct" or raw.get("template") != snapshot.get("template"):
        return raw
    if template.get("name") != snapshot.get("template"):
        return raw
    carried = fields_from_snapshot(snapshot, template, body, segments)
    if not carried:
        return raw
    fields = [item for item in (raw.get("fields") or []) if isinstance(item, dict)]
    index = {_section_key(item.get("field") or ""): position for position, item in enumerate(fields)
             if isinstance(item.get("field"), str)}
    for item in carried:
        key = _section_key(item["field"])
        if key in index:
            fields[index[key]] = item
        else:
            fields.append(item)
    return {**raw, "fields": fields}


def _content_gaps(template: dict[str, Any], check: dict[str, Any], fields: list[dict[str, Any]]) -> list[dict[str, str]]:
    """模型复核每项必填文本。程序已填写且模型未改判的保留；模型确认已提供且证据有效的去掉。"""
    gaps = []
    for item in text_fields(template, check):
        found = _review_of(item["label"], fields)
        if item["program"] == "filled" and found is None:
            continue
        if found is not None and found["status"] == "answered":
            continue
        ask = (found or {}).get("ask") or item["description"] or "请补充具体内容"
        gaps.append({"field": item["label"], "message": ask})
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
    有建议模板时，同时保留该模板的完整性结论和正文冲突。没有建议模板时，只保留冲突。
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
    # 模型确认用对时以模型指向的模板为准，程序没识别到或识别错了都由模型校准。
    if fit == "correct" and chosen:
        current = chosen
    assessment = {"template_fit": fit, "template": chosen, "fallback": None}
    if fit == "wrong" and current is not None and chosen == current:
        return {"fallback": "program"}
    if fit == "insufficient_info":
        return _result(
            status="no_match", recommendation_status="insufficient_info", label_verdict="failed",
            comment_kind="insufficient", comment_parts={"reason": cleaned["reason"]},
            maintainer=None, assessment=assessment,
            recommendation={"status": "insufficient_info", "template": None, "reason": cleaned["reason"],
                            "maintainer": None})
    if fit == "not_applicable" or (fit == "wrong" and not chosen):
        conflicts = cleaned["conflicts"]
        assessment["conflicts"] = conflicts
        suggestion = cleaned["maintainer"] or "请增加能覆盖这类诉求的模板。"
        maintainer = {"reason": cleaned["reason"] or "", "suggestion": suggestion}
        recommendation = {"status": "not_applicable", "template": None, "reason": cleaned["reason"],
                          "maintainer": suggestion}
        if conflicts:
            return _result(
                status="no_match", recommendation_status="not_applicable", label_verdict="failed",
                comment_kind="conflict", comment_parts={"conflicts": conflicts},
                maintainer=maintainer, assessment=assessment, recommendation=recommendation)
        return _result(
            status="no_match", recommendation_status="not_applicable", label_verdict="passed",
            comment_kind="none", comment_parts={},
            maintainer=maintainer, assessment=assessment, recommendation=recommendation)
    suggest = fit == "wrong" and bool(chosen)
    template = _template_by_name(templates, chosen)
    check = _check_for(program, chosen)
    if template is None:
        return {"fallback": "program"}
    gaps = _content_gaps(template, check, cleaned["fields"])
    structural = [] if suggest else structure_problems(check)
    conflicts = cleaned["conflicts"]
    assessment.update({"fields": cleaned["fields"], "conflicts": conflicts, "content_gaps": gaps,
                       "structure": structural})
    if suggest:
        return _result(
            status="no_match", recommendation_status="recommended", label_verdict="failed",
            comment_kind="template",
            comment_parts={"reason": cleaned["reason"], "template": chosen, "content": gaps,
                           "conflicts": conflicts},
            maintainer=None, assessment=assessment,
            recommendation={"status": "recommended", "template": chosen, "reason": cleaned["reason"],
                            "maintainer": None})
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
    reviewed = text_fields(selected, _check_for(program, current)) if selected is not None and current else []
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
            "text_fields": text_fields(template, _check_for(program, template["name"])),
        })
    return {
        "mode": "review",
        "issue": {"title": title, "segments": segments},
        "current_template": current,
        "program_status": program.get("status"),
        "text_fields": reviewed,
        "pending_fields": [item for item in reviewed if item["program"] != "filled"],
        "templates": described,
    }

