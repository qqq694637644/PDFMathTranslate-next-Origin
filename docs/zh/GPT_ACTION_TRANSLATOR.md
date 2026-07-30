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

使用 Python 3.10～3.12 安装本项目。生产环境使用官方 BabelDOC `0.5.24`，不要单独执行不带版本范围的 `pip install -U babeldoc`。

```bash
pip install -e .
pip install "babeldoc==0.5.24" "pymupdf<1.25.3"
```

## 2. 设置共享队列和 API key

API 进程与 PDF 翻译进程必须使用同一个绝对 SQLite 路径。

PowerShell：

```powershell
$env:GPT_ACTION_API_KEY = "请替换为至少16字符的随机密钥"
$env:GPT_ACTION_QUEUE_DB = "$HOME\.config\pdf2zh\gptaction-queue.sqlite3"
$env:GPT_ACTION_API_HOST = "127.0.0.1"
$env:GPT_ACTION_API_PORT = "8000"
```

Linux/macOS：

```bash
export GPT_ACTION_API_KEY='请替换为至少16字符的随机密钥'
export GPT_ACTION_QUEUE_DB="$HOME/.config/pdf2zh/gptaction-queue.sqlite3"
export GPT_ACTION_API_HOST='127.0.0.1'
export GPT_ACTION_API_PORT='8000'
```

默认参数适合个人使用：

```text
claim TTL:                 1800 秒
每次最多领取:              8 个 BabelDOC 请求
Action 响应序列化上限:     30000 字符
Action 提交序列化上限:     60000 字符
单项 output 上限:         30000 字符
```

## 3. 启动 Actions sidecar

```bash
pdf2zh-action-api
```

启动日志会显示监听地址、队列绝对路径和 schema 版本。sidecar 只提供三个翻译操作：

```text
getQueueStatus
getNextBatch
submitBatch
```

不要把 Gradio、本机文件或其他管理接口暴露给 Custom GPT。对外反向代理只需要转发 `/v1/actions/*`，并提供有效 HTTPS 地址。

## 4. 配置 Custom GPT

1. 在 Custom GPT 的 Actions 中导入 `openapi/gpt-actions.openapi.json`。
2. 把 schema 中的 `servers[0].url` 替换为你的 HTTPS 地址。
3. 认证选择 API key，Bearer 方式，值与 `GPT_ACTION_API_KEY` 相同。
4. 将根目录 `CUSTOM_GPT_ACTIONS_INSTRUCTIONS.zh-CN.md` 的内容加入 GPT 指令。
5. 不要让 GPT 上传、下载、解析或排版 PDF；它只领取并提交文本翻译。

## 5. 开始翻译

命令行示例：

```bash
pdf2zh_next document.pdf \
  --gptaction \
  --gptaction-queue-db "$GPT_ACTION_QUEUE_DB" \
  --pool-max-workers 8 \
  --max-pages-per-part 50 \
  --no-auto-extract-glossary
```

Windows PowerShell：

```powershell
pdf2zh_next document.pdf `
  --gptaction `
  --gptaction-queue-db $env:GPT_ACTION_QUEUE_DB `
  --pool-max-workers 8 `
  --max-pages-per-part 50 `
  --no-auto-extract-glossary
```

推荐从以下设置开始：

```text
pool_max_workers = 6～8
Custom GPT 会话数 = 2～4
max_pages_per_part = 50
```

BabelDOC 线程会逐步产生请求。Custom GPT 可以反复调用 `getNextBatch`，翻译后调用 `submitBatch`；请求完成后，对应 BabelDOC 线程继续执行。

## 请求模式

### `LLM_BATCH`

`input` 是 BabelDOC 原始完整 prompt。GPT 必须严格遵循 prompt，并将完整 JSON 结果作为 `output` 字符串提交。不要在 JSON 前后添加 Markdown、解释或代码围栏。

### `SIMPLE_TEXT`

`input` 是单段文本。GPT 只返回译文，必须保留原有 placeholder、公式标记和不可翻译 token。

API 不复制 BabelDOC 的 JSON、ID、placeholder 或长度校验；这些仍由官方 BabelDOC 负责。LLM batch 失败时，BabelDOC 会使用 `SIMPLE_TEXT` fallback。

## 并行、claim 和幂等

- SQLite 使用原子领取，多个 GPT 会话不会正常领取同一请求。
- `claim_token` 在同一请求的重新领取中保持稳定。
- claim 过期后请求可被重新领取，但先到达的有效结果仍会被接受。
- 相同结果重复提交返回 `IDEMPOTENT`。
- 已完成请求提交不同结果返回 `CONFLICT`。
- 一个批次中的合法结果会独立完成，其他错误项不会回滚。
- 单项 output 超过上限时仅该项返回 `ERROR`。

## 取消和恢复

用户取消 PDF 翻译时，等待中的请求会进入 `CANCELED`，迟到提交会被拒绝。

worker 异常退出后，不恢复 Document IL。重新运行官方 pipeline 时：

1. BabelDOC 重新解析当前或前面的分片；
2. 完整请求 fingerprint 相同的已完成结果直接复用；
3. 未完成请求重新进入队列；
4. 官方 BabelDOC 继续排版和输出。

这能复用译文，但不保证从失败分片的中间位置继续；已完成分片可能重新解析和排版。

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

正常情况下父进程会在完成、取消或子进程异常时关闭 run。确认没有另一个 PDF 翻译仍在运行；不要同时启动两个 GPTAction PDF 任务。

### 为什么自动术语提取被关闭

第一版只让 GPTAction 翻译正文。选择 GPTAction 后，应用会强制 `no_auto_extract_glossary=true`，避免正文前出现术语提取任务。已有手工 glossary 仍会作为翻译语义的一部分参与 fingerprint。
