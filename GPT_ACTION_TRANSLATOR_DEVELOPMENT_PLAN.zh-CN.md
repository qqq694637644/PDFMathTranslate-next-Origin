# GPT Actions Translator 开发方案

## 1. 基线与约束

本文档以以下仓库和版本为唯一开发基线：

```text
Repository: qqq694637644/PDFMathTranslate-next-Origin
Base branch: main
Baseline commit: 760e4d709be9f87464f54f2ff0b167493b3ac8b4
Official BabelDOC: 0.5.24
```

后续 GPT Actions 适配必须遵循：

```text
只扩展 PDFMathTranslate-next 应用层
不 fork BabelDOC
不复制 BabelDOC 源码
不发布自建 BabelDOC wheel
不修改 ILTranslator / ILTranslatorLLMOnly
不自建 Document/IL checkpoint
不自建 placeholder 注入和 PDF 构建流程
```

源码依赖和生产环境都固定使用官方 BabelDOC 0.5.24：

```toml
babeldoc==0.5.24
```

普通 `pip install -e .`、Docker 和发布环境必须解析到同一官方版本，不能再执行裸 `-U babeldoc`。

---

## 2. 目标

在保留原始 PDFMathTranslate-next 与 BabelDOC 完整翻译流水线的前提下，新增一个 GPT Actions 翻译器，使多个 Custom GPT 会话能够并行领取和提交 BabelDOC 翻译请求。

目标流程：

```text
原始 Web/CLI 创建 TranslationConfig
→ 原始 high_level.py 启动翻译子进程
→ 原始 babeldoc.format.pdf.high_level.async_translate()
→ 原始 ILTranslatorLLMOnly / ILTranslator 调用 GPTActionTranslator
→ GPTActionTranslator 将请求写入独立 SQLite 队列并等待
→ 多个 Custom GPT 会话通过 Actions 并行领取请求
→ GPT 提交结果
→ 阻塞的 translator 调用返回
→ 原始 BabelDOC 继续 placeholder 校验、fallback、排版和 PDF 输出
```

必须继续使用原始能力：

```text
页面范围
only_include_translated_page
max_pages_per_part 官方分片
translation pool
LLM batch
fallback executor
取消机制
进度事件
split/merge
mono/dual PDF
```

---

## 3. 为什么采用 Translator 插件模式

原始仓库已经提供稳定扩展点：

```text
pdf2zh_next/translator/base_translator.py
pdf2zh_next/translator/translator_impl/*
pdf2zh_next/translator/utils.py
pdf2zh_next/config/translate_engine_model.py
```

原始调用链直接使用：

```python
from babeldoc.format.pdf.high_level import async_translate as babeldoc_translate
```

因此 GPT Actions 只需要实现一个新的翻译器和请求队列，不需要拆分 analysis/build，也不需要将完整 BabelDOC Document 序列化为 checkpoint。当前官方分片对应的 Document 在等待 GPT 翻译期间仍会驻留内存。

该方案删除以下复杂性：

```text
prepared.il.xml
Document checkpoint
checkpoint schema migration
第二次 XML 解析
自定义 segment 注入
analysis/build 两套 worker
自定义 PDF 状态机
Internal source changed 往返问题
```

---

## 4. 原始 BabelDOC 并发模型

BabelDOC 0.5.24 已经提供官方翻译并发：

```text
PriorityThreadPoolExecutor(max_workers=pool_max_workers)
```

LLM-only 路径还自带多段落聚合：

```text
累计 token > 200
或段落数 > 5
→ 提交一个 LLM batch
```

因此一个官方 LLM 请求通常已经包含多个段落，并由 BabelDOC 自己处理：

```text
JSON prompt 构造
ID 对应
placeholder
输出长度
相同文本检测
fallback
post_translate_paragraph
```

GPT Actions 后端不得重新解析这些内部段落，也不得重新实现上述校验。

BabelDOC 可能同时存在：

```text
LLM batch executor: pool_max_workers
fallback executor:  pool_max_workers
```

因此 `pool_max_workers=16` 时，短时间可能接近 32 个阻塞翻译调用。第一版推荐：

```text
pool_max_workers = 6 或 8
GPT 会话数 = 2～4
```

根据队列深度、fallback 比例、RSS 和吞吐量再测试 12、16。

