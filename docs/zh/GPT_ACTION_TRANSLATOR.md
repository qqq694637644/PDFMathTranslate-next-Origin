# 使用 Custom GPT Actions 翻译 PDF

本功能面向个人使用：PDFMathTranslate-next 继续调用官方 BabelDOC `0.5.24` 完成 PDF 解析、段落批处理、占位符校验、排版、分片和输出；Custom GPT 只负责消费翻译请求。

## 架构

```text
PDFMathTranslate-next / BabelDOC
        │
        │ GPTActionTranslator 写入并等待
        ▼
~/.config/pdf2zh/gptaction-queue.sqlite3
        ▲
        │ getNextBatch / submitBatch
        │
Custom GPT Actions ← HTTPS 反向代理 ← pdf2zh-action-api
```

系统只允许一个 active translation run。可以同时打开 2～4 个 Custom GPT 会话并行消费同一个队列。

## 1. 安装

使用 Python 3.10～3.12 安装本项目。项目依赖已经精确固定官方 BabelDOC `0.5.24`；不要单独执行 `pip install -U babeldoc`。

```bash
pip install -e .
```

## 2. 创建根目录 `.env`

在仓库根目录复制示例文件：

```bash
cp .env.example .env
```

PowerShell：

```powershell
Copy-Item .env.example .env
```

至少需要修改：

```text
PUBLIC_BASE_URL
GPT_ACTION_API_KEY
```

示例值 `replace-with-a-long-random-key` 会被程序明确拒绝，避免忘记换密钥后把公开占位值暴露到公网。

默认情况下，API sidecar、PDF 翻译进程、队列恢复命令和 OpenAPI 导出读取当前工作目录的 `.env`。从不同目录启动命令时，应先设置同一个绝对路径：

```text
PDF2ZH_ENV_FILE=C:\path\to\PDFMathTranslate-next-Origin\.env
```

sidecar、队列 CLI 和 OpenAPI 导出也支持显式 `--env-file`。`.env` 中的相对 `GPT_ACTION_QUEUE_DB` 统一相对于该 `.env` 文件所在目录解析，不再相对于各命令的工作目录解析。启动日志会打印实际 `env_file` 和 queue DB 的绝对路径。

统一配置优先级：

```text
显式命令行参数
> 系统环境变量
> 根目录 .env
> 代码默认值
```

主程序原有 TOML 配置仍可使用，但上述环境配置会覆盖其中的同名值。

默认参数适合个人使用：

```text
claim TTL:                 1800 秒
每次最多领取:              2 个 BabelDOC 请求
Action 响应序列化上限:     30000 字符
Action 提交序列化上限:     60000 字符
单项 output 上限:         30000 字符
```

## 3. 启动 Actions sidecar

```bash
pdf2zh-action-api
```

也可以显式覆盖 `.env` 或系统环境：

```bash
pdf2zh-action-api \
  --api-host 127.0.0.1 \
  --api-port 8000 \
  --queue-db ./data/gptaction-queue.sqlite3
```

启动日志会显示监听地址、队列绝对路径和 schema 版本。sidecar 只提供三个翻译操作：

```text
getQueueStatus
getNextBatch
submitBatch
```

两个 POST 操作在 OpenAPI 中都显式包含：

```yaml
x-openai-isConsequential: false
```

这样 Custom GPT 可以在用户授权后连续领取和提交，而不是每次调用都要求单独确认。

不要把 Gradio、本机文件或其他管理接口暴露给 Custom GPT。对外反向代理只需要转发 `/v1/actions/*`，并提供有效 HTTPS 地址。

## 4. 配置 Custom GPT

先根据 `.env` 中的 `PUBLIC_BASE_URL` 生成 schema：

```bash
python script/export_gptaction_openapi.py
```

默认输出：

```text
openapi/gpt-actions.openapi.json
```

命令行 `--server-url` 的优先级高于系统环境和 `.env`：

```bash
python script/export_gptaction_openapi.py \
  --server-url https://translate.example.com
```

然后：

1. 在 Custom GPT 的 Actions 中导入 `openapi/gpt-actions.openapi.json`。
2. 确认 `servers[0].url` 与实际 HTTPS 地址一致。
3. 认证选择 API key，Bearer 方式，值与 `GPT_ACTION_API_KEY` 相同。
4. 将根目录 `CUSTOM_GPT_ACTIONS_INSTRUCTIONS.zh-CN.md` 的内容加入 GPT 指令。
5. 不要让 GPT 上传、下载、解析或排版 PDF；它只领取并提交文本翻译。

## 5. 开始翻译

命令行示例：

```bash
pdf2zh_next document.pdf \
  --gptaction
```

Windows PowerShell：

```powershell
pdf2zh_next document.pdf `
  --gptaction
```

此时队列路径、协议版本、轮询间隔、线程数和分片页数来自 `.env`。需要临时覆盖时继续使用原有参数：

```bash
pdf2zh_next document.pdf \
  --gptaction \
  --gptaction-queue-db ./data/other.sqlite3 \
  --gptaction-protocol-version 2 \
  --gptaction-poll-interval-seconds 1.0 \
  --pool-max-workers 12 \
  --max-pages-per-part 100
