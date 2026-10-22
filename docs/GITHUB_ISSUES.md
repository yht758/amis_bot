# GitHub Issues（可直接复制创建）

## 1. [Enhancement] 实现流式输出与消息分片，模拟真人打字节奏

## 背景 / 动机
真人聊天是短句连发，整段返回让首字等待等于全文生成时间，机器感强；群聊里长段落还容易刷屏、打断阅读节奏。流式加分片能降低首字延迟、贴近真实节奏。

## 现状与痛点
`src/im_bridge/core/llm.py` 已支持 SSE 流式与重试，`src/im_bridge/core/chunker.py` 的 `ChunkStreamer.feed(delta)` 支持增量切片，`src/im_bridge/core/engine.py` 在流式回调里边收边发，开关为 `AMIS_STREAMING_ENABLED`、`AMIS_CHUNKING_ENABLED`、`AMIS_CHUNK_MIN_DELAY`、`AMIS_CHUNK_MAX_DELAY`（默认开启）。不足：切片只识别中英文标点，代码块、URL、英文句点仍可能被截断；延迟为均匀随机，缺少句末长停顿与“正在输入”节拍；无单次回复段数上限与平台单条长度上限；分片被关闭时不按平台长度上限拆条；断流后已发送分段无法撤回，只能保证不重复发送。

## 期望行为 / 验收标准
- [ ] 300 字回复切成 N>1 段，按序拼接与原文逐字一致。
- [ ] 段间隔在 `[AMIS_CHUNK_MIN_DELAY, AMIS_CHUNK_MAX_DELAY]` 内，日志可查。
- [ ] 首段下发早于整段生成完成。
- [ ] 代码块、URL 不被切断。
- [ ] 关闭分片时，超长文本按平台单条长度上限拆条发送。
- [ ] 断流或模型报错时，已发分段不重复，剩余内容一次发出。
- [ ] `python run.py --check-config` 展示延迟参数。

## 实现建议 / 任务拆解
方案：① token 级直发，最拟真但频率高、易限流；② 分句流式，在标点边界 flush，节奏可控。推荐 ②：`llm.py` 暴露异步增量迭代器，`chunker.py` 增加 `feed(delta)` 与边界优先级（句末 > 逗号 > 硬上限），`engine.py` 消费增量并调用 `MessageTransport.send`，延迟按边界取样。任务：增量切分与单测、开关矩阵、异常回退、配置展示。

## 影响范围 / 风险
开关关闭时完全回退一次性发送；断流时已发内容不重发，剩余内容降级一次发出；顺序由队列保证。默认开启，可通过 `AMIS_CHUNKING_ENABLED=false` 回退；后续要兼容代码块/URL 保护与平台单条长度上限。

## 参考
OpenAI Chat Completions 流式响应（SSE）；LangChain `stream` 抽象；OneBot v11 发送限速约定。

---

## 2. [Feature] 引入上下文摘要机制 (Summary Buffer)，优化高频短文本的 Token 消耗

## 背景 / 动机
多用户高频短文本场景下，全量历史使 token、延迟、费用随轮次上涨；只做窗口截断又会丢掉早期人设与约定。摘要加最近原文可在可控成本下保留长期记忆。

## 现状与痛点
`src/im_bridge/core/context.py` 已实现多用户隔离、Summary Buffer 摘要、窗口兜底与压缩失败降级，`src/im_bridge/core/state.py` 保存用户状态，键为 `AMIS_SUMMARY_ENABLED`、`AMIS_SUMMARY_THRESHOLD`、`AMIS_SUMMARY_KEEP_RECENT`、`AMIS_MAX_HISTORY`。不足：压缩在回复链路上同步等待，触发轮次会出现一次额外延迟；阈值按消息条数而非 token，长消息场景不够精确；摘要 Prompt 的保留项还可细化（人设、称呼、约定、未完成事项）；压缩调用没有全局节流，多用户同时触发时可能撞 API 限流；摘要只存内存，重启后丢失，且无法做幂等重放。

