"""规范性检查：根据核对和推荐结果生成评论。"""

import html
import re
from typing import Any

import yaml

from common.errors import ProcessingError
from issue_gov.checks.tools.compliance.parse import _fields
from issue_gov.checks.tools.compliance.rules import COMMENT_TEMPLATES, load_rules

def _escape_comment_text(value: str) -> str:
    """将模型文本转为评论中的单行文字，避免产生 HTML、链接或提及。

    Args:
        value: 已验证的模板名或理由。
    Returns:
        转义后的单行评论文字。
    """
    value = html.escape(" ".join(value.split()), quote=False).replace("@", "@\u200b")
    return re.sub(r"([\\`*_{\[\]}()!#>|~])", r"\\\1", value)


def render_comment_template(key: str, **fields: Any) -> str:
    """填入独立评论模板的占位符，不解析字段内容中的花括号。

    Args:
        key: comment_templates.yml 中的模板条目名称。
        **fields: 占位符名称及对应内容；须提供选中条目使用的全部占位符。
    Returns:
        完成一次占位符替换的 Markdown 字符串，字段内容不会被再次解析。
    Raises:
        OSError: 评论模板文件无法读取。
        yaml.YAMLError: 评论模板文件的 YAML 语法无效。
        KeyError: 模板条目不存在，或缺少必需的占位符值。
        ValueError: 模板中的格式占位符语法无效。
    """
    path = COMMENT_TEMPLATES
    templates = yaml.safe_load(path.read_text(encoding="utf-8"))
    return templates[key].format_map(fields)


def _format_template(template: dict, *, show_title: bool = True, show_body: bool = True,
                     show_name: bool = False) -> list[str]:
    """将仓库模板转为含可复制标题和 Markdown 正文的折叠块。

    Args:
        template: 已解析的 YAML 或 Markdown 模板。
        show_title: 是否展示可复制标题。
        show_body: 是否展示可复制正文。
        show_name: 是否在折叠栏显示名称，用于区分多个候选模板。
    Returns:
        折叠块的 Markdown 文本行列表，仅包含开关指定的标题、正文和名称。
        YAML 仅复制填写字段，保留字段提示、预填值和选项，跳过 markdown 展示说明；
        不填入 Issue 内容，Markdown 文件正文及文本框中的预置 Markdown 原样保留。
    Raises:
        ValueError: YAML 模板字段定义无效。
    """
    title = template.get("title", "")
    if template.get("format") == "markdown":
        body = template["markdown_body"]
    else:
        parts = []
        for field in _fields(template, expand_markdown=False):
            attrs = field["attributes"]
            if 'markdown_body' in field:
                if attrs.get('description'):
                    parts.extend(['<!-- ' + attrs['description'] + ' -->', ''])
                parts.extend([field['markdown_body'], ''])
                continue
            parts.extend(["### " + attrs["label"], ""])
            required = field.get("validations", {}).get("required") or (
                field["type"] == "checkboxes" and any(option.get("required") for option in attrs["options"]))
            spec = load_rules()
            hints = [spec.copy["required"] if required else spec.copy["optional"]]
            hints.extend(attrs[key] for key in ("description", "placeholder") if attrs.get(key))
            if field["type"] == "dropdown":
                prefix = spec.copy["dropdown_many"] if attrs.get("multiple") else spec.copy["dropdown_one"]
                hints.append(prefix + spec.messages["option_joiner"].join(attrs["options"]))
            parts.extend(["<!-- " + "\n".join(hints) + " -->", ""])
            if field["type"] == "checkboxes":
                content = "\n".join("- [ ] " + option["label"] + (spec.copy["checkbox_required"] if option.get("required") else "")
                                    for option in attrs["options"])
            elif field["type"] == "dropdown":
                content = attrs["options"][attrs["default"]] if "default" in attrs else ""
            else:
                content = attrs.get("value") or ""
                if attrs.get("render"):
                    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", content)), default=0))
                    content = f"{fence}{attrs['render']}\n{content}\n{fence}"
            parts.extend([content, ""])
        body = "\n".join(parts).strip()

    def code_fence(text: str) -> str:
        """生成不会被内容中的反引号提前闭合的代码围栏。

        Args:
            text: 要放入代码块的原始文本。
        Returns:
            至少三个反引号组成的字符串，长度大于文本中最长的连续反引号。
        """
        return "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", text)), default=0))

    content = []
    if show_title:
        content.append(render_comment_template("template_title", title=title, title_fence=code_fence(title)))
    if show_body:
        content.append(render_comment_template("template_body", body=body, body_fence=code_fence(body)))
    name = html.escape(" ".join(template["name"].split()), quote=False).replace("@", "@\u200b") + "：" if show_name else ""
    return render_comment_template("template", name=name, content="\n\n".join(content)).split("\n")


