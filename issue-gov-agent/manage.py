"""普通用户管理 Webhook 服务：初始化配置、启动、停止、重启更新和查看状态。"""

import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time

from dotenv import dotenv_values, set_key


DIRECTORY = Path(__file__).resolve().parent
RUNTIME_DIRECTORY = DIRECTORY / "runtime"  # 日志、SQLite 和本机进程套接字
SUPERVISOR_CONFIG = DIRECTORY / "supervisord.conf"
# 这些占位符会同时出现在口令项和连接串中，生成时必须换成同一个随机值。
_GENERATED_PLACEHOLDERS = (
    "replace-with-a-random-secret",
    "replace-with-a-database-password",
    "replace-with-a-queue-password",
)


def _materialize(content: str, generated: dict[str, str] | None = None) -> str:
    """把示例中的口令占位符换成随机值。

    Args:
        content: 示例文件全文，或单个新增配置项的值。
        generated: 已生成的占位符到随机值的映射；同一占位符只生成一次。
    Returns:
        替换后的文本。没有占位符时原样返回。
    """
    values = {} if generated is None else generated
    for placeholder in _GENERATED_PLACEHOLDERS:
        if placeholder not in content:
            continue
        if placeholder not in values:
            values[placeholder] = secrets.token_hex(32)
        content = content.replace(placeholder, values[placeholder])
    return content


def initialize() -> None:
    """创建运行目录和配置，保留已有值并补充示例中新增的环境配置项。

    Args:
        无；配置模板和运行目录由模块中的路径常量确定。
    Returns:
        None；输出初始化、补充配置项或保留已有配置的提示；不输出配置值。
    """

    # 创建运行目录
    RUNTIME_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    for name in ("data", "logs", "run"):
        (RUNTIME_DIRECTORY / name).mkdir(mode=0o700, exist_ok=True)

    # 设置配置。环境文件与代码目录同级，不放进 runtime。
    path = DIRECTORY / ".env"
    if path.exists():
        current = dotenv_values(path, interpolate=False)
        defaults = dotenv_values(DIRECTORY / ".env.example", interpolate=False)
        missing = [key for key in defaults if key not in current]
        generated: dict[str, str] = {}
        for key in missing:
            set_key(path, key, _materialize(defaults[key] or "", generated))
        if missing:
            path.chmod(0o600)
            print(f"已补充配置项：{', '.join(missing)}；已有值保持不变。\n"
                  f"请检查 {path} 中新增项的默认值，填写必要配置后重启服务。")
        else:
            print(f"配置已存在，无新增配置项，保留原内容：{path}")
    else:
        # 不存在时创建env并生成随机密钥
        content = _materialize((DIRECTORY / ".env.example").read_text(encoding="utf-8"))
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
        path.chmod(0o600)
        print(f"已生成配置：{path}\n请填写 ATOMGIT_TOKEN，并配置大模型：LLM_API_KEY（密钥）、LLM_BASE_URL（接口地址）和 LLM_MODEL（模型名称）。数据库和队列口令已写在同一文件中。")

    # 创建仓库及维护者配置
    repository_path = DIRECTORY / "repository.yml"
    if repository_path.exists():
        print(f"仓库配置已存在，保留原内容：{repository_path}")
    else:
        example = Path(__file__).resolve().parent / "repository.yml.example"
        with repository_path.open("x", encoding="utf-8") as stream:
            stream.write(example.read_text(encoding="utf-8"))
        repository_path.chmod(0o600)
        print(f"已生成仓库配置文件：{repository_path}，请按注释示例填写全部接入仓库及其维护者。")


def environment() -> dict[str, str]:
    """读取并验证本地配置，返回环境信息。

    Args:
        无；从服务根目录的 .env 和当前进程环境读取配置。
    Returns:
        包含服务配置的环境变量字典；缺少配置或配置无效时抛出 ValueError。
    """
    path = DIRECTORY / ".env"
    if not path.is_file():
        raise ValueError("请先运行 manage.py init，再填写 .env 中的令牌和仓库信息。")
    
    # 读取并解析 .env 文件
    values = dotenv_values(path, interpolate=False)
    from issue_gov.settings import CONFIG_KEYS, Config
    env = {key: value for key, value in os.environ.items() if key not in CONFIG_KEYS}
    env.update({key: value for key, value in values.items() if value is not None})
    env["WEBHOOK_PYTHON"] = sys.executable
    # 端口从本部署的 .env 读取，未配置或留空时默认使用 7000。
    port = (values.get("WEBHOOK_PORT") or "").strip() or "7000"
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("WEBHOOK_PORT 必须是 1 到 65535 之间的整数。")
    env["WEBHOOK_PORT"] = str(int(port))
    Config.from_env(env) # 验证配置有效性，缺少或无效时抛出 ValueError
    return env


