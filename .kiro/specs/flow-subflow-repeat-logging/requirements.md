# 需求文档：子流程、重复执行与日志优化

## 需求 1：子流程 Step 定义

**EARS 模式**: Ubiquitous

当用户在流程 YAML 中定义一个 step 时，系统应支持通过 `sub_flow` 字段引用另一个 YAML 流程文件路径，使该 step 作为子流程执行。

### 用户故事

- 作为流程编写者，我希望在一个 step 中引用另一个 YAML 流程文件作为子流程，以便复用已有的流程定义。
- 作为流程编写者，我希望子流程 step 与 `send` 和 `transfer_data` 互斥，即一个 step 只能是普通发送、数据传输或子流程中的一种。

### 验收标准

1. `FlowStep` 模型新增 `sub_flow` 字段（字符串类型，表示 YAML 文件路径）
2. `sub_flow`、`send`、`transfer_data` 三者互斥，同时指定多个时校验报错
3. `sub_flow` 路径支持相对路径（相对于当前流程 YAML 所在目录）和绝对路径
4. 加载时解析 `sub_flow` 引用的 YAML 文件并校验其合法性
5. 子流程 step 可以使用 `name`、`delay_ms`、`breakpoint`、`tester_present` 等通用字段
6. 通过 `flow_register_inline` 注册的流程中，`sub_flow` 路径必须为绝对路径，否则校验报错



## 需求 2：子流程嵌套执行

**EARS 模式**: Ubiquitous

当流程引擎执行到子流程 step 时，系统应递归加载并执行引用的子流程，子流程内部也可以包含子流程 step（嵌套）。

### 用户故事

- 作为流程编写者，我希望子流程可以嵌套引用其他子流程，以便构建复杂的多层流程。
- 作为流程编写者，我希望系统对嵌套深度有合理限制，防止无限递归。

### 验收标准

1. 子流程执行时递归调用流程引擎的 step 执行逻辑
2. 子流程继承父流程的 `variables`（子流程可读写，修改会反映到父流程作用域）
3. 子流程继承父流程的 `tester_present_policy`，除非子流程自身定义了不同的策略
4. 子流程的 trace 记录合并到父流程的 trace 中，带有层级标识
5. 嵌套深度限制为 10 层，超过时抛出明确错误
6. 子流程中任何 step 失败，整个流程（包括父流程）立即失败退出
7. 子流程支持 `stop_requested` 信号传播（父流程停止时子流程也停止）

## 需求 3：Step 重复执行

**EARS 模式**: Ubiquitous

当用户在 step 定义中指定 `repeat` 字段时，系统应按指定次数重复执行该 step。

### 用户故事

- 作为流程编写者，我希望能对任意 step（包括子流程 step）指定重复次数，以便实现重试或循环发送场景。
- 作为流程编写者，我希望每次重复后如果 step 有 `delay_ms`，则执行延时。
- 作为流程编写者，我希望任何一次重复失败时流程立即退出。

### 验收标准

1. `FlowStep` 模型新增 `repeat` 字段（整数类型，默认值 1，最小值 1）
2. `repeat > 1` 时，step 执行逻辑循环 `repeat` 次
3. 每次重复执行完成后，如果 `delay_ms > 0`，执行延时等待
4. 任何一次重复执行失败（响应不匹配 expect 或异常），流程立即以 FAILED 状态退出
5. 子流程 step 也支持 `repeat`，即整个子流程重复执行指定次数
6. trace 中每次重复记录包含 `repeat_index` 字段（从 0 开始）
7. `repeat` 值为 1 时行为与当前完全一致（向后兼容）
8. YAML 序列化/反序列化支持 `repeat` 字段



## 需求 4：流程执行结果精简返回

**EARS 模式**: Ubiquitous

当流程执行完成后，`flow_status` 工具应返回精简的执行摘要而非完整的 trace 日志，以避免产生巨大的上下文。

### 用户故事

- 作为 MCP 使用者，我希望流程执行完成后只获取执行结果摘要和流程标识符，而不是完整的原始日志，以减少上下文大小。
- 作为 MCP 使用者，当流程失败时，我希望额外获取失败 step 的日志信息，以便快速定位问题。

### 验收标准