---

## 5. GPTActionTranslator

新增：

```text
pdf2zh_next/translator/translator_impl/gptaction.py
```

翻译器支持两种请求模式：

```text
LLM_BATCH
SIMPLE_TEXT
```

### 5.1 LLM_BATCH

实现 `do_llm_translate()`，输入是 BabelDOC 生成的完整 prompt，输出必须是 BabelDOC 期望的完整字符串。

```python
def do_llm_translate(self, text, rate_limit_params=None):
    if text is None:
        return None
    return self._enqueue_and_wait("LLM_BATCH", text)
```

`text is None` 是 BabelDOC 0.5.24 的能力探测，不能生成队列请求。

### 5.2 SIMPLE_TEXT

实现普通翻译和 LLM batch fallback：

```python
def do_translate(self, text, rate_limit_params=None):
    return self._enqueue_and_wait("SIMPLE_TEXT", text)
```

返回纯译文字符串。

### 5.3 队列作为权威缓存

不能只依赖现有 `TranslationCache`。原因：

```text
translate() 和 llm_translate() 当前共享 cache
cache key 没有 mode
GPT 已提交后、BaseTranslator 写 cache 前 worker 可能崩溃
```

GPTActionTranslator 应覆盖公共 `translate()` 和 `llm_translate()`，先查询 durable queue result。

fingerprint 使用固定 canonical 公式：

```text
SHA-256(
  protocol_version
  + mode
  + lang_in
  + lang_out
  + 完整 input_text / prompt
  + 实际影响翻译内容的 custom prompt / glossary 设置
)
```

必须明确排除所有运行和调度参数：

```text
run_id
request_id
队列数据库路径
API key、host、port
claim TTL 和 claim_token
pool_max_workers
qps
进程、线程和 PID 信息
source part index
时间戳
```

对于 `LLM_BATCH`，完整 BabelDOC prompt 已包含主要翻译语义；仍需把语言、协议版本和真正影响输出的外部 prompt/glossary 设置纳入 fingerprint。

相同 fingerprint 已完成时直接返回结果，不再次等待 GPT。

---

## 6. SQLite durable queue

新增独立模块：

```text
pdf2zh_next/translator/gptaction_queue.py
```

队列数据库与现有翻译缓存分离，避免 mode、claim 和 run 生命周期语义混淆。

### 6.1 请求状态

```text
PENDING
CLAIMED
COMPLETED
CANCELED
```

### 6.2 最小字段

`translation_runs`：

```text
run_id
status              # ACTIVE / COMPLETED / FAILED / CANCELED
created_at
updated_at
completed_at
```

数据库用部分唯一索引保证最多一个 `ACTIVE` run。

`translation_requests`：

```text
request_id
run_id
protocol_version
mode
fingerprint
input_text
status
claim_token
claimed_until
output_text
created_at
updated_at
completed_at
```

第一版不把 `claim_count`、`consumer_label`、`worker_pid`、`source_part_index` 或 heartbeat 放入协议和核心 schema。需要时仅写本地日志，后续有明确诊断价值再扩展。

### 6.3 原子领取

领取必须使用 SQLite 写事务：

```text
BEGIN IMMEDIATE
→ 回收 claim 已过期的请求
→ 选择 PENDING 请求
→ request 首次领取时生成随机稳定 claim_token
→ 再次领取时复用同一 claim_token，只更新 claimed_until
→ COMMIT
```

不能只查询最旧请求，也不能使用进程内软锁。

第一版的 `claim_token` 是 request-scoped submission token，不是每次 lease 都变化的 owner token。它用于防止 GPT 把结果提交到错误 request；同一请求过期后被另一会话重新领取时，两边会看到同一个 token，并按 first-result-wins 竞争提交。这样无需 claim 历史表，也不会因为 token 被覆盖而拒绝先前领取者。

### 6.4 claim lease

claim 是 Action 调度所有权，不是 BabelDOC worker 生命周期。

第一版建议：

```text
claim TTL 可配置
默认 15～30 分钟
```

原因：Custom GPT 翻译一个包含多个内部请求的 Action batch 可能明显超过 120 秒。

claim 过期后请求可重新领取，但 claim TTL 只负责避免多个会话同时领取，不决定译文所有权。提交使用 first-result-wins：