def control(*arguments: str) -> subprocess.CompletedProcess[str]:
    """执行 Supervisor 管理命令，返回执行结果，不直接在终端显示命令输出。

    Args:
        arguments: supervisorctl 子命令及参数。
    Returns:
        含退出码、标准输出和错误输出的命令结果。
    """
    return subprocess.run(
        [sys.executable, "-m", "supervisor.supervisorctl", "-c", str(SUPERVISOR_CONFIG),
         *arguments], cwd=DIRECTORY, capture_output=True, text=True, timeout=60,
        env={**os.environ, "WEBHOOK_PYTHON": sys.executable, "WEBHOOK_PORT": "7000"},
    )


def running() -> bool:
    """检查当前项目的 Supervisor 是否已启动并能响应。

    Args:
        无。
    Returns:
        Supervisor 响应成功且返回有效进程编号时为 True，否则为 False。
    """
    result = control("pid")
    return result.returncode == 0 and result.stdout.strip().isdigit() and int(result.stdout) > 0


def start(env: dict[str, str]) -> None:
    """启动后台服务，等待接收与任务处理两项程序就绪；重复启动时保留已有实例。

    Args:
        env: 已验证的配置环境，传递给管理进程及其子进程。
    Returns:
        None；启动成功或服务已运行时输出状态，启动失败或未能就绪时抛出异常。
    """
    if running():
        print("服务已启动；修改代码或配置后使用 restart。")
        status()
        return
    
    RUNTIME_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    for name in ("data", "logs", "run"):
        (RUNTIME_DIRECTORY / name).mkdir(mode=0o700, exist_ok=True)
    
    # 启动 Supervisor 管理进程
    subprocess.run(
        [sys.executable, "-m", "supervisor.supervisord", "-c", str(SUPERVISOR_CONFIG)],
        cwd=DIRECTORY, env=env, check=True,
    )

    # 等待接收与任务处理两个子进程就绪，最多等待 20 秒
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = control("status")
        if result.returncode == 0 and result.stdout.count("RUNNING") == 2:
            print("服务已启动，正在后台运行。")
            print(result.stdout.strip())
            return
        if "FATAL" in result.stdout:
            break
        time.sleep(0.5)
    raise RuntimeError("服务未能就绪，请运行 manage.py logs 查看原因；修复后运行 restart。")


def stop() -> None:
    """停止接收与任务处理两项服务及管理进程，等待退出后才允许重新启动。

    Args:
        无。
    Returns:
        None；服务已停止或原本未运行时返回，停止失败或超时时抛出 RuntimeError。
    """
    if not running():
        print("服务未运行。")
        return

    # 停止 Supervisor 管理进程及其子进程
    result = control("shutdown")
    if result.returncode != 0:
        raise RuntimeError("无法停止服务，请运行 manage.py status 或 logs 检查。")

    # 等待 Supervisor 管理进程退出，最多等待 65 秒
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        if not (RUNTIME_DIRECTORY / "run/supervisor.sock").exists():
            print("服务已停止。")
            return
        time.sleep(0.5)
    raise RuntimeError("停止服务超时，请检查 runtime/logs/supervisord.log。")


