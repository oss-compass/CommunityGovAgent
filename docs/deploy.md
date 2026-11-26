# 容器部署

本机两套服务并存时的边界、灰度入口和接手步骤见 [handoff.md](handoff.md)。

接收服务、分析消费者、PostgreSQL 和 RabbitMQ 由仓库根目录的 `docker-compose.yml` 一起启动。检查规则、评论内容和模型推荐与原来的单机进程相同；变化的是任务如何传递和保存。

`manage.py` 与 Supervisor 的启动方式保持不变，仍然使用本机 SQLite。容器不经过 Supervisor，分别执行 `receiver.py` 和 `worker.py` 里的 `main()`。

## 架构

```text
AtomGit Webhook
    │  POST /webhook（校验 X-GitCode-Token）
    ▼
receiver（可多进程）
    │  写入任务和状态
    ▼
PostgreSQL ◀──────── consumer × N（竞争消费）
    ▲                    │
    │                    ├─ 读取 Issue、检查、必要时调用模型
    │                    ├─ 成功：更新任务并写入 reports
    │                    └─ 全部模型密钥失败：标记 failed，消息进入死信队列
RabbitMQ
    ├─ issue.analysis      待分析任务
    └─ issue.analysis.dlq  死信
```

同一 Issue 的新通知会把尚未完成的旧任务标为已覆盖。多个消费者同时拿到同一条消息时，只有一个能把状态从 `dispatching` 改为 `running`。处理中断后未确认的消息会重新投递；评论标记和待确认记录仍用来避免重复发评论。

可重试错误（平台超时、限流、网关 404 等）仍按原来的次数和退避写回数据库，由消费者里的重试线程到期后再投递。同一次 AtomGit 调用里，连接失败、502/503/504 和网关返回的非 JSON 404 会按客户端请求间隔再试 2 次；403、429 和接口 JSON 404 不在这一层重试。Issue 不存在时任务失败一次。模型密钥全部失败不再做任务级重试；模型结果格式不对只重试调用，不进入死信。

## 配置

两个应用容器挂载同一份文件：

| 文件 | 作用 |
| --- | --- |
| `issue-gov-agent/.env` | Webhook 密码、AtomGit 令牌、模型配置，以及数据库和队列的账号与口令 |
| `issue-gov-agent/repository.yml` | 接入仓库及维护者 |

全项目只有这一份 `.env`，不要在仓库根目录再创建。接收服务和全部消费者挂载同一份文件。容器进程只额外设置 `SERVICE_MODE=queue`，服务用文件中的账号和口令拼出连接串，主机名是 `postgres` 和 `rabbitmq`，容器内部端口是 `5432` 和 `5672`。`manage.py` 不设置 `SERVICE_MODE`，因此仍使用 SQLite，不会去解析这两个主机名。

应用容器以**执行 `scripts/service` 的用户身份**运行（脚本导出 `CONTAINER_UID`/`CONTAINER_GID`，Compose 的 `user:` 取自它们），与 `manage.py init` 生成的 `0600` 配置文件属主一致。需要以其他 uid 运行时，`CONTAINER_UID=1001 CONTAINER_GID=1001 scripts/service up`。直接执行 `docker compose` 时要自行导出这两个变量，否则默认 `1000:1000`。`scripts/service up` 会在启动前检查容器用户能否读取配置，不能时直接给出修复命令并退出。

口令只写在 `POSTGRES_PASSWORD` 和 `RABBITMQ_PASSWORD`。口令只用字母和数字。`init` 会把占位符换成随机值。宿主机映射端口由 `POSTGRES_PORT`、`RABBITMQ_PORT` 和 `RABBITMQ_MANAGEMENT_PORT` 决定，且只绑定在 `127.0.0.1`，不对外网开放；远程访问数据库或管理页面请走 SSH 隧道。本机直接连接时在进程环境中设置 `DATABASE_URL` 和 `RABBITMQ_URL`，不要把这两项写进 `.env`。

多个模型密钥写在 `AGENT_API_KEYS`，用英文逗号分隔。鉴权失败立即换下一个密钥。超时和进程启动失败先按 `LLM_KEY_RETRY_INTERVAL`（默认 10 秒）再试当前密钥 `LLM_KEY_RETRIES` 次（默认 2 次），然后切换。结果格式不对只重试这次调用，不计入密钥耗尽，任务按可重试失败回到队列。密钥本身全部失败后任务状态改为 `failed`，`dead_lettered` 为 1，消息进入 `issue.analysis.dlq`。只填写 `AGENT_API_KEY` 时，`manage.py` 仍按原来的任务级重试；容器里的消费者会对这一个密钥执行同样的分类重试，密钥失败后进入死信队列。

首次生成配置：

```bash
cd issue-gov-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py init
```

然后填写 `.env` 和 `repository.yml`。至少配置 `ATOMGIT_TOKEN`、`AGENT_API_KEY`（或 `AGENT_API_KEYS`）、`LLM_BASE_URL`、`LLM_MODEL`，并在 `repository.yml` 列出接入仓库。数据库和队列口令已在 `init` 时写入同一份 `.env`。

