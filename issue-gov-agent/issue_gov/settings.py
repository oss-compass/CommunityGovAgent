"""服务配置。接收进程和消费者都从这里读取，不再把配置定义放在 HTTP 入口里。"""

from dataclasses import dataclass, field
import os
from typing import Mapping
import re
from urllib.parse import quote

import yaml
from dotenv import dotenv_values

import issue_gov.paths as paths


CONFIG_KEYS = (
    "WEBHOOK_SECRET", "ATOMGIT_TOKEN", "LLM_API_KEY", "LLM_API_KEYS",
    "LLM_BASE_URL", "LLM_MODEL", "LLM_KEY_RETRIES", "LLM_KEY_RETRY_INTERVAL",
    "WEBHOOK_DB", "DATABASE_URL", "RABBITMQ_URL", "SERVICE_MODE",
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST",
    "RABBITMQ_USER", "RABBITMQ_PASSWORD", "RABBITMQ_HOST",
)

# 数据库和队列地址由账号和口令拼出。只有容器进程带 SERVICE_MODE=queue 时才采用。
# 未设置时忽略这些项，manage.py 继续使用 SQLite，不会解析容器主机名。
SERVICE_MODE = "queue"
_URL_OVERRIDE_KEYS = (
    "DATABASE_URL", "RABBITMQ_URL",
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST",
    "RABBITMQ_USER", "RABBITMQ_PASSWORD", "RABBITMQ_HOST",
)