1. `flow_status` 返回精简摘要：`run_id`、`flow_name`、`status`、`current_step`、`error`、`step_count`（总 step 数）、`message_count`（总消息数）
2. 不再在 `flow_status` 中返回完整的 `trace` 列表
3. 当 `status` 为 `FAILED` 时，额外返回 `failed_step_trace`：失败 step 的最近 N 条 trace 记录（N 可配置，默认 50）
4. 对于 `transfer_data` 类型的大 step 失败，`failed_step_trace` 只返回最后 N 条消息记录（避免返回数百条传输记录）
5. 向后兼容：保留完整 trace 在内存中，可通过新的检索工具访问

## 需求 5：流程日志检索工具

**EARS 模式**: Ubiquitous

系统应提供一个新的 MCP 工具 `flow_trace_search`，允许通过流程标识符和搜索模式检索流程执行日志。

### 用户故事

- 作为 MCP 使用者，我希望通过 `run_id` 和搜索字符串（支持正则表达式）检索流程日志中的相关内容。
- 作为 MCP 使用者，我希望搜索结果只返回匹配的条目，而不是全部日志。

### 验收标准

1. 新增 MCP 工具 `flow_trace_search`，参数：`run_id`（字符串）、`pattern`（字符串，支持普通子串匹配和正则表达式）
2. 在 trace 记录中搜索匹配 `pattern` 的条目（搜索范围包括 step 名称、request_hex、response_hex 等字段的字符串表示）
3. 返回匹配的 trace 条目列表，每条包含完整的 trace 记录信息
4. 支持 `limit` 参数限制返回条目数量（默认 100）
5. 当 `run_id` 不存在时返回明确错误
6. 正则表达式语法错误时返回明确错误信息



## 需求 6：日志实时落盘

**EARS 模式**: Complex (Event-driven + State-driven)

当配置了 `persist_dir` 时，系统应在每条日志事件产生时实时写入磁盘文件，以防止长流程执行过程中因异常导致日志丢失。

### 用户故事

- 作为系统管理员，我希望可以配置日志持久化目录，使日志在产生时实时写入磁盘。
- 作为系统管理员，我希望未配置持久化目录时，系统行为与当前完全一致（纯内存模式）。

### 验收标准

1. `EventStore` 支持可选的 `persist_dir` 参数（`Path | None`）
2. 当 `persist_dir` 不为 `None` 时，每次 `append()` 在写入内存的同时，以追加模式写入磁盘文件
3. 磁盘文件格式为 JSON Lines（每行一个 JSON 对象），文件名基于日期或 session 标识
4. 写入磁盘操作线程安全，不阻塞主流程执行（考虑使用缓冲写入）
5. 未配置 `persist_dir` 时，行为与当前纯内存模式完全一致
6. `AppConfig` 新增 `log_persist_dir` 配置项，支持环境变量和 TOML 配置

## 需求 8：BLF 实时流式写入

**EARS 模式**: Event-driven

当用户在 CLI `flow-run` 或 MCP `flow_start` 中指定 `blf_output` 路径时，系统应在流程执行过程中实时将 CAN 事件写入 BLF 文件，而非事后批量导出。

### 用户故事

- 作为流程执行者，我希望在启动流程时指定 BLF 输出路径，流程结束后直接获得 BLF 文件，无需再手动调用 `log_export_blf`。
- 作为流程执行者，我希望未指定 BLF 输出路径时，行为与当前完全一致。

### 验收标准

1. CLI `flow-run` 新增 `--blf-output` 可选参数
2. MCP `flow_start` 新增 `blf_output` 可选参数
3. 指定路径时，`BlfExporter` 以流式模式运行：在 `EventStore.append()` 时同步将 CAN_TX/CAN_RX 事件写入 BLF 文件
4. 流程结束（DONE/FAILED/STOPPED）后自动关闭 BLF writer
5. 未指定路径时，行为与当前完全一致（不写入 BLF）
6. 现有的 `log_export_blf` 批量导出工具保持不变

## 需求 7：子流程 YAML 序列化

**EARS 模式**: Ubiquitous

当保存包含子流程 step 的流程定义时，系统应正确序列化 `sub_flow` 字段到 YAML 文件。

### 用户故事

- 作为流程编写者，我希望包含子流程引用的流程定义可以正确保存和加载。

### 验收标准