def status() -> None:
    """查看服务是否运行，并显示各仓库的任务数量和最近任务。

    服务停止后仍可查看当前 .env 指定的数据库，不会启动服务或修改任务。
    服务未运行、状态查询失败或配置及数据库读取失败时，命令以非零退出码结束。

    Args:
        无；使用 .env 指定的数据库及本项目的 Supervisor。
    Returns:
        None；结果输出到终端。服务未运行或状态查询失败时抛出非零退出码的 SystemExit。
    """
    # 查询两个子进程的状态
    print("服务进程：")
    exit_code = 0
    if running():
        result = control("status")
        print(result.stdout.strip() or "无法读取进程状态。")
        exit_code = result.returncode
    else:
        print("服务未运行。")
        exit_code = 1

    # 读取当前配置和数据库中的任务统计，即使服务未运行也继续查询
    from issue_gov.receiver import Config, queue_snapshot

    config = Config.from_env(environment())
    snapshot = queue_snapshot(config)
    print(f"\n任务数据库（当前配置）：{snapshot['database']}")
    if not snapshot["exists"]:
        print("数据库尚未创建；启动服务并接收通知后会有任务记录。")
    else:
        # 将任务状态转换为中文，并为配置中的每个仓库准备初始为 0 的统计数量
        labels = {"queued": "等待处理", "running": "正在检查", "completed": "已完成",
                  "failed": "处理失败"}
        counts = {repository: dict.fromkeys(labels, 0) for repository in config.repositories}
        # 填入数据库中的实际数量，同时保留数据库中记录的其他仓库
        for row in snapshot["counts"]:
            state = "completed" if row["state"] == "superseded" else row["state"]
            counts.setdefault(row["repository"], dict.fromkeys(labels, 0))[state] += row["count"]

        # 按仓库显示尚需处理的 Issue 数量和已完成历史任务数量
        print("各仓库任务数量：")
        for repository, states in counts.items():
            summary = "，".join(f"{label} {states[state]}" for state, label in labels.items())
            print(f"  {repository}：{summary}")

        # 显示最近更新的任务，包括状态、尝试次数、更新时间、评论处理结果和错误信息
        print("最近更新的任务（最多 5 条）：")
        if not snapshot["recent"]:
            print("  暂无任务。")
        labels["superseded"] = "已被新请求覆盖"
        for row in snapshot["recent"]:
            updated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(row["updated_at"]))
            details = (f"  {row['repository']} #{row['issue_number']}："
                       f"{labels.get(row['state'], row['state'])}，尝试 {row['attempts']} 次，{updated}")
            if row["result"]:
                result = json.loads(row["result"])
                actions = {"created": "已发布评论", "updated": "已更新评论", "unchanged": "评论无变化",
                           "suppressed": "静默处理，未发表评论",
                           "skipped_closed": "跳过已关闭的 Issue", "recovered": "已确认先前评论，未重复发布"}
                label_actions = {"assigned": "已写入", "rejected": "未写入", "skipped": "未分配"}
                details += "，" + actions.get(result.get("comment_action"), "已处理")
                if result.get("label_action"):
                    label_text = label_actions.get(result["label_action"], result["label_action"])
                    if result.get("label"):
                        label_text += " " + result["label"]
                    details += "，标签" + label_text
            if row["last_error"]:
                details += f"，最近错误 {row['last_error']}"
            print(details)
        print("检查已完成表示服务器已处理（包含已被新请求覆盖的任务）。")

    # 任务信息展示完后，再用退出码告知调用方服务未运行或进程状态查询失败
    if exit_code:
        raise SystemExit(exit_code)


def main() -> None:
    """解析管理命令，自动加载配置、设置工作目录并报告简短错误。

    Args:
        无显式参数；管理命令从 sys.argv 读取。
    Returns:
        None；管理操作正常完成后返回。logs 命令会用日志查看进程替换当前进程，
        参数无效或操作失败时通过 SystemExit 返回非零退出码。
    """

    # 解析命令行参数
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "start", "stop", "restart", "status",
                                           "logs", "retry-failed"))
    args = parser.parse_args()

    # 设置 umask 和工作目录
    os.umask(0o077) # 权限屏蔽规则，避免日志和数据库被其他用户读取
    os.chdir(DIRECTORY)

    # 执行命令
    try:
        if args.command == "init":
            # 初始化：创建目录和配置文件，不运行服务
            initialize()
        elif args.command == "start":
            # 启动服务
            start(environment())
        elif args.command == "restart":
            # 统一日常更新：补充配置、检查代码和配置、重启后台、平滑重载接收端。
            if not running():
                raise RuntimeError("服务未运行，请先使用 start")
            initialize()  # 只补缺失项，保留已有值；日常更新无需手动 init。
            env = environment()
            # 重启前检查代码导入和配置是否有效。
            check = "import issue_gov.worker; from issue_gov.receiver import Config; Config.from_file()"
            result = subprocess.run([sys.executable, "-c", check], cwd=DIRECTORY, env=env,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError("新代码或配置检查失败，未操作运行中的服务；请检查代码、依赖和配置")
            result = control("restart", "issue-gov-worker")
            if result.returncode:
                raise RuntimeError("后台重启失败，未重载接收端；请运行 manage.py logs 查看原因")
            # HUP 保留 Gunicorn 主进程和监听端口，仅替换 HTTP 工作进程。
            result = control("signal", "HUP", "issue-gov-webhook")
            if result.returncode:
                raise RuntimeError("后台已重启，但接收端重载信号发送失败；请查看 logs，修复后重试 restart")
            print("后台已重启，接收端已收到平滑重载信号；中断任务将自动恢复。请通过 logs 确认新接收进程正常启动。")
        elif args.command == "stop":
            # 停止服务
            stop()
        elif args.command == "status":
            # 查询服务状态和任务信息
            status()
        elif args.command == "logs":
            # 查看日志
            paths = [RUNTIME_DIRECTORY / "logs" / name for name in
                     ("supervisord.log", "webhook.log", "worker.log")]
            if not all(path.exists() for path in paths):
                raise ValueError("日志尚未生成，请先启动服务；启动报错时检查 runtime/logs/ 目录。")
            os.execvp("tail", ["tail", "-n", "40", "-f", *map(str, paths)])
        elif args.command == "retry-failed":
            # 重新入队失败的任务，返回重新入队的数量
            from issue_gov.receiver import Config
            from issue_gov.worker import retry_failed
            count = retry_failed(Config.from_env(environment()))
            print(json.dumps({"requeued": count}))
    except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
