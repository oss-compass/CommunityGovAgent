# Issue 治理服务交接

这份说明给接下来接手本仓库的人。日常启动和扩容细节仍以 [deploy.md](deploy.md) 为准。这里写清代码边界、这台机器上正在跑的两套服务，以及哪些操作不能做。

## 这台机器上有两套服务

旧服务是正在使用的生产入口，新服务是灰度。两边共用同一枚 AtomGit 令牌和同一份仓库名单，但进程、数据库、队列和监听端口彼此独立。

| | 旧服务 | 灰度服务 |
| --- | --- | --- |
| 代码目录 | `/root/community-gov-agent/webhook` | 本仓库 `/root/CommunityGovAgent` |
| 进程 | Supervisor：`issue-gov-webhook`、`issue-gov-worker` | Compose 项目 `issue-gov` |
| 数据 | SQLite `runtime/data/events.sqlite3` | PostgreSQL，库名 `issue_gov` |
| 队列 | 无 RabbitMQ，进程内处理 | RabbitMQ |
| 监听 | `0.0.0.0:7000` | 只在 `127.0.0.1:7100`，容器内仍是 7000 |

不要停止、重启或重新部署旧服务。不要把 Nginx 指到 7000，也不要让 Compose 占用 7000。改灰度之前和之后各看一次。Supervisor 不在默认 PATH 里，要带上它自己的配置：

```bash
ss -ltnp | grep -E ':7000|:7100'
/root/community-gov-agent/webhook/.venv/bin/supervisorctl \
  -c /root/community-gov-agent/webhook/supervisord.conf status
ps -o pid,lstart,cmd -p 3863148
```

7000 上应仍是原来的 gunicorn，监听 `0.0.0.0:7000`。两项程序都应为 `RUNNING`。supervisord 主进程是 pid `3863148`，启动时间是 `Fri Nov 20 18:07:20 2026`。gunicorn 的 worker 可能换 pid，主进程的启动时间不应被这次操作改掉。

## 灰度怎么从公网进来

域名 `governance-agent.oss-compass.io` 的 Nginx 配置在 `/etc/nginx/sites-available/governance-agent`。下面四个地址都转发到 `http://127.0.0.1:7100/webhook`：

- `https://governance-agent.oss-compass.io/api/v1/issue`
- `https://governance-agent.oss-compass.io/api/v1/issue/`
- `https://governance-agent.oss-compass.io/api/v1/issue/webhook`
- `https://governance-agent.oss-compass.io/api/v1/issue/webhook/`

请求头是 `X-GitCode-Token`，值是 `issue-gov-agent/.env` 里的 `WEBHOOK_SECRET`。认证方式选 WebHook 密码，不要选签名密钥。POST 正文为 `application/json`，只订阅 Issue 事件。

`/health` 仍转到模拟后端 `127.0.0.1:18181`，不是新接收服务。`location /` 返回纯文本 `ok`。回退灰度时，把上述四个 `proxy_pass` 改回 `http://127.0.0.1:18181`，然后 `nginx -s reload`。模拟进程由 systemd 单元 `governance-agent-mock.service` 保持运行，不要为了灰度把它停掉。

现有仓库上已经指向 `http://113.44.237.121:7000/webhook` 的 Webhook 继续走旧服务。不要把那条生产 Webhook 改到灰度地址，除非明确要求切换。灰度要用另一条 Webhook，URL 用上面的 `/api/v1/issue/webhook`。

## 代码从哪里进

```text
AtomGit
  POST /webhook
      │
receiver（Gunicorn，python -m issue_gov.receiver）
      │  校验令牌、筛选 Issue、写入 PostgreSQL、投递消息
      ▼
RabbitMQ  issue.analysis
      │
consumer × N（python -m issue_gov.worker，prefetch 1）
      │  读 Issue 和模板、跑检查、必要时调模型、发一条评论
      ▼
PostgreSQL  events / reports
```

密钥本身全部失败时，消息进入 `issue.analysis.dlq`，交换器是 `issue.analysis.dlx`。其他失败不进死信。

目录分工：

- `common/`：数据库、队列、AtomGit 客户端、错误类型。不做成可安装包，也不要 `pip install -e`。
- `issue-gov-agent/issue_gov/`：接收、消费者、检查和评论。`issue_gov/__init__.py` 把仓库根目录放进 `sys.path`，从而导入 `common`。
- `issue-gov-agent/config/`：检查规则、推荐提示词、评论模板。结果文案改这里，不要改成另一套输出。
- `scripts/service`：仍可用来启动、查看状态和重试失败任务。副本数不在这里改。
- `manage.py`：本机 SQLite 和 Supervisor。它不设置 `SERVICE_MODE`，不会去连 Compose 里的 Postgres 或 RabbitMQ。