```text
请求为 PENDING 或 CLAIMED，且尚未完成：
  claim_token 必须等于该 request_id 的稳定 token
  即使 claimed_until 已经过期，也接受第一个有效非空结果

请求已经 COMPLETED：
  相同 output → 幂等成功
  不同 output → 返回冲突，不覆盖

请求已经 CANCELED：
  拒绝提交
```

claim 过期后新旧会话都可能完成翻译，最终只有第一个提交成功。这样不会因为 GPT 翻译稍慢于 TTL 而丢弃已经完成的结果。

### 6.5 任务隔离

第一版限制：

```text
同一时间只允许一个 active GPTAction translation run
```

内部使用 `run_id` 隔离一次 PDF 翻译运行，但 Custom GPT 不需要提交或维护 `run_id`。API 自动查找唯一 active run；`getQueueStatus` 和 `getNextBatch` 可以返回 `run_id` 供显示和日志使用，`submitBatch` 只需要 `request_id`、`claim_token` 和结果。

run 生命周期：

```text
Gradio/CLI 启动一次使用 GPTActionTranslator 的翻译
→ 主进程创建 run_id，并设为唯一 active run
→ 翻译子进程产生的所有 request 绑定该 run_id
→ 正常完成后 run = COMPLETED
→ 用户明确取消后 run = CANCELED，未完成 request = CANCELED
→ 子进程异常退出后 run = FAILED，但未明确取消的 request 结果仍可提交并缓存
→ 下一次翻译创建新的 run_id，并按 fingerprint 复用历史 COMPLETED 结果
```

CLI 顺序处理多个 PDF 时，每个 PDF 创建独立 run；前一个 run 结束后才能激活下一个。第一版不允许两个 active run。

主进程被任务管理器强杀、终端直接关闭、断电或系统重启时，无法依赖 `finally` 更新 SQLite。个人版不增加 heartbeat，而是提供本地显式恢复入口：

```text
pdf2zh-action-queue status
pdf2zh-action-queue recover-active-run
```

用户确认原进程已经不存在并输入当前 `run_id` 后：

```text
ACTIVE run → FAILED
COMPLETED 请求保持不变
CLAIMED 请求释放为 PENDING，保留稳定 claim_token
下一次运行按 fingerprint 重新绑定
```

该入口只操作本机 SQLite，不通过 GPT Actions 暴露。

---

## 7. Actions API

新增应用层 FastAPI 服务，建议模块：

```text
pdf2zh_next/gptaction_api.py
```

第一版采用独立 sidecar，不挂载到 Gradio：

```text
Gradio UI:
  127.0.0.1:7860

GPT Actions API:
  127.0.0.1:8000

反向代理/隧道：
  只公开 /v1/actions/*
```

新增命令入口：

```text
pdf2zh-action-api
```

sidecar 与 Gradio/CLI 翻译子进程通过同一个 SQLite 文件通信，不共享 Python 内存。最小配置：

```text
GPT_ACTION_API_KEY
GPT_ACTION_QUEUE_DB
GPT_ACTION_API_HOST=127.0.0.1
GPT_ACTION_API_PORT=8000
GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS=30000
```

`GPT_ACTION_QUEUE_DB` 必须在主进程启动时解析为规范化绝对路径，并通过配置/环境传递给翻译子进程和 sidecar。两边启动时都打印同一个绝对路径和数据库 schema version；路径不一致时 fail-fast。

认证只使用一个 Bearer API key。不实现多用户认证、OAuth、RBAC、租户隔离、多个 key 或审计平台。

sidecar 可以单独启动，也可以由后续统一 launcher 同时启动 Gradio 与 API；第一版必须先提供可独立运行、可诊断的 `pdf2zh-action-api`，不能依赖 Gradio 内部 ASGI 挂载行为。

公开接口第一版只需要：

```text
getQueueStatus
getNextBatch
submitBatch
```

GPT 不操作 PDF 文件、工作目录、终端或构建命令。

### 7.1 getQueueStatus

返回：

```json
{
  "run_id": "run_xxx",
  "status": "TRANSLATING",
  "pending": 12,
  "claimed": 8,
  "completed": 300,
  "run_active": true
}
```