def format_compliance_check(result: dict[str, Any], *,
                             templates: list[dict] | tuple[dict, ...] = ()) -> str:
    """根据检查结果生成通过通知或需要贡献者修改 Issue 的评论。

    Args:
        result: 检查结果。通过时 status 为 pass，或未匹配且 recommendation.status 为
            not_applicable；部分匹配时包含 matched_templates 和 checks；未匹配时包含
            recommendation，其 status 为 recommended 或 insufficient_info。
        templates: 已解析的仓库模板，提供可复制的标题和正文。
    Returns:
        完整 Markdown 检查章节。通过时返回通过通知；部分匹配仅展示有问题的标题或正文；
        推荐成功展示指定模板，信息不足展示所有候选模板。
    Raises:
        ProcessingError: 检查或推荐状态不属于需要贡献者修改的有效结果。
    """
    # 1. 通过
    if result["status"] == "pass" or (
            result["status"] == "no_match"
            and result.get("recommendation", {}).get("status") == "not_applicable"):
        return render_comment_template("passed")
    # 2. 部分匹配
    if result["status"] == "partial_match":
        comments = []
        for check in result["checks"]:
            # 只反馈本次匹配到的模板
            if check["template"] in result["matched_templates"]:
                name = check["template"]
                # 提取未匹配的部分
                problems = "\n".join(
                    f"- {_escape_comment_text(p['field'])}：{_escape_comment_text(p['message'])}"
                    for p in check["problems"])
                selected = next((t for t in templates if t["name"] == name), None)
                template_text = ""
                if selected is not None:
                    show_title = any(p["reason"] == "title_prefix" for p in check["problems"])
                    show_body = any(p["reason"] != "title_prefix" for p in check["problems"])
                    template_lines = _format_template(selected, show_title=show_title, show_body=show_body)
                    template_text = "\n".join(template_lines)
                # 渲染评论
                comments.append(render_comment_template("partial_match",
                    name=_escape_comment_text(name), problems=problems, template=template_text))
        return "\n\n".join(comments)
    # 3. 未匹配
    if result["status"] == "no_match":
        recommendation = result.get("recommendation", {})
        decision = recommendation.get("status")
        # 3.1 已推荐具体模板：展示推荐原因和该模板的完整标题、正文
        if decision == "recommended":
            name = recommendation["template"]
            selected = next((t for t in templates if t["name"] == name), None)
            template_text = ""
            if selected is not None:
                template_lines = _format_template(selected)
                template_text = "\n".join(template_lines)
            # 渲染评论
            return render_comment_template("recommended", reason=_escape_comment_text(recommendation["reason"]), name=_escape_comment_text(name), template=template_text)
        # 3.2 信息不足：展示所有仓库模板，并在折叠栏注明名称，供贡献者补充后选择。
        if decision == "insufficient_info":
            template_blocks = ["\n".join(_format_template(t, show_name=True)) for t in templates]
            # 渲染评论
            return render_comment_template("insufficient_info", reason=_escape_comment_text(recommendation["reason"]), templates="\n\n".join(template_blocks))
        raise ProcessingError("模板推荐未返回需要贡献者修改的有效结果")
    raise ProcessingError("规范性检查未返回需要贡献者修改的有效结果")


def format_decision(decision: dict[str, Any], *, templates: list[dict] | tuple[dict, ...] = ()) -> str:
    """按评估决定生成回复。只包含开发者需要处理的部分。

    Args:
        decision: decide_review 返回的决定，使用 comment_kind 和 comment_parts。
        templates: 已解析模板，用于附上可复制内容。
    Returns:
        可发布的 Markdown。不需要回复时返回空字符串。
    """
    kind = decision.get("comment_kind")
    parts = decision.get("comment_parts") or {}
    if kind == "pass":
        return render_comment_template("review_passed")
    if kind == "template":
        selected = next((item for item in templates if item["name"] == parts.get("template")), None)
        block = "\n".join(_format_template(selected)) if selected is not None else ""
        return render_comment_template(
            "review_template", reason=_escape_comment_text(parts.get("reason") or ""),
            name=_escape_comment_text(parts.get("template") or ""), template=block)
    if kind == "insufficient":
        blocks = ["\n".join(_format_template(item, show_name=True)) for item in templates]
        return render_comment_template(
            "review_insufficient", reason=_escape_comment_text(parts.get("reason") or ""),
            templates="\n\n".join(blocks))
    if kind == "conflict":
        notes = "\n".join(f"- {_escape_comment_text(item['note'])}" for item in parts.get("conflicts") or [])
        return f"### 提交内容检查：未通过\n\n#### 内容冲突\n{notes}"
    if kind != "issues":
        return ""
    sections = ["### 提交内容检查：未通过", ""]
    structural = parts.get("structure") or []
    content = parts.get("content") or []
    conflicts = parts.get("conflicts") or []
    if structural:
        lines = "\n".join(
            f"- {_escape_comment_text(item['field'])}：{_escape_comment_text(item['message'])}"
            for item in structural)
        sections.extend(["#### 结构", lines, ""])
    if content:
        lines = "\n".join(
            f"- {_escape_comment_text(item['field'])}：{_escape_comment_text(item['message'])}"
            for item in content)
        sections.extend(["#### 必填内容", lines, ""])
    if conflicts:
        lines = "\n".join(f"- {_escape_comment_text(item['note'])}" for item in conflicts)
        sections.extend(["#### 内容冲突", lines, ""])
    return "\n".join(sections).strip()