1. `dump_flow_yaml` 正确序列化包含 `sub_flow` 字段的 step
2. `load_flow_yaml` 正确反序列化包含 `sub_flow` 字段的 step
3. 序列化后的 YAML 中 `sub_flow` 保持为相对路径（相对于输出文件所在目录）
4. 加载-保存-加载的往返测试通过

## 需求 9：流程 Step 寻址模式控制

**EARS 模式**: Ubiquitous

当用户在流程 YAML 中定义 step 时，系统应支持通过 `addressing_mode` 字段控制该 step 使用物理寻址还是功能寻址。

### 用户故事

- 作为流程编写者，我希望能在 step 级别指定 UDS 寻址模式（物理/功能），以便在同一流程中对不同 step 使用不同的寻址方式。
- 作为流程编写者，我希望有一个流程级别的默认寻址模式，step 默认继承流程级别的设置。

### 验收标准

1. `FlowStep` 模型新增 `addressing_mode` 字段，类型为 `Literal["physical", "functional", "inherit"]`，默认值 `"inherit"`
2. `FlowDefinition` 模型新增 `default_addressing_mode` 字段，类型为 `Literal["physical", "functional"]`，默认值 `"physical"`
3. step 的 `addressing_mode` 为 `"inherit"` 时，使用 `FlowDefinition.default_addressing_mode` 的值
4. 流程引擎在调用 `UdsClientService.send()` 时传入解析后的寻址模式
5. 子流程 step 的寻址模式解析：子流程内部 step 的 `"inherit"` 继承子流程自身的 `default_addressing_mode`（如果子流程定义了的话），否则继承父流程的
6. YAML 序列化/反序列化支持 `addressing_mode` 和 `default_addressing_mode` 字段
7. 向后兼容：未指定时默认行为与当前一致（物理寻址）

---

## 设计决策记录

### D1: 子流程变量作用域 — 共享
子流程与父流程共享 `variables` 字典（同一引用），子流程的修改直接反映到父流程。理由：隔离作用域实现复杂且实际使用中子流程通常需要读写父流程变量。

### D2: Trace 存储策略 — 保持现状
- `FlowRun.trace` 已按 run_id 隔离，`flow_trace_search` 基于此搜索
- `EventStore` 保持全局共享事件流，CAN/UDS 层面事件天然跨流程（共享物理总线）
- 单独发送 UDS/CAN 和流程执行的事件混在 `EventStore` 中是合理的

### D3: BLF 实时写入 — 低优先级增强
`python-can` 的 `BLFWriter` 本身支持流式 `on_message_received`，技术上可行。主要价值是流程结束后直接有 BLF 文件，无需手动调用 `log_export_blf`。JSON Lines 落盘已解决核心的日志丢失问题，BLF 实时写入作为便利性增强。

### D4: flow_capabilities 和 flow_templates 同步更新
新增 `sub_flow`、`repeat`、`addressing_mode` 字段后，`flow_capabilities` 工具的能力描述和 `flow_templates` 的模板生成需要同步更新，确保 AI/工具发现机制能感知新能力。

### D5: CLI flow-run 输出精简
CLI `flow-run` 直接打印 `flow_engine.status()` 的返回值，需求 4 改了 `status()` 后 CLI 自然受益。可考虑增加 `--verbose` 选项用于调试时获取完整 trace。

---

## 正确性属性

### P1: 子流程互斥性
对于任意 `FlowStep`，`send`、`transfer_data`、`sub_flow` 三个字段中最多只有一个非空，且至少有一个非空。

### P2: 重复执行次数一致性
对于 `repeat = N` 的 step，trace 中该 step 的记录数量恰好为 N 倍于单次执行的记录数量（假设所有重复均成功）。

### P3: 重复失败快速退出
对于 `repeat > 1` 的 step，如果第 K 次（K < repeat）执行失败，则不会有第 K+1 次执行的 trace 记录。

### P4: 嵌套深度限制
子流程嵌套深度超过 10 层时，系统必须抛出错误而非无限递归。

### P5: 日志精简返回
`flow_status` 返回的数据中不包含完整的 `trace` 列表，但包含 `step_count` 和 `message_count` 统计信息。

### P6: 日志检索完整性
通过 `flow_trace_search` 使用空模式（匹配所有）搜索的结果数量等于该 run 的完整 trace 长度。