`run_id` 仅用于显示和日志，GPT 不需要在后续调用中回传。`run_active` 只表示 SQLite 中 run 状态为 `ACTIVE`，不声称检测到真实进程存活。`worker_pid` 不暴露给 GPT。

### 7.2 getNextBatch

一个 Action 响应可以组合多个 BabelDOC 原始请求：

```json
{
  "run_id": "run_xxx",
  "requests": [
    {
      "request_id": "req_1",
      "claim_token": "...",
      "mode": "LLM_BATCH",
      "input": "BabelDOC 原始完整 prompt"
    },
    {
      "request_id": "req_2",
      "claim_token": "...",
      "mode": "SIMPLE_TEXT",
      "input": "..."
    }
  ]
}
```

外层聚合只组合请求，不修改内部 prompt。

`getNextBatch` 只领取唯一 `ACTIVE` run 绑定的请求。`FAILED` 或 `CANCELED` run 的未完成请求不会继续分发，避免在没有 worker 等待时制造新翻译；但已经领取的旧请求仍可通过 `submitBatch` 按下述规则提交。

边界由以下两项共同控制：

```text
max_requests
max_serialized_response_chars
```

必须按完整 JSON 序列化后的响应大小校验，不能只统计 input 长度。

`GPTActionTranslator` 在请求写入 SQLite 之前，必须使用实际 `request_id`、稳定 `claim_token`、语言、模式和 input 构造单项完整 Action envelope 进行预检。单项本身超过上限时：

```text
立即抛出明确错误
错误包含 required_chars 和 max_chars
不插入 PENDING 请求
当前 BabelDOC 调用失败，而不是永久堵塞队首
```

sidecar 与翻译进程统一读取 `GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS`，避免两边上限不一致。

### 7.3 submitBatch

```json
{
  "results": [
    {
      "request_id": "req_1",
      "claim_token": "...",
      "output": "[{\"id\":0,\"output\":\"译文\"}]"
    }
  ]
}
```

API 层只验证：

```text
request_id
claim_token
当前状态
非空输出
单项和总响应大小
```

API 根据 `request_id` 找到内部 run。只要 request 未被明确标记为 `CANCELED`，即使原 run 已经 `FAILED`、claim 已过期，也允许 first-result-wins 提交；GPT 不负责维护 run 生命周期。

BabelDOC 内部 JSON ID、placeholder、长度和 fallback 规则继续由官方 BabelDOC 验证。

建议支持部分提交：

```text
合法项立即完成
非法项单独返回错误
不因一个错误回滚整组合法结果
未提交项保持 claim 或按明确策略释放
```

---

## 8. 多 GPT 会话并行

多个 Custom GPT 会话并行消费同一个 active run：

```text
BabelDOC 官方线程池并发产生请求
→ durable queue 持续积累 PENDING
→ GPT A/B/C/D 分别事务领取不同请求
→ 乱序提交
→ 对应阻塞 translator 调用返回
→ 官方线程池继续产生后续请求
```

外层 Action batch 建议初始：

```text
每次 3～8 个内部请求
同时受序列化字符上限约束
```

这保留官方 LLM batch 语义，同时减少 Action 往返。

不需要预先生成整本 PDF 的 529 个 batch；只要官方线程池维持足够队列深度，就可以让 2～4 个 GPT 会话持续工作。

监控指标：

```text
pending queue depth
claimed queue depth
每次 Action 内部请求数
平均 claim 时长
fallback 比例
GPT 会话空队列比例
worker RSS
part 切换耗时
```

---

## 9. 健康检查

当前 `pdf2zh_next/translator/utils.py` 会执行：

```python
translator.translate("Hello", ignore_cache=True)
```

GPTActionTranslator 不能在启动时生成一个等待 GPT 的 `Hello` 请求。

应用层新增统一接口：

```python
class BaseTranslator:
    def health_check(self) -> None:
        self.translate("Hello", ignore_cache=True)
```

现有 translator 保持默认行为。

GPTActionTranslator 覆盖：

```python
def health_check(self) -> None:
    self.queue.verify_database()
    self.queue.verify_schema()
    self.queue.verify_writable()
```

`translator/utils.py` 改为调用 `translator.health_check()`。

---

## 10. 取消与请求回收

原始 PDFMathTranslate-next 已在翻译子进程中使用 cancel message 和强制 terminate/kill。

