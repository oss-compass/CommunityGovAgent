# Issue 治理服务

本文是给其他 agent 接入和部署本服务的单独说明，不依赖仓库里的其他文档。

本服务没有任务查询接口。接入方只向 Webhook 投递 Issue 事件。处理结果写在 PostgreSQL 的 `events.result`，不在 HTTP 响应里。

## 服务介绍

服务接收 AtomGit / GitCode 的 Issue Webhook，检查标题和正文是否符合仓库 Issue 模板，由模型确认模板是否用对、必填内容是否提供、正文是否自相矛盾，然后在 Issue 上发表或更新一条评论，并按结论写入检查标签。

它不判断问题是否真实，也不对照仓库代码。正确性只核对同一 Issue 内部的陈述能否自洽。同一 Issue 同时只有一条活跃任务。新通知会把尚未完成的旧任务标为 `superseded`。

```text
GitCode Issue 事件
    │  POST /webhook，请求头 X-GitCode-Token
    ▼
接收服务 receiver
    │  校验密码、筛选事件、写入 PostgreSQL、投递消息
    ▼
RabbitMQ  issue.analysis
    │
消费者 consumer × N
    │  读取 Issue 和模板、做程序检查；已配置模型时确认模板、内容和正文是否自洽
    │  发表或更新一条评论，并写入检查标签
    ▼
PostgreSQL  events / reports
```

处理结果：

| 情况 | 检查状态 | 评论 | 标签 |
| --- | --- | --- | --- |
| 模板用对，必填内容齐全，正文无冲突 | `pass` | 此前没有评论则不发；已有评论且其后没有其他评论则改为通过，其后已有评论则另发一条 | `issue-check-passed` |
| 模板用对，但有必填内容缺失、选项非法、必选未勾选或正文冲突 | `partial_match` | 只列出有问题的项 | `issue-check-failed` |
| 模板用错，已选出应改用的模板 | `no_match` | 说明原因并给出该模板 | `issue-check-failed` |
| 未使用模板，已选出应使用的模板 | `no_match` | 说明原因并给出该模板 | `issue-check-failed` |
| 未使用模板，信息不足以选择 | `no_match` | 列出全部模板，要求补充诉求 | `issue-check-failed` |
| 现有模板都不适用 | `no_match`，`recommendation_status=not_applicable` | 不发评论；建议写入维护者建议表 | `issue-check-passed` |
| 仓库没有可用模板，正文无冲突 | `template_error` | 不发评论；模板问题写入维护者建议表 | `issue-check-passed` |
| 仓库没有可用模板，正文有冲突 | `template_error` | 只写冲突，不提模板 | `issue-check-failed` |
| 未配置模型时完整匹配 | `pass` | 此前没有评论则不发；已有评论则更新为通过 | `issue-check-passed` |
| 未配置模型时部分匹配 | `partial_match` | 指出缺项，并给出可复制模板 | `issue-check-failed` |
| 未配置模型且未匹配模板 | `no_match` | 请使用以下模板提交内容，并列出可复制模板 | `issue-check-failed` |
| 内容与上次检查相同 | `skipped_unchanged` | 不重复处理 | 不改标签 |
| Issue 已关闭 | `skipped_closed` | 不处理 | 不改标签 |

模型说模板用错并指明应改用哪一份时，评论建议改用该模板，同时列出该模板下还缺什么、正文哪里不一致。程序没能识别或识别错模板、模型确认用对时，以模型指向的为准。章节标题用词不同但内容已经写清时，算作已提供。标题前缀不一致只记录，不单独造成失败。

模型超时、鉴权失败或输出无法识别时，任务按原有退避重试；密钥全部失败时进入死信队列。

上一条本服务评论仍是 Issue 的最后一条评论时，原地修改该评论；其后已有其他评论时另发一条，旧评论保留。仓库没有模板时，回退读取所属组织 `.gitcode` 仓库默认分支中的公共模板。

标签名固定为 `issue-check-passed` 和 `issue-check-failed`。服务不会创建它们，仓库里需要事先存在。写入失败只记在该任务上，任务仍完成，其他任务不受影响。新标签写成功后才移除相反标签。`label_action=rejected` 之后，如果标题和正文不再变化，后续通知会变成 `skipped_unchanged`，不会自动重试打标签。

一次检查完成后，`events.result` 是 JSON：

