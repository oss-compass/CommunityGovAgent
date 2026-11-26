"""规范性检查：在格式未匹配时调用模型推荐模板。"""

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Any

from common.errors import ProcessingError
from issue_gov.checks.tools.compliance.parse import _fields
from issue_gov.checks.tools.compliance.rules import RECOMMEND_PROMPT

LLM_TIMEOUT = 120
_AUTH_MARKERS = (
    "authentication_error",
    "authentication error",
    "invalid api key",
    "invalid x-api-key",
    "invalid authentication",
    "unauthorized",
    "permission_error",
    "please run /login",
    "not logged in",
    "http 401",
    "status 401",
    "401 unauthorized",
    "403 forbidden",
    "http 403",
    "invalid bearer",
)


def _is_auth_failure(detail: str) -> bool:
    """根据模型进程返回的错误文本判断是否为密钥鉴权失败。"""
    lowered = detail.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)

def invoke_model(
    system_prompt: str, payload: str, agent_api_key: str, model: str, base_url: str, *,
    max_turns: str = "3", purpose: str = "模板推荐",
) -> dict[str, Any]:
    """启动一次 Claude Code 任务，要求模型只返回一个 JSON 对象。

    Args:
        system_prompt: 本次任务的系统提示词。
        payload: 传给模型的 JSON 文本。
        agent_api_key: Claude Code 兼容接口的 Bearer 认证密钥。
        model: 服务商提供的模型名称。
        base_url: 服务商提供的 Anthropic 兼容接口地址。
        max_turns: Claude Code 最大轮次。模板推荐保持 3，内容评估使用 1。
        purpose: 失败提示中的任务名称。
    Returns:
        模型输出 JSON 解析后的字典。
    Raises:
        ProcessingError: 配置缺失、调用失败、超时或响应不是 JSON 对象。
    """
    if not agent_api_key.strip():
        raise ProcessingError(f"未配置 Agent Key，无法执行{purpose}")
    if not model.strip() or not base_url.strip():
        raise ProcessingError(f"未配置 LLM_BASE_URL 或 LLM_MODEL，无法执行{purpose}")
    try:
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
               if key in os.environ}
        env.update({
            "ANTHROPIC_AUTH_TOKEN": agent_api_key,
            "ANTHROPIC_BASE_URL": base_url.strip(),
            "API_TIMEOUT_MS": str(LLM_TIMEOUT * 1000),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        })
        command = [
            "claude", "--bare", "-p", "--model", model.strip(),
            "--tools", "", "--no-session-persistence", "--max-turns", max_turns,
            "--output-format", "json",
            "--system-prompt", system_prompt,
        ]
        with tempfile.TemporaryDirectory(prefix="issue-gov-") as directory:
            env["CLAUDE_CONFIG_DIR"] = str(Path(directory) / ".claude")
            with subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=directory, env=env, start_new_session=True,
            ) as process:
                try:
                    output, stderr = process.communicate(payload, timeout=LLM_TIMEOUT)
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        try:
            envelope = json.loads(output)
        except ValueError:
            raise ValueError(stderr.strip() or output.strip() or "Claude Code 未返回结果") from None
        if not isinstance(envelope, dict):
            raise ValueError("Claude Code 返回格式无效")
        if (process.returncode != 0 or envelope.get("type") != "result"
                or envelope.get("subtype") != "success" or envelope.get("is_error") is not False):
            raise ValueError(envelope.get("result") or envelope.get("errors")
                             or stderr.strip() or "Claude Code 调用失败")
        parsed = json.loads(envelope["result"])
        if not isinstance(parsed, dict):
            raise ValueError("模型结果格式无效")
        return parsed
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            raise ProcessingError(f"模型响应超时，未能完成{purpose}。", failure="timeout") from None
        if isinstance(exc, OSError):
            raise ProcessingError(f"模型程序运行失败，未能完成{purpose}。", failure="timeout") from None
        if _is_auth_failure(str(exc)):
            raise ProcessingError(f"模型鉴权失败，未能完成{purpose}。", failure="auth") from None
        raise ProcessingError(f"模型未返回有效的结果，未能完成{purpose}。", failure="invalid_output") from None


def recommend_template(
    title: str, body: str, templates: list[dict[str, Any]],
    agent_api_key: str, model: str = "", base_url: str = "",
) -> dict[str, Any]:
    """启动一次 Claude Code 任务，调用配置的模型推荐模板，不修改格式检查结果。

    Args:
        title: Issue 原始标题。
        body: Issue 原始 Markdown 正文。
        templates: 仓库候选模板字典列表。
        agent_api_key: Claude Code 兼容接口的 Bearer 认证密钥；为空时不调用模型。
        model: 服务商提供的模型名称。
        base_url: 服务商提供的 Anthropic 兼容接口地址。
    Returns:
        一个字典，包含四个字段：
            status 为 recommended / not_applicable / insufficient_info；
            template 为推荐的模板名称；
            reason 为原因；
            maintainer 为可选的维护者建议文本。
    Raises:
        ProcessingError: 配置缺失、调用失败、超时或响应无效；交由任务队列重试。
    """
    if not agent_api_key.strip():
        raise ProcessingError("未配置 Agent Key，无法执行模板推荐")
    if not model.strip() or not base_url.strip():
        raise ProcessingError("未配置 LLM_BASE_URL 或 LLM_MODEL，无法执行模板推荐")
    # 将模板信息整理为json传给模型
    candidates = [{
        "name": item["name"], "description": item.get("description", ""),
        "title": item.get("title", ""),
        "markdown_body": item.get("markdown_body", ""),
        "fields": [{"label": field["attributes"]["label"],
                    "description": field["attributes"].get("description", ""),
                    "type": field["type"], "options": field["attributes"].get("options", []),
                    "placeholder": field["attributes"].get("placeholder", ""),
                    "value": field["attributes"].get("value", ""),
                    "required": field.get("validations", {}).get("required", False)}
                   for field in _fields(item)],
    } for item in templates]

    data = json.dumps({"issue": {"title": title, "body": body},
                       "templates": candidates}, ensure_ascii=False)
    suggestion = invoke_model(
        RECOMMEND_PROMPT.read_text(encoding="utf-8"), data, agent_api_key, model, base_url)
    try:
        status = suggestion["status"]
        name = suggestion["template"]
        reason = suggestion["reason"]
        maintenance = suggestion.get("maintainer")
        if status not in {"recommended", "not_applicable", "insufficient_info"}:
            raise ValueError("推荐状态无效")
        if status == "recommended":
            if name not in {item["name"] for item in templates}:
                raise ValueError("推荐模板无效")
        elif name is not None:
            raise ValueError("未推荐时不能指定模板")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("推荐理由无效")
        if status == "not_applicable" and maintenance is None:
            raise ValueError("没有适用模板时须提供维护者建议")
        if maintenance is not None:
            if not isinstance(maintenance, str) or not 1 <= len(maintenance.strip()) <= 500:
                raise ValueError("维护者建议无效")
            maintenance = maintenance.strip()
        return {"status": status, "template": name, "reason": reason.strip(), "maintainer": maintenance}
    except (ValueError, TypeError, KeyError):
        raise ProcessingError("模型未返回有效的推荐结果，未能完成模板推荐。", failure="invalid_output") from None