GPTActionTranslator 的阻塞等待还必须感知应用层 cancel event。

在 `high_level.py` 创建 translator/config 后绑定：

```python
if hasattr(translator, "bind_cancel_event"):
    translator.bind_cancel_event(cancel_event)
```

等待循环：

```python
while True:
    if cancel_event.is_set():
        cancel_incomplete_request(request_id)
        raise TranslationCancelled()

    result = queue.load_completed_result(request_id)
    if result is not None:
        return result

    cancel_event.wait(0.5)
```

第一版不实现 heartbeat、ABANDONED 状态或基于 worker PID 的请求状态机。原始父进程已经知道翻译子进程是正常完成、用户取消还是异常退出。

处理规则：

```text
正常完成：
  保留 COMPLETED 结果作为 durable cache

用户明确取消：
  当前 run 标记 CANCELED
  该 run 中未完成的 PENDING/CLAIMED 请求标记 CANCELED
  后续提交拒绝

子进程异常退出：
  当前 run 标记 FAILED
  不主动取消未完成请求
  FAILED run 不再分发新 claim
  GPT 已完成的旧 claim 仍可按 first-result-wins 提交并保存为 COMPLETED
```

异常退出后接受的 COMPLETED 结果可能暂时没有线程等待，但会在下一次运行遇到相同 fingerprint 时直接复用。这是个人版恢复机制的一部分，不应当把有价值的译文作为 stale 丢弃。

GPTAction 模式是人工消费队列，允许用户长时间暂停。原 `_translate_in_subprocess()` 的 30 分钟进度事件超时只保留给其他翻译器；GPTAction 使用：

```python
AsyncCallback(timeout=None)
```

因此“30 分钟没有 paragraph 完成”不会误杀仍在等待 Custom GPT 的正常 worker。明确取消仍通过原 cancel pipe 和 event 完成。

---

## 11. 崩溃恢复

不实现 IL checkpoint，也不实现 part-level resume。

worker 崩溃后重新运行：

```text
从 part 1 重新执行原始 BabelDOC pipeline
→ 相同翻译输入查询 durable queue fingerprint
→ 已完成请求直接复用
→ 未完成历史请求按 fingerprint 重新绑定到新的 ACTIVE run
→ 保留原 request_id 和稳定 claim_token
→ 原 claim 仍可提交；claim 过期后也可由新会话重新领取
→ 继续原始 split/merge 流程
```

预期结果：

```text
已完成译文不丢失
不需要重新让 GPT 翻译稳定命中的请求
会重复解析、排版和前面 part 的 PDF 构建
```

必须用同一 PDF、同一 BabelDOC 版本和同一设置重复运行，验证 prompt fingerprint 命中率。

如果 prompt 不稳定，只允许 cache miss 和重新翻译，不能做模糊匹配或错误注入。

---

## 12. 页面范围与官方分片

页面范围完全复用原始实现：

```text
settings.pdf.pages
settings.pdf.only_include_translated_page
```

不得自建页面选择状态机或 PDF 合并逻辑。

大 PDF 内存控制使用原始：

```text
settings.pdf.max_pages_per_part
BabelDOCConfig.create_max_pages_per_part_split_strategy()
```

BabelDOC 0.5.24 会顺序处理各 part：

```text
part 1: 解析 → 翻译 → 排版 → 输出 → 清理
part 2: 解析 → 翻译 → 排版 → 输出 → 清理
...
最终合并
```

938 页 PDF 初始建议：

```text
max_pages_per_part = 50
pool_max_workers = 8
GPT 会话数 = 2～4
no_auto_extract_glossary = true
```

需要观察：

```text
当前 part 等待翻译时 RSS
峰值 RSS
part 切换后 RSS
总解析时间
总翻译时间
最终 merge 时间
```

官方分片降低单个常驻 Document 的页数，但当前 part 在等待 GPT 时仍常驻内存，这是明确且可接受的权衡。

---

## 13. 自动术语提取

GPTActionSettings 需要声明 LLM 支持，原始配置系统会因此把它加入 term extraction engine 集合。

第一版必须强制：

```text
no_auto_extract_glossary = true
```

并从自动术语提取 translator 的可选集合中排除 GPTActionTranslator，避免正文前产生无法区分的术语提取请求。

未来需要支持时，新增独立模式：

