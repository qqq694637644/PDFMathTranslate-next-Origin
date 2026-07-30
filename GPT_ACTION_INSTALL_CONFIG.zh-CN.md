# GPTAction 个人版安装与配置说明

本文档用于当前 PR 的个人单用户场景：使用原始 PDFMathTranslate Next WebUI 上传 PDF，选择 `GPTAction` 翻译器，让一个或多个 Custom GPT 会话通过 Actions 并行领取、翻译并提交文本。

目标使用方式：

```text
WebUI 上传 PDF、选择页范围和输出选项
+ 本地 pdf2zh-action-api sidecar 提供 Actions API
+ Custom GPT 通过 HTTPS 访问 /v1/actions/*
```

---

## 1. 当前限制

第一版按个人使用设计，不是企业多用户服务。

推荐先使用：

```text
PDF 页范围：前 5 页
GPT 会话数：2～4 个
Pool Max Workers：8
每个 GPT 每次领取：2 个请求
Maximum pages per part：50
```

暂不建议一开始使用 20 个 GPT 会话。代码队列可以支持多个消费者并发领取，但 BabelDOC 同时能产生的等待请求数主要由 `Pool Max Workers` 决定。默认 `8` 更适合先跑通流程。

---

## 2. 安装

### 2.1 克隆当前 PR 分支

当前功能还在 PR 分支上，先克隆该分支：

```powershell
git clone -b gpt/gpt-action-translator-development-plan https://github.com/qqq694637644/PDFMathTranslate-next-Origin.git
cd PDFMathTranslate-next-Origin
```

如果已经克隆过仓库：

```powershell
git fetch origin
git checkout gpt/gpt-action-translator-development-plan
git pull
```

### 2.2 创建 Python 3.12 虚拟环境

Windows 推荐 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
```

确认版本：

```powershell
python --version
```

应显示 Python 3.12.x。

### 2.3 安装项目

```powershell
pip install -e .
```

安装后确认命令存在：

```powershell
pdf2zh --help
pdf2zh-action-api --help
pdf2zh-action-queue --help
```

---

## 3. 创建 `.env`

在仓库根目录复制示例配置：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`。最小推荐配置如下：

```text
PUBLIC_BASE_URL=https://你的公网域名
GPT_ACTION_API_KEY=换成一个很长的随机密钥
GPT_ACTION_QUEUE_DB=./data/gptaction-queue.sqlite3
GPT_ACTION_API_HOST=127.0.0.1
GPT_ACTION_API_PORT=8000
GPT_ACTION_CLAIM_TTL_SECONDS=1800
GPT_ACTION_MAX_REQUESTS=2
GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS=30000
GPT_ACTION_MAX_SUBMIT_CHARS=60000
GPT_ACTION_MAX_OUTPUT_CHARS=30000

PDF2ZH_POOL_MAX_WORKERS=8
PDF2ZH_MAX_PAGES_PER_PART=50
```

必须修改：

```text
GPT_ACTION_API_KEY=replace-with-a-long-random-key
```

这个公开占位值会被程序拒绝。请换成只有你自己知道的长随机字符串。

### 3.1 路径要求

`GPT_ACTION_QUEUE_DB` 是 WebUI 翻译进程和 Actions API sidecar 共享的 SQLite 队列。

推荐使用相对路径：

```text
GPT_ACTION_QUEUE_DB=./data/gptaction-queue.sqlite3
```

该相对路径会按 `.env` 所在目录解析。

如果你从不同目录启动命令，设置统一的 `.env` 绝对路径：

```powershell
$env:PDF2ZH_ENV_FILE="C:\Users\Administrator\Desktop\PDFMathTranslate-next-Origin\.env"
```

sidecar、WebUI、队列 CLI 和 OpenAPI 导出必须使用同一个 `.env` 或同一个 queue DB 绝对路径。

---

## 4. 启动服务

需要两个 PowerShell 窗口，都建议在仓库根目录启动，并激活同一个虚拟环境。

### 4.1 窗口 1：启动 Actions API sidecar

```powershell
cd C:\Users\Administrator\Desktop\PDFMathTranslate-next-Origin
.\.venv\Scripts\Activate.ps1
pdf2zh-action-api
```

默认监听：

```text
http://127.0.0.1:8000
```

这个地址是本机地址。Custom GPT 不能直接访问本机地址，你需要使用 HTTPS 反向代理或隧道，把公网域名转发到：

```text
http://127.0.0.1:8000/v1/actions/*
```

只需要公开 `/v1/actions/*`。不要把 WebUI、队列数据库或其他本机端口直接公开。

