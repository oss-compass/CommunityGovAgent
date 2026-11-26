"""把 Issue 正文切成可引用的片段。模型只能引用这里的编号。"""

from markdown_it import MarkdownIt
from markdown_it.token import Token

_MAX_SEGMENTS = 100
_MAX_TEXT = 4000


def _tokens(body: str) -> list[Token]:
    return MarkdownIt("commonmark").parse(body or "")


def _collect(tokens: list[Token]) -> list[tuple[str, str]]:
    """按块级 token 收集片段。同一个列表的所有列表项合并，保留步骤顺序。"""
    pieces: list[tuple[str, str]] = []
    list_text: list[str] = []
    list_depth = 0

    def flush() -> None:
        if list_text:
            pieces.append(("list", "\n".join(list_text)))
            list_text.clear()

    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            flush()
            pieces.append(("heading", tokens[index + 1].content.strip()))
        elif token.type in {"fence", "code_block"}:
            flush()
            pieces.append(("code", token.content.strip()))
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            list_depth += 1
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_depth -= 1
            if list_depth == 0:
                flush()
        elif token.type == "inline":
            if list_depth:
                list_text.append(token.content.strip())
            elif not index or tokens[index - 1].type != "heading_open":
                flush()
                pieces.append(("paragraph", token.content.strip()))
    flush()
    return pieces


def segment_body(body: str) -> list[dict[str, str]]:
    """按 Markdown 块切分正文，并为每块分配 S1、S2 这样的编号。

    标题、代码块各占一个片段。同一个列表的连续列表项合并成一个片段，
    保留复现步骤的顺序。段落各自独立。

    Args:
        body: Issue 原始 Markdown 正文。
    Returns:
        片段列表。每项含 id、kind 和 text。kind 为 heading、code、list 或 paragraph。
        空正文返回空列表。单个片段最多保留 4000 字，总数最多 100 个。
    """
    pieces = [(kind, text) for kind, text in _collect(_tokens(body)) if text]
    if not pieces and (body or "").strip():
        pieces.append(("paragraph", body.strip()))
    return [{"id": f"S{number}", "kind": kind, "text": text[:_MAX_TEXT]}
            for number, (kind, text) in enumerate(pieces[:_MAX_SEGMENTS], start=1)]
