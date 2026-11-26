"""读取规范性检查的检查规范。词表和文案在配置文件中，匹配步骤仍在各工具模块。"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

import yaml

from issue_gov.paths import config_dir


def compliance_dir():
    """规范性检查的规范、评估模板和回复模板。导入时确定，不跟随测试里的临时根目录。"""
    return config_dir() / "checks" / "compliance"


RULES_FILE = compliance_dir() / "rules.yml"
RECOMMEND_PROMPT = compliance_dir() / "recommend_template.md"
ASSESS_PROMPT = compliance_dir() / "assess_prompt.md"
COMMENT_TEMPLATES = compliance_dir() / "comment_templates.yml"


@dataclass(frozen=True)
class CheckRules:
    """一次读取后的检查规范。正则已按文件中的写法编译。"""

    section_suffix: re.Pattern[str]
    normalize: dict[str, str]
    required_negation: re.Pattern[str]
    required_marker: re.Pattern[str]
    optional_heading: re.Pattern[str]
    instruction_headings: frozenset[str]
    placeholders: frozenset[str]
    title_colon: str
    title_closed_boundary: tuple[str, ...]
    body_match_min_required_sections: int
    messages: dict[str, str]
    copy: dict[str, str]


def _pattern(source: str) -> re.Pattern[str]:
    return re.compile(source, re.IGNORECASE)


@lru_cache(maxsize=4)
def _load(path: str) -> CheckRules:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    words = raw["section_hint_words"]
    hint = "(?:" + "|".join(words) + ")"
    separators = raw["section_hint_separators"]
    hints = hint + "(?:[" + separators + "]" + hint + ")*"
    suffix = rf"(?:\({hints}\)|（{hints}）|\[{hints}\])"
    return CheckRules(
        section_suffix=re.compile(rf"(?:{suffix})+$"),
        normalize=dict(raw["section_normalize"]),
        required_negation=_pattern(raw["required_negation"]),
        required_marker=_pattern(raw["required_marker"]),
        optional_heading=_pattern(raw["optional_heading"]),
        instruction_headings=frozenset(raw["instruction_headings"]),
        placeholders=frozenset(raw["placeholders"]),
        title_colon=raw["title_colon"],
        title_closed_boundary=tuple(raw["title_closed_boundary"]),
        body_match_min_required_sections=int(raw["body_match_min_required_sections"]),
        messages=dict(raw["messages"]),
        copy=dict(raw["copy"]),
    )


def load_rules() -> CheckRules:
    """读取当前配置目录中的检查规范。同一路径只解析一次。"""
    return _load(str(RULES_FILE))
