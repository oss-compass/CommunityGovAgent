"""模板使用、完整性与正文冲突的模型评估。模型复核程序的文本结论，程序保留封闭规则。"""

import json
import re
from typing import Any

from markdown_it import MarkdownIt

from issue_gov.checks.tools.compliance.parse import (
    _fields, _resolve_section_name, _section_forms, _section_key, _sections)
from issue_gov.checks.tools.compliance.recommend import invoke_model
from issue_gov.checks.tools.compliance.rules import ASSESS_PROMPT
from issue_gov.checks.tools.compliance.segment import segment_body

_FITS = {"correct", "wrong", "not_applicable", "insufficient_info"}
_FIELD_STATUS = {"answered", "partial", "absent"}
_CONFLICTS = {"version_env", "step_result", "expectation_inverted"}
# 只去掉网址本身。中文说明常常紧挨在链接后面，不能把后面的字一起删掉。
_LINK = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+|@[A-Za-z0-9][A-Za-z0-9_-]*",
    re.IGNORECASE,
)


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
    """去掉链接和 @ 提及。其余文字仍可用，避免整份评估因为一句带链接的说明被丢掉。"""
    if not isinstance(value, str):
        return None
    text = " ".join(_LINK.sub(" ", value).split())
    if not text or len(text) > limit:
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