容器入口直接调用 `receiver.py` 和 `worker.py` 的 `main()`，不经过 Supervisor。Compose 给容器设置 `SERVICE_MODE=queue`，应用再用 `.env` 里的账号拼出连接串，主机名是 `postgres` 和 `rabbitmq`。

## 任务怎么走

状态：`queued`、`dispatching`、`running`、`completed`、`failed`、`superseded`。

同一 Issue 的新通知把尚未完成的旧任务标为 `superseded`。PostgreSQL 用 `pg_advisory_xact_lock` 按 Issue 串行入队。消费者领取时写入 `owner`。同一次 `running` 只允许重投接管一次；旧持有者再写完成或失败会被拒绝。

可重试错误最多 5 次，退避从 30 秒起、上限 300 秒。到期且状态为 `queued` 的任务，由消费者里的投递线程重新入队。`dispatching` 表示消息已经在队列里，不再按停留时间重复投递。正在 `running` 的任务不会被抢走。

评论在真正 POST/PATCH 之前先记下标记。响应丢失时，下次用这个标记到平台上查找，避免再发一条。

## 平台和模型失败时怎么处理

AtomGit 客户端默认每个进程每秒最多开始 1 次请求。这个间隔不跨进程共享，所以多个消费者叠在一起会超过平台限额。429 的冷却写在表 `api_cooldown`（`name='atomgit'`），本服务的所有消费者领取排队任务前都会看到。正在执行的那一次请求不会被中途拉回。旧服务不读这张表。

同一次 HTTP 调用内再试 2 次的情况：连接失败、超时、502、503、504，以及网关页面形式的 404（HTML、空正文或其他非 JSON）。间隔就是这个客户端自己的请求间隔。403、429 和接口返回的 JSON 404 不在这一层重试。

Issue 或路径确实不存在时，平台返回 JSON 404，任务失败一次，不发评论，不进死信。网关 404 短重试后仍失败，则按可重试错误回到队列。

模型调用只发生在规范性检查未匹配模板时，通过 Claude Code CLI。分类如下：

| 失败 | 行为 |
| --- | --- |
| 鉴权失败 | 立刻换下一个密钥 |
| 超时、进程启动失败 | 按 `LLM_KEY_RETRIES`（默认 2）和 `LLM_KEY_RETRY_INTERVAL`（默认 10 秒）重试当前密钥，然后换密钥 |
| 结果不是合法推荐 JSON | 只重试这次调用，不计入密钥耗尽，不进死信 |
| 全部密钥都因鉴权或超时失败 | `failed`，`dead_lettered=1`，消息进死信 |
| 未配置密钥或模型地址 | 立即失败，不睡眠、不轮换 |

容器里即使只填了 `LLM_API_KEY`，也会走这套分类。`manage.py` 的单机路径在没有 `LLM_API_KEYS` 时仍是一次调用失败后按任务重试。

还没有做的：模型连续超时后的熔断、评论分页上限、数据库语句超时、消费者就绪检查是否连着队列、把代理变量传进 Claude 子进程。不要顺手加这些，除非这次任务明确要求。

## 配置文件

这台机器的灰度配置在 `/opt/CommunityGovAgent/`。仓库里的 `issue-gov-agent/.env`、`issue-gov-agent/repository.yml` 和 `docker-compose.override.yml` 是指到该目录的符号链接，方便 `scripts/service` 继续使用原来的路径。不要在仓库根目录再放 `.env`，也不要把 `DATABASE_URL`、`RABBITMQ_URL` 写进环境文件。这两个连接串由程序自己拼。

| 文件 | 是否提交 | 内容 |
| --- | --- | --- |
| `/opt/CommunityGovAgent/.env` | 否 | Webhook 密码、AtomGit 令牌、模型密钥、数据库和队列口令 |
| `/opt/CommunityGovAgent/repository.yml` | 否 | 接入仓库和维护者 |
| `/opt/CommunityGovAgent/docker-compose.yml` | 否 | 灰度编排入口。项目名 `issue-gov`，镜像从 `/root/CommunityGovAgent` 构建。`consumer.deploy.replicas` 与仓库里的同名字段保持一致 |
| `/opt/CommunityGovAgent/docker-compose.override.yml` | 否 | 接收端口只绑 `127.0.0.1:7100`，并挂载上面两份文件 |