```text
TERM_EXTRACTION
```

在协议、fingerprint、API 和 Custom GPT 指令中单独定义。

---

## 14. 依赖与 Docker

源码和生产环境统一固定官方版本：

```toml
babeldoc==0.5.24
pymupdf<1.25.3
```

当前 Dockerfile 中的独立命令：

```text
-U babeldoc
```

必须删除，否则会绕过项目精确依赖升级到未经验证的版本。

禁止：

```text
自建 BabelDOC fork
本地 wheel 替换
monkey patch ILTranslator
运行时修改 site-packages
```

---

## 15. 最小代码改动范围

建议新增或修改：

```text
pdf2zh_next/config/translate_engine_model.py
  GPTActionSettings
  第一版排除术语提取

pdf2zh_next/translator/translator_impl/gptaction.py
  GPTActionTranslator
  SIMPLE_TEXT / LLM_BATCH
  health_check / cancel binding

pdf2zh_next/translator/gptaction_queue.py
  SQLite schema
  fingerprint
  durable claim
  enqueue / wait / submit / recovery

pdf2zh_next/gptaction_queue_cli.py
  status
  recover-active-run

pdf2zh_next/gptaction_api.py
  FastAPI Actions API
  getQueueStatus / getNextBatch / submitBatch

pdf2zh_next/translator/base_translator.py
  health_check() 默认实现

pdf2zh_next/translator/utils.py
  调用 translator.health_check()

pdf2zh_next/high_level.py
  将子进程 cancel event 绑定到 GPTActionTranslator
  GPTAction 等待进度不设置空闲超时

Dockerfile / constraints
  固定官方 BabelDOC 0.5.24
  删除裸 -U babeldoc
```

不修改：

```text
BabelDOC 源码
babeldoc.format.pdf.high_level
ILTranslator
ILTranslatorLLMOnly
PDFCreater
Typesetting
Document IL
官方 pages
官方 split/merge
官方 placeholder
```

---

## 16. 实施阶段

### Phase 0：固定基线

1. 固定官方 BabelDOC 0.5.24；
2. 删除 Docker 裸升级；
3. 记录 Python、PyMuPDF 和 BabelDOC 精确版本；
4. 建立原始 translator 的 smoke baseline。

### Phase 1：队列原型

1. SQLite schema；
2. fingerprint；
3. enqueue/wait/result reuse；
4. durable claim；
5. claim 过期后重新领取；
6. first-result-wins 提交；
7. 单 active run 限制；
8. 单项 Action envelope 入队前大小预检。

### Phase 2：Translator 接入

1. GPTActionSettings；
2. GPTActionTranslator；
3. `do_llm_translate(None)` 探测兼容；
4. SIMPLE_TEXT / LLM_BATCH；
5. health_check；
6. 禁用自动术语提取。

### Phase 3：Actions API

1. 独立 `pdf2zh-action-api` sidecar；
2. 规范化绝对 queue DB 路径；
3. 单 Bearer 认证；
4. getQueueStatus；
5. getNextBatch；
6. submitBatch；
7. 完整响应大小限制；
8. 部分提交和幂等冲突。

### Phase 4：取消和恢复

1. cancel event 绑定；
2. run 状态与父进程退出结果绑定；
3. 明确取消时批量标记 CANCELED；
4. 异常退出后保留未完成请求和 completed 结果；
5. worker 重启结果复用；
6. 本地显式恢复孤儿 ACTIVE run；
7. GPTAction 进度等待不使用 30 分钟空闲超时；
8. 不实现 heartbeat 或 IL/part checkpoint。

### Phase 5：Web 与文档

1. Web 中增加 GPTAction translator 配置；
2. 继续使用原始 pages UI；
3. 暴露 max_pages_per_part、pool_max_workers；
4. Custom GPT Instructions；
5. OpenAPI schema；
6. 安装和单机使用说明。

---

## 17. 必要测试

### 17.1 Translator contract

```text
do_llm_translate(None) 不入队
SIMPLE_TEXT 返回纯字符串
LLM_BATCH 返回原始 BabelDOC JSON 字符串
health_check 不生成 Hello 请求
mode-aware fingerprint 不串用结果
超大单项请求在入队前明确失败且不留下 PENDING 记录
```

### 17.2 并发和 claim

