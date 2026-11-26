"""规范性检查：对照模板核对标题和必填章节。不访问网络。"""

import re
from typing import Any

from markdown_it import MarkdownIt

from issue_gov.checks.tools.compliance.parse import (
    _fields, _label, _resolve_section_name, _section_forms, _section_key, _sections,
)
from issue_gov.checks.tools.compliance.rules import load_rules

def _checkbox_sections(
    body: str, *, field_keys: set[str] | None = None,
    field_names: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    """提取各章节中已勾选的列表选项。

    Args:
        body: Issue 的原始 Markdown 正文。
        field_keys: 检查 Issue 时的模板字段键；提供时勾选仅归入最近出现的模板字段。
        field_names: 与文本字段共用的章节书写形式映射。
    Returns:
        章节标题到已勾选选项集合的映射；子节勾选计入父节，
        忽略代码块、引用及普通文字中的勾选标记。
    """
    tokens = MarkdownIt("commonmark").parse(body)
    sections: dict[str, set[str]] = {}
    headings: list[tuple[int, str]] = []
    list_depth = quote_depth = 0
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif token.type == "list_item_open":
            list_depth += 1
        elif token.type == "list_item_close":
            list_depth -= 1
        elif token.type == "heading_open" and token.level == 0:
            depth = int(token.tag[1:])
            key = _section_key(tokens[index + 1].content)
            if field_names is not None:
                key = _resolve_section_name(key, field_names)
            if field_keys is not None:
                if key in field_keys:
                    headings = [(depth, key)]
                elif headings and depth <= headings[-1][0]:
                    # 同级或更高的非模板标题开始新内容，避免把其他章节的勾选项归入当前字段。
                    headings = []
            else:
                headings = [(d, label) for d, label in headings if d < depth]
                headings.append((depth, key))
        elif token.type == "inline" and list_depth and not quote_depth:
            match = re.match(r"^\[[xX]\]\s+(.+)$", token.content, re.DOTALL)
            if match:
                for _, label in headings:
                    sections.setdefault(label, set()).add("".join(_label(match.group(1)).split()))
    return sections


def _matches_dropdown_choice(value: str, options: list[str]) -> bool:
    """匹配下拉选项，允许唯一类别的末尾括号说明被补充、细化或省略。

    Args:
        value: Issue 中填写的单个选项。
        options: 模板定义的候选选项列表。
    Returns:
        忽略空白后完全匹配，或去除末尾括号说明后唯一对应某个类别时为 True。
        非完全匹配时，若多个候选仅靠括号区分、类别为空或不在候选中，则为 False。
    """
    value = "".join(value.split())
    choices = {"".join(option.split()) for option in options}
    if value in choices:
        return True
    # 仅处理末尾成对的括号说明；类别名称本身必须一致，不做子串或模糊匹配。
    annotation = r"(?:\([^()（）]*\)|（[^()（）]*）)$"
    category = re.sub(annotation, "", value)
    return bool(category) and sum(re.sub(annotation, "", option) == category for option in choices) == 1


def check_issue(
    title: str, body: str, templates: list[dict[str, Any]],
) -> dict[str, Any]:
    """检查标题前缀和必填章节，不调用网络或判断内容语义。

    Args:
        title: Issue 标题。
        body: Issue 的原始 Markdown 正文。
        templates: 待匹配的 YAML/Markdown 模板字典列表，不能为空。
    Returns:
        一个字典，包含三个字段：
            status 为 pass/partial_match/no_match；
            matched_templates 为完全/部分匹配模板名称列表；
            checks 为各模板的匹配情况，包含 template 名称及 problems 列表，每个问题包含 field、reason 和 message；如果模板缺少可检查的规则还会附加 limitation。
    Raises:
        ValueError: 模板列表为空或模板字段格式无效。
    """
    if not templates:
        raise ValueError("至少需要一个模板")
    spec = load_rules()
    text = spec.messages

    # 本地检查：逐一对照模板的标题前缀、必填章节及占位内容
    title_key = "".join(title.split())
    checks = [] # 记录各模板的检查结果
    matched = [] # 记录候选模板
    has_title_match = False
    for template in templates:
        fields = _fields(template)
        field_keys = {_section_key(field["attributes"]["label"]) for field in fields}
        # 只保留无歧义的章节写法，完整字段名优先。
        candidates: dict[str, set[str]] = {}
        for key in field_keys:
            for form in _section_forms(key):
                candidates.setdefault(form, set()).add(key)
        field_names = {form: next(iter(targets)) for form, targets in candidates.items() if len(targets) == 1}
        field_names.update({key: key for key in field_keys})
        sections = _sections(body, normalize_hints=True, field_keys=field_keys, field_names=field_names)
        selected_options = _checkbox_sections(body, field_keys=field_keys, field_names=field_names)
        markdown = template.get("format") == "markdown"
        problems = []
        prefix = template.get("title", "").strip()
        prefix_key = "".join(prefix.split())
        title_matches = title_key.startswith(prefix_key)
        if not title_matches and prefix_key.endswith(tuple(spec.title_colon)) and len(prefix_key) > 1:
            stem = prefix_key[:-1]
            # 闭合括号已构成边界，允许 [Bug]启动失败；普通文字仍需分隔，避免匹配 Buggy。
            if stem.endswith(spec.title_closed_boundary):
                title_matches = title_key.startswith(stem)
            else:
                pattern = r"\s*".join(re.escape(char) for char in stem)
                boundary = "[" + spec.title_colon + "]"
                title_matches = re.match(r"\s*" + pattern + r"(?=\s|" + boundary + r"|$)", title) is not None
        # 检查标题前缀
        if not title_matches:
            problems.append({"field": "title", "reason": "title_prefix",
                             "message": text["title_prefix"].format(prefix=prefix)})
        required_sections = set()
        # 必填规则已统一为配置与文字指示的并集；已填写的选填下拉框也校验候选值。
        for field in fields:
            attributes = field["attributes"]
            label = attributes["label"].strip()
            section_key = _section_key(label)
            required = field.get("validations", {}).get("required", False)
            required_options = ([option for option in attributes["options"] if option.get("required", False)]
                                if field["type"] == "checkboxes" else [])
            if required or required_options:
                required_sections.add(section_key)
            elif field["type"] != "dropdown" or section_key not in sections:
                continue
            if section_key not in sections:
                problems.append({"field": label, "reason": "missing_section",
                                 "message": text["missing_section"]})
                continue
            if field["type"] == "checkboxes":
                selected = selected_options.get(section_key, set())
                valid = {"".join(_label(option["label"]).split()) for option in attributes["options"]}
                if required and not selected.intersection(valid):
                    problems.append({"field": label, "reason": "unchecked_required",
                                     "message": text["unchecked_any"]})
                for option in required_options:
                    if "".join(_label(option["label"]).split()) not in selected:
                        problems.append({"field": label, "reason": "unchecked_required",
                                         "message": text["unchecked_option"] + option["label"]})
                continue
            content = sections[section_key]
            placeholders = set(spec.placeholders)
            placeholders.add((attributes.get("placeholder") or "").strip())
            # 下拉选项或预填值本身恰为占位词时，仍按模板允许的真实值处理。
            allowed = attributes.get("options", []) if field["type"] == "dropdown" else []
            # YAML 和 Markdown 使用同一规则；仅改变占位提示的空白仍算未填写。
            content_key = "".join(content.split())
            is_placeholder = content_key in {"".join(item.split()) for item in placeholders}
            is_allowed = content_key in {"".join(item.split()) for item in allowed}
            is_prefilled = bool(content_key) and content_key == "".join((attributes.get("value") or "").split())
            if is_placeholder and not is_allowed and not is_prefilled:
                if required:
                    problems.append({"field": label, "reason": "empty_section",
                                     "message": text["empty_section"]})
                continue
            if field["type"] == "dropdown" and not is_allowed:
                valid = _matches_dropdown_choice(content_key, allowed) or (
                    attributes.get("multiple", False) and all(
                        _matches_dropdown_choice(option, allowed) for option in content_key.split(",")))
                if not valid:
                    choice = text["invalid_option_many"] if attributes.get("multiple", False) else text["invalid_option_one"]
                    options = text["option_joiner"].join(allowed)
                    problems.append({"field": label, "reason": "invalid_option",
                                     "message": choice.replace("{options}", options)})
        check = {"template": template["name"], "problems": problems}
        if markdown and not any(field.get("validations", {}).get("required", False) for field in fields):
            check["limitation"] = text["markdown_no_required"]
        elif not prefix_key and not any(
            field.get("validations", {}).get("required", False)
            or (field["type"] == "checkboxes" and any(
                option.get("required", False) for option in field["attributes"]["options"]))
            for field in fields
        ):
            check["limitation"] = text["no_checkable_rule"]
        checks.append(check)
        if check.get("limitation"):
            continue
        # 记录候选模板
        if prefix_key and title_matches:
            if not has_title_match:
                matched.clear() # 优先保留本次标题匹配的模板
            has_title_match = True
            matched.append(template["name"])
        # 标题不符时，至少两个不同的必填章节全部出现，才按正文识别模板。
        elif not has_title_match and (
            (len(required_sections) >= spec.body_match_min_required_sections
             and required_sections <= sections.keys())
            or (not prefix_key and any(
                _section_key(field["attributes"]["label"]) in sections for field in fields))
        ):
            matched.append(template["name"])

    # 返回匹配模板列表和各模板的检查结果
    complete = [check["template"] for check in checks if not check["problems"] and not check.get("limitation")]
    if not has_title_match and len(matched) > 1:
        matched = []  # 正文对应多个候选时，交给推荐流程，不强行选定模板。
    status = "pass" if complete else "partial_match" if matched else "no_match"
    return {"status": status, "matched_templates": complete or matched, "checks": checks}