当前接入的仓库是 `cann/infrastructure`、`cann/community`、`cann/cannbot-skills`、`cann/runtime`、`cann/catlass`，每个仓库 1 名维护者。模型是智谱 `glm-5.3`，地址前缀为 `https://open.bigmodel.cn/api/anthropic`。密钥和口令只在 `.env` 里看，不要写进文档、提交信息或对话摘录。

`/opt/CommunityGovAgent/docker-compose.override.yml` 必须保留，内容是：

```yaml
services:
  receiver:
    ports: !override
      - "127.0.0.1:7100:7000"
```

没有它时，Compose 会按 `.env` 的 `WEBHOOK_PORT` 发布端口。这份文件里该值是 `7100`，容器内的 `WEBHOOK_PORT` 仍由 Compose 设为 `7000`。`scripts/service` 用 `.env` 里的 `7100` 做宿主机就绪检查。

应用容器的 uid 是执行 `scripts/service` 的用户。这台机器当前以 root 执行，因此容器也是 root，才能读取 `0600` 的配置。排查时用 `CONTAINER_UID`，不要用 bash 的 `UID`。

## 端口

全部调试端口只绑在 `127.0.0.1`。

| 端口 | 服务 |
| --- | --- |
| 7000 | 旧服务，不要占用 |
| 7100 | 灰度接收服务 |
| 54329 | 灰度 PostgreSQL |
| 56729 | 灰度 RabbitMQ |
| 15672 | 灰度 RabbitMQ 管理页，账号与 `RABBITMQ_USER` / `RABBITMQ_PASSWORD` 相同 |

## 更新灰度

日常在 `/opt/CommunityGovAgent` 管理这一套容器。`docker compose` 会读同目录的 `.env` 和 `docker-compose.override.yml`，项目名是 `issue-gov`，数据卷仍是原来的两份。镜像构建使用 `/root/CommunityGovAgent` 里的代码。

```bash
cd /opt/CommunityGovAgent
docker compose ps
docker compose up -d
docker compose logs -f receiver consumer
```

消费者副本数写在两份编排文件的 `consumer.deploy.replicas`，当前是 1。改数量时两处一起改，然后在 `/opt/CommunityGovAgent` 执行 `docker compose up -d`。

从仓库根目录执行 `scripts/service up` 也会操作同一套容器，并按 `requirements.lock` 重建镜像。它不再传入 `--scale`，副本数以编排文件为准。数据卷 `issue_gov_postgres` 和 `issue_gov_rabbitmq` 会保留。

不要对正在服务的 RabbitMQ 运行 `scripts/concurrency_check.py`。它使用同一个 `issue.analysis` 队列，并会删除 `events`。PostgreSQL 回归要用独立库名 `issue_gov_test`：

```bash
cd issue-gov-agent
PG_TEST_DATABASE_URL=postgresql://issue:<口令>@127.0.0.1:54329/issue_gov_test \
  .venv/bin/python -m unittest discover -s tests
```

不设置 `PG_TEST_DATABASE_URL` 时，该文件会跳过，其余测试仍用临时 SQLite。从 `issue-gov-agent` 目录执行，不要从仓库根目录执行，否则找不到 `issue_gov`。

改完代码后的最小验证：

```bash
cd issue-gov-agent
.venv/bin/python -m unittest discover -s tests
ruff check ../common issue_gov ../scripts
```

灰度更新后确认：

```bash
curl -sS http://127.0.0.1:7100/ready
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:7100/webhook -H 'Content-Type: application/json' -d '{}'
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://governance-agent.oss-compass.io/api/v1/issue/webhook -H 'Content-Type: application/json' -d '{}'
```

无令牌时应为 401，正文 `{"error":"unauthorized"}`。带对的 `X-GitCode-Token` 且正文是 `{}` 时为 200，正文 `{"status":"ignored"}`。然后再看 7000 的进程没有换过。

死信里的任务修好密钥或网络后，执行 `scripts/service retry`。它把失败任务重新放回分析队列，并清掉对应的死信消息。

## 接手时不要做的事

- 不要动 Supervisor 管理的旧服务，不要改它的 `runtime/.env` 和 `runtime/repository.yml`。
- 不要把 `.env`、`repository.yml`、`docker-compose.override.yml` 提交进 Git。
- 不要在灰度消费者还连着队列时跑 `scripts/concurrency_check.py`。
- 不要用生产 Issue 做批量压测。模拟编号也会真实调用 AtomGit，和旧服务共用令牌。
- 不要为了灰度去改已经指向 7000 的生产 Webhook。