| 字段 | 取值 |
| --- | --- |
| `status` | 上表中的检查状态 |
| `comment_action` | `created` 已发新评论，`updated` 已更新评论，`recovered` 已找到先前评论，`suppressed` 未发评论，`unchanged` 内容未变，`skipped_closed` 已关闭 |
| `recommendation_status` | 模型选择模板的状态；未调用模型或模板已用对时为空 |
| `label` | `issue-check-passed` 或 `issue-check-failed`；未分配时没有该字段 |
| `label_action` | `assigned` 已写入，`rejected` 平台拒绝，`skipped` 本次未分配 |
| `maintainer_suggestion_id` | 写入维护者建议时的记录编号 |
| `assessment` | 调用模型时的评估明细：`template_fit`、字段结论、冲突和结构问题 |

任务状态还有 `queued`、`dispatching`、`running`、`failed`。可重试错误最多 5 次。模型密钥全部失败时任务变为 `failed`，并进入死信队列 `issue.analysis.dlq`。失败原因在 `events.last_error`。

## 部署说明

编排文件是仓库根目录的 `docker-compose.yml`，项目名 `issue-gov`。其中有 PostgreSQL、RabbitMQ、接收服务 `receiver`、消费者 `consumer`。

全项目只有一份环境文件：`issue-gov-agent/.env`。不要在仓库根目录另放 `.env`，也不要把 `DATABASE_URL`、`RABBITMQ_URL` 写进这个文件。程序用 `POSTGRES_*` 和 `RABBITMQ_*` 拼连接串。容器内主机名是 `postgres` 和 `rabbitmq`，端口是 `5432` 和 `5672`。

接入仓库写在 `issue-gov-agent/repository.yml`：

```yaml
owner/repo:
  - maintainer-username
owner/another-repo: []
```

未列出的仓库会被忽略。名单下的账号只用于维护者建议。修改 `.env` 或 `repository.yml` 后执行 `docker compose up -d`，接收服务和消费者才会重新读取。

`.env` 至少要有：

| 变量 | 作用 |
| --- | --- |
| `WEBHOOK_SECRET` | Webhook 密码，对应请求头 `X-GitCode-Token` |
| `ATOMGIT_TOKEN` | 读取 Issue、模板和发表评论的服务账号令牌 |
| `LLM_API_KEY` 或 `LLM_API_KEYS` | 模型密钥。多个密钥用英文逗号分隔。配置后模型参与每次检查；不配置时只做程序的模板匹配 |
| `LLM_BASE_URL` | 模型服务地址 |
| `LLM_MODEL` | 模型名 |
| `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` | 数据库账号。口令只用字母和数字 |
| `POSTGRES_PORT` | 宿主机回环端口，默认 `54329` |
| `RABBITMQ_USER`、`RABBITMQ_PASSWORD` | 队列账号 |
| `RABBITMQ_PORT`、`RABBITMQ_MANAGEMENT_PORT` | 队列和管理页的宿主机回环端口，默认 `56729` 和 `15672` |
| `WEB_CONCURRENCY` | 一个接收容器里的 Gunicorn 工作进程数，默认 `2`，范围 1 到 32 |

首次生成配置：

```bash
cd issue-gov-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py init
```

然后填写令牌、模型配置和 `repository.yml`。在仓库根目录启动：

```bash
export CONTAINER_UID="$(id -u)" CONTAINER_GID="$(id -g)"
docker compose --env-file issue-gov-agent/.env up -d --build
```

不导出 `CONTAINER_UID` 和 `CONTAINER_GID` 时，容器用户是 `1000:1000`，读不了属主为其他人的 `0600` 配置。消费者数量写在 `docker-compose.yml` 的 `consumer.deploy.replicas`。改数字后再次执行 `docker compose up -d`。

消费者镜像要下载 Node.js 22 和 Claude Code。接收镜像不包含它们。数据库和队列端口只绑在 `127.0.0.1`。RabbitMQ 管理页是 `http://127.0.0.1:15672`。

就绪检查：

```bash
curl -sS http://127.0.0.1:7100/ready
```

新机器若按编排文件的默认端口发布接收服务，则把 `7100` 换成实际映射端口。返回 `{"status":"ok"}` 表示数据库和队列都可连接。

失败任务修好密钥或网络后，在消费者容器中执行：

```bash
docker compose exec -T consumer python -m issue_gov.worker --retry-failed
```

它把失败任务放回分析队列，并清掉对应的死信消息。

数据保存：

| 组件 | 作用 |
| --- | --- |
| PostgreSQL | 任务 `events`、报告 `reports`、已检查内容 `issue_checks`、待确认评论 `pending_comments`、维护者建议 `maintainer_suggestions`、接口冷却 `api_cooldown` |
| RabbitMQ | 待分析队列 `issue.analysis`，死信队列 `issue.analysis.dlq` |
| Elasticsearch | 本服务不使用 |