## 期望行为 / 验收标准
- [ ] 触发摘要后，下一轮输入 token 估算值低于触发前。
- [ ] 最近 `AMIS_SUMMARY_KEEP_RECENT` 条消息逐字保留。
- [ ] 压缩不阻塞主回复，额外延迟 < 100ms 或完全异步。
- [ ] 摘要失败自动回退窗口截断，回复照常且不丢最近 N 条。
- [ ] 5 个用户并发压缩互不串扰，`/reset` 清空历史与摘要。
- [ ] 摘要长度有上限，长会话 prompt 体积稳定。

## 实现建议 / 任务拆解
方案：① 同步压缩，简单但延迟尖刺；② 异步后台压缩 + 单飞；③ token 阈值加条数兜底。推荐 ②③：`context.py` 增加 token 估算（有 tokenizer 用之，否则按字符近似），压缩任务投递到 `engine.py` 后台队列，每用户锁去重；Prompt 固定保留人设、称呼、约定、未完成事项，丢弃指令文本。任务：估算与阈值、异步队列去重、Prompt 与长度上限、失败回退与幂等、并发与 `/reset` 用例。

## 影响范围 / 风险
摘要多一次模型调用，以成本换长期稳定；信息有损，需最近原文兜底。`AMIS_SUMMARY_ENABLED` 已默认开启，可随时关闭；`state.py` 持久化需兼容旧数据。失败回退已保证用户可感知行为不变。

## 参考
LangChain `ConversationSummaryBufferMemory`；MemGPT 的分层记忆思路；OpenAI Chat Completions 的 messages 拼接约定。

---

## 3. [Improvement] 完善 .env.example 中的配置校验与报错提示

## 背景 / 动机
新用户第一步是复制 `.env.example` 为 `.env`。配置缺失或格式错误时往往到真正请求才失败，报错来自底层 HTTP/WS 库，难以定位字段。字段级校验与修复提示能显著降低上手成本。

## 现状与痛点
`src/im_bridge/config.py` 已按「环境变量 > `.env` > `config.secret.json` > `config.json`」加载，`Config.validate()` 会聚合校验必填 API Key、URL 协议、数值范围、分片延迟交叉约束等并一次列出全部问题；`python run.py --check-config` 可提前检查，示例在 `.env.example`。不足：报错不打印当前值（或脱敏值）与合法区间；URL 未校验是否误带 `/chat/completions`；`AMIS_MAX_HISTORY` 与摘要阈值的关系未做交叉校验；布尔值接受口径需与 `.env.example` 注释统一；`.env.example` 键名易漂移且无一致性测试；启动摘要之外的日志/报错缺少密钥脱敏。

## 期望行为 / 验收标准
- [ ] 缺 `AMIS_API_KEY` 时启动立即失败，输出字段名、来源与修复示例。
- [ ] `--check-config` 一次列出全部问题，含字段、当前值（脱敏）、期望格式、修复建议，退出码非 0。
- [ ] `AMIS_BASE_URL` 非 http(s)、缺 `/v1`、带空格时有明确提示；数值越界提示区间。
- [ ] 交叉约束有独立报错。
- [ ] `.env.example` 与 `config.py` 的键、默认值一一对应并有单测。
- [ ] 日志与报错不出现明文 `AMIS_API_KEY`。

## 实现建议 / 任务拆解
方案：① `pydantic-settings` 声明式校验，文档同源但新增依赖；② 纯手写校验，零依赖但元数据分散；③ 手写字段表加轻量校验器。推荐 ③：`config.py` 维护 `FIELD_SPECS`（名称/类型/必填/默认/范围/说明），加载时聚合错误，`--check-config` 按表渲染。任务：字段表与聚合校验、错误含来源与修复提示、输出与退出码、`.env.example` 同步加单测、密钥脱敏。

## 影响范围 / 风险
错误文案与退出码变化影响现有脚本，需在文档注明；严格校验可能让原本能跑的宽松配置失败，建议区分 error 与 warning，先以 warning 发布一版；脱敏不得影响真实请求。

## 参考
`pydantic-settings`；`python-dotenv`；The Twelve-Factor App 的 Config 原则。