@dataclass(frozen=True)
class Config:
    """webhook服务端采用统一配置；凭据不出现在对象的字符串表示中。

    Attributes:
        secret: 各接入仓库 WebHook 页面使用的共同密码。
        token: 有权读取接入仓库、Issue 和模板，并发布评论的个人访问令牌。
        repositories: 允许接入的仓库路径列表。
        database: 本机 SQLite 队列文件路径，接收与任务处理进程必须使用同一文件。
        agent_api_key: 所有接入仓库共用的模型接口密钥，使用 Bearer 认证。多个密钥时等于第一个。
        llm_base_url: 模型服务的 Anthropic 兼容接口地址。
        llm_model: 模型服务支持的模型名称。
        maintainers: 接入仓库到维护者账号列表的映射，空列表表示不 @。
        database_url: 服务模式下的 PostgreSQL 连接串；未开启服务模式时为空。
        rabbitmq_url: 服务模式下的 AMQP 连接串；未开启服务模式时为空。
        agent_api_keys: 按顺序尝试的模型密钥。未单独配置多密钥时只含 agent_api_key。
        llm_key_retries: 每个密钥在首次请求失败后的重试次数。
        llm_key_retry_interval: 同一密钥相邻两次请求的间隔秒数。
        llm_failover: 为 True 时按密钥轮换；manage.py 启动的单密钥模式保持一次调用。
    """

    secret: str = field(repr=False) # 凭据不出现在配置对象的打印结果中，避免意外写入日志；字段本身仍可读取
    token: str = field(repr=False)
    repositories: tuple[str, ...]
    database: str
    agent_api_key: str = field(default="", repr=False)
    llm_base_url: str = ""
    llm_model: str = ""
    maintainers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    database_url: str = field(default="", repr=False)
    rabbitmq_url: str = field(default="", repr=False)
    agent_api_keys: tuple[str, ...] = field(default_factory=tuple, repr=False)
    llm_key_retries: int = 2
    llm_key_retry_interval: float = 10.0
    llm_failover: bool = False

    @classmethod
    def from_file(cls) -> "Config":
        """读取当前运行配置，避免重启或重载时沿用 Supervisor 启动时的旧值。

        Args:
            无；配置文件固定为服务根目录的 .env。
        Returns:
            已校验的服务配置；配置文件缺失或内容无效时抛出 ValueError。
        """
        path = paths.env_file()
        if not path.is_file():
            raise ValueError("请先运行 manage.py init 并填写 .env")
        values = {key: value for key, value in dotenv_values(path, interpolate=False).items()
                  if value is not None}
        # 口令和主机写在 .env 中。只有容器进程带 SERVICE_MODE=queue 时才拼出连接串。
        # 未设置时丢弃连接串，manage.py 继续使用 SQLite，不会解析容器主机名。
        if os.environ.get("SERVICE_MODE", "").strip() == SERVICE_MODE:
            values["SERVICE_MODE"] = SERVICE_MODE
            for key in _URL_OVERRIDE_KEYS:
                override = os.environ.get(key, "").strip()
                if override:
                    values[key] = override
        else:
            for key in ("SERVICE_MODE", "DATABASE_URL", "RABBITMQ_URL"):
                values.pop(key, None)
        return cls.from_env(values)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        """读取环境配置，校验仓库与维护者名单。

        Args:
            environ: 配置映射；为 None 时使用进程环境变量。
        Returns:
            服务配置；缺少必填项或配置不合法时抛出 ValueError。
        """
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            """读取必填配置，拒绝空值和未替换的示例凭据。"""
            value = env.get(name, "").strip()
            if not value or value.startswith("replace-with-"):
                raise ValueError(f"请配置 {name}")
            return value

        repository_file = paths.repository_file()
        try:
            configured = yaml.safe_load(repository_file.read_text(encoding="utf-8"))
            if not isinstance(configured, dict) or not configured:
                raise ValueError
            maintainers = {}
            for repository, accounts in configured.items():
                if (not isinstance(repository, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                        or any(part in {".", ".."} for part in repository.split("/"))
                        or not isinstance(accounts, list)
                        or any(not isinstance(account, str) for account in accounts)):
                    raise ValueError
                accounts = [account.strip().removeprefix("@") for account in accounts]
                maintainers[repository] = tuple(dict.fromkeys(accounts))
        except (OSError, UnicodeError):
            raise ValueError("无法读取 repository.yml，请先运行 manage.py init 并检查文件权限") from None
        except (ValueError, yaml.YAMLError):
            raise ValueError("仓库及维护者配置格式无效，请在 repository.yml 中至少填写一个 owner/repo；无需维护者时使用 []") from None

        agent_api_keys, agent_api_key, llm_failover = _model_keys(env, required)
        database_url, rabbitmq_url = _service_urls(env)
        retries = _bounded_int(env, "LLM_KEY_RETRIES", 2, upper=10)
        interval = _bounded_float(env, "LLM_KEY_RETRY_INTERVAL", 10, upper=300)
        return cls(
            secret=required("WEBHOOK_SECRET"),
            token=required("ATOMGIT_TOKEN"),
            repositories=tuple(maintainers),
            database=env.get("WEBHOOK_DB", "").strip() or "./runtime/data/events.sqlite3",
            agent_api_key=agent_api_key,
            llm_base_url=required("LLM_BASE_URL"),
            llm_model=required("LLM_MODEL"),
            maintainers=maintainers,
            database_url=database_url,
            rabbitmq_url=rabbitmq_url,
            agent_api_keys=agent_api_keys,
            llm_key_retries=retries,
            llm_key_retry_interval=interval,
            llm_failover=llm_failover,
        )


def _model_keys(env: Mapping[str, str], required) -> tuple[tuple[str, ...], str, bool]:
    """解析一个或多个模型密钥。只配置 LLM_API_KEY 时不启用密钥轮换。"""
    raw = env.get("LLM_API_KEYS", "") or ""
    keys = tuple(dict.fromkeys(item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()))
    if any(key.startswith("replace-with-") for key in keys):
        raise ValueError("请配置 LLM_API_KEYS")
    if keys:
        return keys, keys[0], True
    single = required("LLM_API_KEY")
    return (single,), single, False


def _credential(env: Mapping[str, str], name: str) -> str:
    """读取拼连接串用的账号或口令。空值和示例占位符都拒绝。"""
    value = (env.get(name) or "").strip()
    if not value or value.startswith("replace-with-"):
        raise ValueError(f"服务模式下需要配置 {name}")
    return value


def _service_urls(env: Mapping[str, str]) -> tuple[str, str]:
    """服务模式用账号和口令拼出连接串。显式给出的连接串仍可覆盖，供本机验证使用。"""
    if (env.get("SERVICE_MODE") or "").strip() != SERVICE_MODE:
        return "", ""
    database_url = (env.get("DATABASE_URL") or "").strip()
    rabbitmq_url = (env.get("RABBITMQ_URL") or "").strip()
    if database_url or rabbitmq_url:
        if not database_url or not rabbitmq_url:
            raise ValueError("服务模式下需要同时配置 DATABASE_URL 和 RABBITMQ_URL")
        if not database_url.startswith("postgresql://") and not database_url.startswith("postgres://"):
            raise ValueError("DATABASE_URL 必须是 PostgreSQL 连接串")
        if not rabbitmq_url.startswith("amqp://") and not rabbitmq_url.startswith("amqps://"):
            raise ValueError("RABBITMQ_URL 必须是 AMQP 连接串")
        return database_url, rabbitmq_url
    database_url = "postgresql://{user}:{password}@{host}:5432/{name}".format(
        user=quote(_credential(env, "POSTGRES_USER"), safe=""),
        password=quote(_credential(env, "POSTGRES_PASSWORD"), safe=""),
        host=(env.get("POSTGRES_HOST") or "postgres").strip() or "postgres",
        name=quote(_credential(env, "POSTGRES_DB"), safe=""),
    )
    rabbitmq_url = "amqp://{user}:{password}@{host}:5672/%2F".format(
        user=quote(_credential(env, "RABBITMQ_USER"), safe=""),
        password=quote(_credential(env, "RABBITMQ_PASSWORD"), safe=""),
        host=(env.get("RABBITMQ_HOST") or "rabbitmq").strip() or "rabbitmq",
    )
    return database_url, rabbitmq_url


def _bounded_int(env: Mapping[str, str], name: str, default: int, *, upper: int) -> int:
    """读取非负整数配置。留空时使用默认值。"""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} 必须是 0 到 {upper} 之间的整数")
    value = int(raw)
    if value > upper:
        raise ValueError(f"{name} 必须是 0 到 {upper} 之间的整数")
    return value


def _bounded_float(env: Mapping[str, str], name: str, default: float, *, upper: float) -> float:
    """读取非负小数配置。留空时使用默认值。"""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是 0 到 {upper:g} 之间的数") from None
    if value < 0 or value > upper:
        raise ValueError(f"{name} 必须是 0 到 {upper:g} 之间的数")
    return value