def _required_index(template: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    """必填文本字段的匹配键，以及标题写法到字段键的映射。"""
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
    return field_keys, names


def required_sections(template: dict[str, Any], body: str) -> dict[str, str]:
    """必填文本字段对应的章节正文。没有该章节时为空字符串。"""
    field_keys, names = _required_index(template)
    found = _sections(body, normalize_hints=True, field_keys=field_keys, field_names=names)
    return {key: found.get(key, "") for key in field_keys}


def _preamble(template: dict[str, Any], body: str) -> str:
    """第一个必填标题之前的正文。证据写在前言里时靠它判断这段有没有改。"""
    tokens = MarkdownIt("commonmark").parse(body or "")
    field_keys, names = _required_index(template)
    first = None
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.level != 0:
            continue
        resolved = _resolve_section_name(_section_key(tokens[index + 1].content), names)
        if resolved in field_keys:
            first = index
            break
    if first is None:
        return body or ""
    parts = []
    for index in range(first):
        token = tokens[index]
        if token.type in {"fence", "code_block"}:
            parts.append(token.content)
        elif token.type == "inline" and (index == 0 or tokens[index - 1].type != "heading_open"):
            parts.append(re.sub(r"<!--[\s\S]*?-->", "", token.content))
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _ids_in_section(segments: list[dict[str, str]], section_text: str) -> list[str]:
    wanted = _norm_text(section_text)
    if not wanted:
        return []
    return [item["id"] for item in segments
            if _norm_text(item.get("text") or "") and _norm_text(item["text"]) in wanted]


def _ids_matching(segments: list[dict[str, str]], texts: list[str]) -> list[str]:
    """按规范化后的原文找回片段编号。每段证据都要能对上。"""
    wanted = {_norm_text(text) for text in texts if _norm_text(text)}
    if not wanted:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for item in segments:
        text = _norm_text(item.get("text") or "")
        if text in wanted:
            found.append(item["id"])
            seen.add(text)
    return found if wanted <= seen else []


def _evidence_in_section(text: str, section_text: str) -> bool:
    """证据原文就是这项标题下的整段、其中一行，或多行块被完整包含。"""
    wanted = _norm_text(text)
    if not wanted:
        return False
    if wanted == _norm_text(section_text):
        return True
    lines = {_norm_text(line) for line in section_text.splitlines() if line.strip()}
    if wanted in lines:
        return True
    return "\n" in text and wanted in _norm_text(section_text)


def _evidence_is_stable(texts: list[str], snapshot: dict[str, Any], template: dict[str, Any],
                       body: str, segments: list[dict[str, str]]) -> bool:
    """上次的证据原文都还在，且所在的必填项或前言没有改动。"""
    if not texts or not _ids_matching(segments, texts):
        return False
    current = required_sections(template, body)
    previous = snapshot.get("sections") or {}
    changed = {key for key, text in current.items() if text != previous.get(key, "")}
    preamble_same = _preamble(template, body) == _preamble(template, snapshot.get("body") or "")
    current_preamble = _preamble(template, body)
    for text in texts:
        owners = [key for key, section in current.items() if section and _evidence_in_section(text, section)]
        if owners:
            if any(key in changed for key in owners):
                return False
            continue
        if not preamble_same or not _evidence_in_section(text, current_preamble):
            return False
    return True


def _has_new_evidence(field: dict[str, Any], segments: list[dict[str, str]], previous_body: str) -> bool:
    """模型用上次正文里没有的片段说明这项已经提供。"""
    if field.get("status") not in {"answered", "partial"}:
        return False
    previous = _norm_text(previous_body)
    by_id = {item["id"]: item.get("text") or "" for item in segments}
    return any(_norm_text(by_id[item]) not in previous
               for item in field.get("evidence") or [] if item in by_id and _norm_text(by_id[item]))


def build_snapshot(template: dict[str, Any] | None, body: str, assessment: dict[str, Any] | None,
                   segments: list[dict[str, str]], *, title: str = "") -> dict[str, Any] | None:
    """记下本次标题、模板、必填段落正文、证据原文和字段结论。"""
    if not template or not assessment or not assessment.get("template"):
        return None
    sections = required_sections(template, body)
    by_id = {item["id"]: item.get("text") or "" for item in segments}
    fields = {}
    for item in assessment.get("fields") or []:
        key = _section_key(item.get("field") or "")
        if key not in sections or item.get("status") not in {"answered", "absent"}:
            continue
        evidence_texts = [by_id[evidence] for evidence in item.get("evidence") or []
                          if evidence in by_id and by_id[evidence].strip()]
        fields[key] = {"field": item["field"], "status": item["status"], "ask": item.get("ask"),
                       "evidence_texts": evidence_texts}
    conflicts = []
    for item in assessment.get("conflicts") or []:
        left, right = by_id.get(item.get("left")), by_id.get(item.get("right"))
        if left and right and item.get("kind") in _CONFLICTS and item.get("note"):
            conflicts.append({"kind": item["kind"], "left_text": left, "right_text": right, "note": item["note"]})
    return {"template": assessment["template"], "title": title, "body": body,
            "sections": sections, "fields": fields, "conflicts": conflicts}


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


def _carried_field(item: dict[str, Any], evidence: list[str]) -> dict[str, Any]:
    status = item.get("status")
    return {"field": item["field"], "status": status,
            "evidence": evidence if status == "answered" else [], "ask": item.get("ask")}


def fields_from_snapshot(snapshot: dict[str, Any], template: dict[str, Any], body: str,
                         segments: list[dict[str, str]]) -> list[dict[str, Any]]:
    """整篇未改时，把上次字段结论改写成当前片段编号。证据可以在其他项里。"""
    current = required_sections(template, body)
    previous = snapshot.get("sections") or {}
    fields = []
    for key, item in (snapshot.get("fields") or {}).items():
        if current.get(key, "") != previous.get(key, ""):
            continue
        status = item.get("status")
        if status not in {"answered", "absent"}:
            continue
        evidence: list[str] = []
        if status == "answered":
            if "evidence_texts" in item:
                evidence = _ids_matching(segments, item.get("evidence_texts") or [])
            else:
                evidence = _ids_in_section(segments, current.get(key, ""))
            if not evidence:
                continue
        fields.append(_carried_field(item, evidence))
    return fields


def _fields_to_carry(snapshot: dict[str, Any], template: dict[str, Any], body: str,
                     segments: list[dict[str, str]], raw_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """正文有变化时，证据所在位置没改则沿用已提供。新证据留给模型。"""
    current = required_sections(template, body)
    previous = snapshot.get("sections") or {}
    model_fields: dict[str, dict[str, Any]] = {}
    for item in raw_fields:
        if isinstance(item, dict) and isinstance(item.get("field"), str):
            model_fields[_section_key(item["field"])] = item
    carried = []
    for key, item in (snapshot.get("fields") or {}).items():
        if current.get(key, "") != previous.get(key, ""):
            continue
        status = item.get("status")
        if status == "answered":
            texts = item.get("evidence_texts") if "evidence_texts" in item else None
            if not texts or not _evidence_is_stable(texts, snapshot, template, body, segments):
                continue
            evidence = _ids_matching(segments, texts)
            if not evidence:
                continue
            carried.append(_carried_field(item, evidence))
        elif status == "absent":
            model_item = model_fields.get(key)
            if model_item is not None and _has_new_evidence(model_item, segments, snapshot.get("body") or ""):
                continue
            carried.append(_carried_field(item, []))
    return carried


def _stabilize_conflicts(conflicts: list[Any], snapshot: dict[str, Any],
                         segments: list[dict[str, str]]) -> list[Any]:
    """两边都是上次已有原文、且上次没有这组原文时，丢掉新报的冲突。"""
    previous_body = _norm_text(snapshot.get("body") or "")
    if not previous_body:
        return conflicts
    known = set()
    for item in snapshot.get("conflicts") or []:
        pair = tuple(sorted((_norm_text(item.get("left_text") or ""), _norm_text(item.get("right_text") or ""))))
        if all(pair):
            known.add(pair)
    by_id = {item["id"]: item.get("text") or "" for item in segments}
    kept = []
    for item in conflicts:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        left, right = by_id.get(item.get("left"), ""), by_id.get(item.get("right"), "")
        left_text, right_text = _norm_text(left), _norm_text(right)
        if not left_text or not right_text or left_text not in previous_body or right_text not in previous_body:
            kept.append(item)
            continue
        if tuple(sorted((left_text, right_text))) in known:
            kept.append(item)
    return kept


def overlay_unchanged_fields(raw: dict[str, Any], snapshot: dict[str, Any] | None,
                             template: dict[str, Any] | None, body: str,
                             segments: list[dict[str, str]]) -> dict[str, Any]:
    """模板未变时盖住证据仍在原处的必填项，并丢掉针对未改原文新报的冲突。"""
    if not snapshot or not template or not isinstance(raw, dict):
        return raw
    if raw.get("template_fit") != "correct" or raw.get("template") != snapshot.get("template"):
        return raw
    if template.get("name") != snapshot.get("template"):
        return raw
    raw_fields = [item for item in (raw.get("fields") or []) if isinstance(item, dict)]
    carried = _fields_to_carry(snapshot, template, body, segments, raw_fields)
    fields = list(raw_fields)
    index = {_section_key(item.get("field") or ""): position for position, item in enumerate(fields)
             if isinstance(item.get("field"), str)}
    for item in carried:
        key = _section_key(item["field"])
        if key in index:
            fields[index[key]] = item
        else:
            fields.append(item)
    updated = {**raw, "fields": fields}
    if isinstance(raw.get("conflicts"), list):
        updated["conflicts"] = _stabilize_conflicts(raw["conflicts"], snapshot, segments)
    return updated


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