```text
多个 GPT 会话领取不同请求
claim_token 必须匹配
claim 过期可重新领取
过期 claim 的第一个有效结果仍可提交
相同提交幂等
不同结果冲突
明确 CANCELED 请求拒绝提交
部分提交不回滚合法项
```

### 17.3 取消与恢复

```text
取消期间阻塞 translator 及时退出
明确取消的未完成请求变为 CANCELED
异常退出不依赖 heartbeat
异常退出后的旧 claim 仍可 first-result-wins 提交
completed 结果在重新运行时复用
worker 崩溃不需要 IL checkpoint
强制关闭后可通过本地命令将孤儿 ACTIVE run 标记 FAILED
恢复时保留 COMPLETED，释放 CLAIMED 请求
GPTAction 超过 30 分钟无进度事件不会被误杀
```

### 17.4 原始 BabelDOC 集成

```text
官方 ILTranslatorLLMOnly 正常批量
LLM 输出非法时官方 fallback 生效
placeholder 继续由官方校验
pages 行为不改变
only_include_translated_page 行为不改变
split/merge 行为不改变
mono/dual 输出正常
```

### 17.5 自动集成测试

至少测试：

```text
1 页简单文本
5 页公式/图片
50 页单 part
100 页两 part
```

自动测试还应使用模拟 2～4 个消费者覆盖 claim、乱序提交、取消和结果复用。

### 17.6 手工性能/稳定性验收

首个可用版本完成后，人工运行：

```text
938 页真实 PDF
max_pages_per_part=50
pool_max_workers=6～8
2～4 个 GPT 会话
```

该项目不进入每次 PR/CI 的常规必跑测试。

记录：

```text
worker RSS
峰值 RSS
pending/claimed 深度
Action 平均请求数
fallback 比例
part 切换耗时
完整耗时
输出页数和文件大小
```

---

## 18. 验收标准

实现完成必须满足：

1. 使用 `PDFMathTranslate-next-Origin/main` 和官方 BabelDOC 0.5.24；
2. 仓库中没有复制或修改 BabelDOC 源码；
3. 原始 `async_translate()`、pages、split/merge、排版和输出路径保持不变；
4. 多个 GPT 会话可以并行领取不同请求；
5. 请求 claim 持久化到 SQLite，claim 过期后可重新领取；
6. 过期 claim 仍按 first-result-wins 接受第一个有效结果；
7. Custom GPT 不需要提交或维护 run_id；
8. 独立 `pdf2zh-action-api` sidecar 与翻译子进程使用同一个规范化绝对 queue DB 路径；
9. LLM_BATCH 和 SIMPLE_TEXT 使用 canonical、排除运行参数的 fingerprint；
10. 健康检查不会生成等待中的 Hello 请求；
11. 自动术语提取第一版明确禁用；
12. 明确取消后未完成请求全部变为 CANCELED；
13. worker 异常退出不依赖 heartbeat，旧 claim 结果仍可保存并复用；
14. 不生成 prepared.il.xml 或自定义 Document checkpoint；
15. 大 PDF 可使用官方 max_pages_per_part 限制峰值内存；
16. Docker 不会升级到 BabelDOC 0.6.x；
17. 普通 pip 安装同样精确使用 BabelDOC 0.5.24；
18. 强制关闭后有本地显式恢复孤儿 ACTIVE run 的入口；
19. GPTAction 等待期间不受固定 30 分钟进度空闲超时影响；
20. 单项超大请求在入队前失败，不会永久堵塞队列；
21. PDF 正确性仍由原始 BabelDOC 流程保证。

---

## 19. 最终决策

正式采用：

```text
PDFMathTranslate-next-Origin
+ 官方 BabelDOC 0.5.24
+ GPTActionTranslator
+ SQLite durable queue
+ FastAPI Custom GPT Actions
```

多 GPT 并行依赖：

```text
官方 BabelDOC translation executors
+ durable request queue
+ Action 外层聚合
```

停止推进：

```text
自定义 BabelDOC
prepared IL checkpoint
analysis/build 双流水线
ActionsTranslationBridge
自定义 IL 注入
自定义 PDF 页面和 split/merge 实现
```

后续所有 GPT Actions 开发、审查、测试和 PR 都以本仓库 `main` 为基准。