### 4.2 窗口 2：启动 WebUI

```powershell
cd C:\Users\Administrator\Desktop\PDFMathTranslate-next-Origin
.\.venv\Scripts\Activate.ps1
pdf2zh --gui
```

浏览器打开：

```text
http://127.0.0.1:7860
```

---

## 5. WebUI 中怎么配置 GPTAction

WebUI 左侧通常有两个图标：

```text
火箭图标：翻译页面，用于上传 PDF、选择页范围并开始翻译
齿轮图标：设置页面，用于选择翻译器和填写 GPTAction 参数
```

### 5.1 设置页面：选择 GPTAction

进入齿轮图标设置页。

#### UI Language

按需选择语言，例如：

```text
English
```

#### Service

选择：

```text
GPTAction
```

选择后会出现 GPTAction 相关配置。

#### SQLite queue path

填写与 `.env` 中 `GPT_ACTION_QUEUE_DB` 一致的路径。

推荐直接填绝对路径，避免混淆。例如：

```text
C:\Users\Administrator\Desktop\PDFMathTranslate-next-Origin\data\gptaction-queue.sqlite3
```

如果这里和 `pdf2zh-action-api` 使用的队列路径不同，Custom GPT 会一直拿不到请求。

#### GPT Actions translation protocol version

保持默认即可：

```text
2
```

当你修改 Custom GPT 指令、输出协议或翻译语义策略时，才需要提高该版本，让旧缓存不再复用。

#### Queue polling interval in seconds

推荐：

```text
0.5
```

表示 BabelDOC 翻译线程等待 GPT 结果时，每 0.5 秒检查一次 SQLite。

#### Maximum serialized getNextBatch response size in characters

推荐：

```text
30000
```

GPT Actions 平台对请求和响应有字符限制，不要设置到 `100000` 或更高。当前程序允许的上限应小于 `100000`。

#### Rate Limit Mode

选择：

```text
Custom
```

#### QPS

推荐先用：

```text
4
```

GPTActionTranslator 实际不是调用传统 API，而是在本地队列中等待 GPT 提交；这里主要配合原始 BabelDOC 的速率和线程配置。

#### Pool Max Workers

推荐先用：

```text
8
```

含义：BabelDOC 最多同时启动约 8 个翻译工作线程等待 GPT 结果。

与 GPT 会话数的关系：

```text
2～4 个 GPT 会话：Pool Max Workers = 8，GPT_ACTION_MAX_REQUESTS = 2
5～8 个 GPT 会话：需要进一步测试，可考虑提高 Pool Max Workers
20 个 GPT 会话：不建议第一轮使用，尚未作为稳定目标验收
```

#### Auto Term Extraction

建议关闭。

第一版 GPTAction 只处理正文翻译。自动术语提取可能让 GPT 在正文前先收到术语任务，不利于个人调试。

---

## 6. WebUI 中怎么选择前 5 页

进入左侧火箭图标翻译页面。

操作顺序：

```text
上传 PDF
→ Pages 选择 First 5 pages
→ 或 Pages 选择 Range，并在 Page range 输入 1-5
→ 按需选择输出模式
→ 点击 Translate
```

### 6.1 Pages

选择：

```text
First 5 pages
```

等价于翻译第 1～5 页。

也可以选择：

```text
Range
```

并输入：

```text
1-5
```

### 6.2 Only include translated pages in the output PDF

如果你只想输出前 5 页，勾选：

```text
Only include translated pages in the output PDF
```

如果不勾选，则由原始 BabelDOC 按页面范围语义生成输出：只翻译选中页面，但输出文件是否包含未选页面取决于该选项和 BabelDOC 行为。

### 6.3 Maximum pages per part

保持：

```text
50
```

这不是“翻译 50 页”，而是大 PDF 的官方分片大小，用于控制内存。测试前 5 页时也可以保持 50。

---

## 7. 生成 OpenAPI 并导入 Custom GPT

`openapi/gpt-actions.openapi.json` 是动态生成文件，不提交到 Git。

修改 `.env` 后运行：

```powershell
python script/export_gptaction_openapi.py
```

生成文件：

```text
openapi/gpt-actions.openapi.json
```

在 Custom GPT 编辑器中：

```text
Configure
→ Actions
→ Import from schema
→ 粘贴或上传 openapi/gpt-actions.openapi.json
```

认证方式选择 API Key / Bearer，并填入 `.env` 中同一个：

```text
GPT_ACTION_API_KEY
```

Custom GPT 指令使用根目录：

```text
CUSTOM_GPT_ACTIONS_INSTRUCTIONS.zh-CN.md
```

导入后确认有三个操作：