```

推荐从以下设置开始：

```text
pool_max_workers = 6～8
Custom GPT 会话数 = 2～4
max_pages_per_part = 50
```

GPTActionTranslator 不再设置会覆盖用户输入的推荐线程数。显式命令行、系统环境或 `.env` 中的 `pool_max_workers` 会保持生效，不会被静默改回 8。

BabelDOC 线程会逐步产生请求。Custom GPT 可以反复调用 `getNextBatch`，翻译后调用 `submitBatch`；请求完成后，对应 BabelDOC 线程继续执行。

GPTAction 模式不会使用原有的 30 分钟“无进度事件”超时。只要进程仍在运行，用户可以暂停领取请求，数小时后继续；当前 PDF 分片和 BabelDOC 内存也会继续驻留。

## 请求模式

### `LLM_BATCH`

`input` 是 BabelDOC 原始完整 prompt。GPT 必须严格遵循 prompt，并将完整 JSON 结果作为 `output` 字符串提交。不要在 JSON 前后添加 Markdown、解释或代码围栏。

### `SIMPLE_TEXT`

`input` 是单段文本。GPT 只返回译文，必须保留原有 placeholder、公式标记和不可翻译 token。

API 不复制 BabelDOC 的 JSON、ID、placeholder 或长度校验；这些仍由官方 BabelDOC 负责。`LLM_BATCH` 失败后，BabelDOC 0.5.24 的 LLM-only 路径通常改为单段 LLM prompt 并再次调用 `LLM_BATCH`，而不是固定切换到 `SIMPLE_TEXT`。`SIMPLE_TEXT` 仍用于普通非 LLM 翻译调用。

## 并行、claim 和幂等

- SQLite 使用原子领取，多个 GPT 会话不会正常领取同一请求。
- 默认每次最多领取 2 个请求；`pool_max_workers=8` 时，2～4 个 GPT 会话可以分摊当前队列。
- `claim_token` 在同一请求的重新领取中保持稳定。
- claim 过期后请求可被重新领取，但先到达的有效结果仍会被接受。
- 相同结果重复提交返回 `IDEMPOTENT`。
- 已完成请求提交不同结果返回 `CONFLICT`。
- 一个批次中的合法结果会独立完成，其他错误项不会回滚。
- 单项 output 超过上限时仅该项返回 `ERROR`。
- 请求在写入 SQLite 前会按完整单项 Action envelope 预检；如果单项本身超过响应上限，当前 BabelDOC 翻译会立即明确失败，不会留下永久堵塞队首的请求。

## 取消和恢复

用户取消 PDF 翻译时，等待中的请求会进入 `CANCELED`，迟到提交会被拒绝。

worker 异常退出后，不恢复 Document IL。重新运行官方 pipeline 时：

1. BabelDOC 重新解析当前或前面的分片；
2. 完整请求 fingerprint 相同的已完成结果直接复用；
3. 未完成请求重新进入队列；
4. 官方 BabelDOC 继续排版和输出。

这能复用译文，但不保证从失败分片的中间位置继续；已完成分片可能重新解析和排版。

### 强制关闭后的孤儿 ACTIVE run

如果主进程被任务管理器结束、终端被直接关闭、断电或系统重启，SQLite 无法自动执行正常清理。确认原翻译进程已经不存在后，运行：

```bash
pdf2zh-action-queue status
pdf2zh-action-queue recover-active-run
```

恢复命令会显示当前唯一 `ACTIVE` run，并要求输入完整 `run_id` 确认。无人值守脚本可以使用：

```bash
pdf2zh-action-queue recover-active-run --yes
```

它只做以下操作：

```text
ACTIVE run → FAILED
已完成结果保持 COMPLETED
已领取但未完成的请求释放为 PENDING
下一次运行按 fingerprint 重新绑定并复用
```

不要在原翻译进程仍然运行时执行该命令。

## 大 PDF 与内存

`max_pages_per_part` 是官方 BabelDOC 的串行分片能力。它限制当前内存中 Document 的页数，但当前分片在等待 GPT 翻译期间仍会驻留内存。

建议先使用 50 页分片，观察峰值 RSS 和总耗时后再调整到 75 或 100。

## 常见问题

### Actions 一直显示没有请求

确认 PDF 翻译进程已经使用 `--gptaction` 启动，并检查：

```text
GPT_ACTION_QUEUE_DB 与 --gptaction-queue-db 指向同一绝对路径
当前没有第二个 active run
Custom GPT 使用正确 Bearer key
反向代理转发 /v1/actions/*
```

### 启动翻译时报已有 active run

正常情况下父进程会在完成、取消或子进程异常时关闭 run。先确认没有另一个 PDF 翻译仍在运行；若上次是强制退出，使用 `pdf2zh-action-queue recover-active-run` 显式恢复。不要同时启动两个 GPTAction PDF 任务。

### 请求提示超过 Action 响应上限

错误会包含 `required_chars` 与 `max_chars`。确认 Custom GPT Actions 能接受更大的响应后，在 sidecar 与 PDF 翻译进程中设置同一个值：

```text
GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS
```

然后重新启动翻译。默认值为 `30000`。

GPT Actions 的请求和响应必须少于 100,000 字符，因此以下三个配置均被硬限制为 `1000～99999`：

```text
GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS
GPT_ACTION_MAX_SUBMIT_CHARS
GPT_ACTION_MAX_OUTPUT_CHARS
```

### 清除一个已知错误的缓存结果

translator 日志会打印：

```text
request_id
mode
fingerprint
```

如果 BabelDOC 后续判断某个已提交结果无效，先结束或取消当前 run，再精确删除该缓存：

```bash
pdf2zh-action-queue invalidate-request req_xxx
```

命令默认要求输入完整 `request_id` 确认，也支持 `--yes`。删除后，下次相同 fingerprint 会重新进入 GPT Actions 队列。active run 存在时命令会拒绝执行，避免删除仍有线程等待的结果。

### 为什么自动术语提取被关闭

第一版只让 GPTAction 翻译正文。选择 GPTAction 后，应用会强制 `no_auto_extract_glossary=true`，避免正文前出现术语提取任务。已有手工 glossary 仍会作为翻译语义的一部分参与 fingerprint。