修改口令后需要同步数据库或 RabbitMQ 里的用户，或删除对应数据卷后重新创建。已经写在进程环境里的 `DATABASE_URL`、`RABBITMQ_URL` 也要一起改。

## 启动、扩容和停止

在仓库根目录执行：

```bash
docker compose --env-file issue-gov-agent/.env up -d --build
scripts/service status
scripts/service retry
scripts/service compose logs -f receiver consumer
scripts/service compose stop
```

`scripts/service up` 在配置缺失时只生成文件并退出，不会用空令牌启动。配置写好后再次执行才会构建镜像并启动。

消费者镜像包含 Node.js 22 和 Claude Code `2.1.316`，构建时需要能下载这两个组件。接收镜像不包含它们。Python 依赖按 `issue-gov-agent/requirements.lock` 安装，与 CI 使用同一组版本；更新依赖时同步修改锁文件。

消费者副本数写在 `docker-compose.yml` 的 `consumer.deploy.replicas`，当前是 1。修改后执行 `docker compose up -d` 即可增减容器。灰度机器上的 `/opt/CommunityGovAgent/docker-compose.yml` 使用同一个数字。`scripts/service up` 不再传入 `--scale`，不会盖过这个配置。

RabbitMQ 管理页面默认是 `http://127.0.0.1:15672`（用户和密码与 `RABBITMQ_USER`、`RABBITMQ_PASSWORD` 相同）。死信堆在 `issue.analysis.dlq`。处理完密钥或网络问题后执行 `scripts/service retry`，失败任务会重新进入分析队列，同时清空死信队列中对应上次失败的消息。

同一 Issue 的多条通知在 PostgreSQL 下按 Issue 加咨询锁串行入队，只会留下一条活跃任务，其余标为已覆盖。消费者领取任务时写入持有者标识。同一次处理中的消息重投只能接管一次；旧持有者的完成或失败写入会被拒绝，不会覆盖接管者的结果。

Webhook 仍是 `http://服务器:7000/webhook`，请求头 `X-GitCode-Token` 使用 `.env` 里的 `WEBHOOK_SECRET`。存活检查是 `GET /health`，数据库和队列都可连接时 `GET /ready` 返回 200。

## 本机原启动方式

不设置 `SERVICE_MODE=queue` 时，下面的命令与改造前一致：

```bash
cd issue-gov-agent
.venv/bin/python manage.py start
.venv/bin/python manage.py status
.venv/bin/python manage.py restart
.venv/bin/python manage.py stop
```

也可以不使用 Supervisor，在两个终端分别执行 `python -m issue_gov.receiver` 和 `python -m issue_gov.worker`。此时两者仍共享 SQLite，消费者不会连接 RabbitMQ。

## 验证

功能回归：

```bash
cd issue-gov-agent
.venv/bin/python -m unittest discover -s tests
```

PostgreSQL 路径测试默认跳过；本机验证时先启动数据库，再设置连接串（CI 已自动执行）。账号、口令和端口与 `issue-gov-agent/.env` 里的 `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_PORT` 一致。库名用独立的 `issue_gov_test`，不要指向正在使用的业务库；库不存在且该账号可以建库时，测试会创建它。

```bash
scripts/service compose up -d postgres
PG_TEST_DATABASE_URL=postgresql://issue:<口令>@127.0.0.1:54329/issue_gov_test \
  .venv/bin/python -m unittest discover -s tests
```

并发与死信（会清空该数据库里的 `events` 和 `reports`，只对验证库执行）：

```bash
scripts/service compose up -d postgres rabbitmq
python3 scripts/concurrency_check.py
```

脚本启动 4 个消费者，并发提交 80 条通知，确认每条只处理一次；再提交一条会耗尽模型密钥的任务，确认数据库为失败且死信队列增加一条。可用环境变量 `CONCURRENCY_REQUESTS` 和 `CONCURRENCY_CONSUMERS` 调整规模。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `scripts/service up` 提示容器用户无法读取配置 | 按提示 `chown <uid>` 该文件，或 `CONTAINER_UID=<uid> CONTAINER_GID=<gid> scripts/service up` |
| `scripts/service up` 提示填写配置 | 编辑 `issue-gov-agent/.env` 和 `repository.yml` 后重试 |
| `/ready` 持续失败 | `scripts/service compose logs receiver`，确认 Postgres 和 RabbitMQ 健康；数据库暂不可达时接收进程保持运行并每 2 秒重试，`/ready` 返回 `initializing` |
| 任务停在 `dispatching` | 消费者未运行或队列确认失败；启动消费者后，超过 60 秒会被重新投递 |
| 任务 `failed` 且 `dead_lettered=1` | 查看 `last_error` 和死信队列，修复密钥或模型配置后执行 `scripts/service retry` |
| 修改 `.env` 或仓库名单后未生效 | `scripts/service compose restart receiver consumer`；需要重建镜像时执行 `docker compose --env-file issue-gov-agent/.env up -d --build`，副本数仍以 `consumer.deploy.replicas` 为准 |
| 只想恢复单机 | 不要设置 `SERVICE_MODE`，继续使用 `manage.py` |