```text
getQueueStatus
getNextBatch
submitBatch
```

如果出现：

```text
request body schema is not an object schema; skipping
```

说明你导入的是旧 schema。请更新代码后重新运行：

```powershell
python script/export_gptaction_openapi.py
```

再重新导入。

---

## 8. 推荐的第一次端到端测试

第一次不要跑全书，先测试前 5 页。

### 8.1 `.env`

```text
GPT_ACTION_MAX_REQUESTS=2
GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS=30000
GPT_ACTION_MAX_SUBMIT_CHARS=60000
GPT_ACTION_MAX_OUTPUT_CHARS=30000
PDF2ZH_POOL_MAX_WORKERS=8
PDF2ZH_MAX_PAGES_PER_PART=50
```

### 8.2 WebUI 设置页

```text
Service: GPTAction
SQLite queue path: 与 .env 同一个 queue DB
Protocol version: 2
Polling interval: 0.5
Max serialized response size: 30000
Rate Limit Mode: Custom
QPS: 4
Pool Max Workers: 8
Auto Term Extraction: 关闭
```

### 8.3 WebUI 翻译页

```text
上传 PDF
Pages: First 5 pages
Maximum pages per part: 50
点击 Translate
```

### 8.4 Custom GPT

打开 2～4 个 Custom GPT 会话，让它们按指令循环：

```text
getQueueStatus
getNextBatch
翻译
submitBatch
继续 getNextBatch
```

队列没有可翻译请求、进入准备/收尾阶段或已完成时，GPT 应停止本轮调用，不要无间隔轮询。

---

## 9. 常用队列命令

查看队列状态：

```powershell
pdf2zh-action-queue status
```

强制关闭、断电或终端被直接关掉后，如果下次启动提示已有 active run，先确认没有旧翻译进程仍在运行，然后执行：

```powershell
pdf2zh-action-queue recover-active-run
```

如果 BabelDOC 日志显示某个已提交译文无效，可以在没有 active run 时删除该坏缓存：

```powershell
pdf2zh-action-queue invalidate-request req_xxx
```

---

## 10. 常见问题

### 10.1 Custom GPT 一直拿不到请求

检查：

```text
WebUI 是否已经点击 Translate
WebUI 是否选择 GPTAction
pdf2zh-action-api 是否正在运行
Custom GPT 使用的 Bearer key 是否正确
反向代理是否只转发 /v1/actions/* 到 127.0.0.1:8000
WebUI 的 SQLite queue path 是否等于 sidecar 使用的 GPT_ACTION_QUEUE_DB
```

先运行：

```powershell
pdf2zh-action-queue status
```

确认是否有 active run 和 pending/claimed/completed 数量。

### 10.2 启动翻译时报已有 active run

通常是上次被强制关闭，没有机会正常清理 SQLite。

确认没有旧翻译进程后：

```powershell
pdf2zh-action-queue recover-active-run
```

### 10.3 Custom GPT 导入 OpenAPI 报 request body schema 不是 object

你导入了旧的 OpenAPI。更新当前 PR 分支后重新生成：

```powershell
python script/export_gptaction_openapi.py
```

然后重新导入 `openapi/gpt-actions.openapi.json`。

### 10.4 提示请求或响应太大

保持默认：

```text
GPT_ACTION_MAX_REQUESTS=2
GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS=30000
GPT_ACTION_MAX_SUBMIT_CHARS=60000
GPT_ACTION_MAX_OUTPUT_CHARS=30000
```

如果 `submitBatch` 返回 413，把本次结果拆成更小批次提交。

### 10.5 想翻译前 20 页

WebUI 翻译页选择：

```text
Pages: Range
Page range: 1-20
```

或者 CLI：

```powershell
pdf2zh input.pdf --gptaction --pages 1-20
```

### 10.6 想增加 GPT 会话数

先用 2～4 个会话跑通。若要提高并发，可以逐步测试：

```text
Pool Max Workers: 12 或 16
GPT_ACTION_MAX_REQUESTS: 1 或 2
```

不要直接上 20 个会话。20 会话需要真实压力测试，并且不保证 Custom GPT 平台持续调用能力。

---

## 11. Docker 说明

当前 Docker 默认命令只启动 GUI，不会同时启动 `pdf2zh-action-api`。

使用 Docker 做 GPT Actions 时，需要两个进程或两个容器：

```text
容器/进程 1：pdf2zh --gui
容器/进程 2：pdf2zh-action-api
```

并且二者共享同一个 queue DB volume。

个人 Windows 本地调试阶段，建议先不用 Docker，直接用两个 PowerShell 窗口启动。