`manage.py` 不设置 `SERVICE_MODE`，只适合本机 SQLite，不会连接 Compose 里的 PostgreSQL 和 RabbitMQ。

### 当前灰度机器

配置目录是 `/opt/CommunityGovAgent/`，里面有 `.env`、`repository.yml` 和 `docker-compose.yml`。镜像从 `/root/CommunityGovAgent` 构建。在该目录执行 `docker compose build` 和 `docker compose up -d`。

接收服务只监听 `127.0.0.1:7100`，容器内仍是 `7000`。公网地址是 `https://governance-agent.oss-compass.io/api/v1/issue/webhook`，由 Nginx 转到 `http://127.0.0.1:7100/webhook`。公网 `/health` 不是这个接收服务。

`0.0.0.0:7000` 是旧服务。不要停止、重启或重新部署它，也不要让 Compose 占用 7000。已经指向 `http://113.44.237.121:7000/webhook` 的生产 Webhook 保持不动。灰度使用上面的 HTTPS 地址，并另建一条 Webhook。

灰度数据库是 `127.0.0.1:54329` 上的库 `issue_gov`。不要对正在服务的 RabbitMQ 跑并发测试脚本，它会使用同一个分析队列并删除 `events`。

## 接口说明

接收服务只有三个 HTTP 接口。容器内监听 `7000`。灰度宿主机是 `127.0.0.1:7100`。

### 在 GitCode 上登记

进入仓库的 **项目设置 → WebHook → 新建 WebHook**：

| 项目 | 填写内容 |
| --- | --- |
| URL | 灰度用 `https://governance-agent.oss-compass.io/api/v1/issue/webhook`。其他部署用 `http://服务器:<接收端口>/webhook` |
| 认证方式 | **WebHook 密码**。不要选「签名密钥」 |
| 密码 | `.env` 中的 `WEBHOOK_SECRET` |
| POST 内容类型 | `application/json` |
| 事件类型 | 只勾选「Issue 事件」 |
| 启用状态 | 勾选「激活」 |

服务账号需要能读取该仓库的 Issue 和模板，并能发表评论。仓库路径还要写进 `repository.yml`。

### `GET /health`

进程存活。返回 `200` 和 `{"status":"ok"}`。不检查数据库和队列。

### `GET /ready`

数据库可读写，且队列模式下还能连接 RabbitMQ 时，返回 `200` 和 `{"status":"ok"}`。初始化未完成返回 `503` 和 `{"status":"initializing"}`。依赖不可用返回 `503` 和 `{"status":"unavailable"}`。

### `POST /webhook`

请求头 `X-GitCode-Token` 的值等于 `WEBHOOK_SECRET`。`Content-Type` 为 `application/json`。请求体上限 1 MiB。

会入队的事件同时满足：

- `object_kind` 或 `event_type` 为 `issue`
- `project.path_with_namespace` 在 `repository.yml` 中
- `object_attributes.action` 为 `open` 或 `update`
- Issue 未关闭
- `update` 时标题、描述或正文确有变化
- `object_attributes.iid` 是正整数

GitCode 的测试推送通常不是这种事件。密码正确时返回忽略，不创建任务。

响应：

| HTTP | 正文 | 含义 |
| --- | --- | --- |
| 202 | `{"status":"queued","state":"queued"}` | 新任务已入队。队列模式下数据库先写成 `queued`，代理确认后再改为 `dispatching`；响应里的 `state` 仍是 `queued` |
| 200 | `{"status":"duplicate","state":"<已有状态>"}` | 同一事件重复投递，不新建任务 |
| 200 | `{"status":"ignored"}` | 密码正确，但不是要处理的 Issue 事件 |
| 401 | `{"error":"unauthorized"}` | 密码错误 |
| 400 | `{"error":"expected JSON object"}` | 正文不是 JSON 对象 |
| 400 | `{"error":"invalid Issue event"}` | 目标仓库的 Issue 事件缺少字段或编号不合法 |
| 400 | `{"error":"invalid event identifier"}` | 事件标识不是字符串 |
| 503 | `{"error":"initializing"}` | 数据库尚未就绪，通知方应稍后重试 |
| 503 | `{"error":"queue unavailable"}` | 入库或投递队列失败，通知方应稍后重试 |
| 413 | Flask 默认响应 | 请求体超过 1 MiB |

Webhook 响应不包含检查结论。结论在 `events.result`，失败原因在 `events.last_error`。
