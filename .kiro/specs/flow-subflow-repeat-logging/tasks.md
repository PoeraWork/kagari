# Implementation Plan: 子流程、重复执行与日志优化

## Overview

按依赖顺序实现：先 schema 变更，再 engine 逻辑，然后 EventStore 持久化和 BLF 流式写入，接着 flow_status 精简和 flow_trace_search，最后 MCP/CLI 接口更新和模板同步。每个任务完成后运行 `uv run ruff check` + `uv run ruff format` + `uv run pytest`。

## Tasks

- [x] 1. Schema 变更：FlowStep 和 FlowDefinition 模型扩展
  - [x] 1.1 扩展 FlowStep 模型，新增 `sub_flow`、`repeat`、`addressing_mode` 字段
    - 在 `uds_mcp/flow/schema.py` 的 `FlowStep` 中新增：
      - `sub_flow: str | None = None`
      - `repeat: int = Field(default=1, ge=1)`
      - `addressing_mode: Literal["physical", "functional", "inherit"] = "inherit"`
    - 修改 `_validate_request_source`：`send`、`transfer_data`、`sub_flow` 三者互斥且至少一个非空
    - 子流程 step 不允许设置 `before_hook`、`message_hook`、`after_hook`、`expect`
    - _Requirements: 1.1, 1.2, 1.5, 3.1, 9.1_

  - [x] 1.2 扩展 FlowDefinition 模型，新增 `default_addressing_mode` 字段
    - 在 `uds_mcp/flow/schema.py` 的 `FlowDefinition` 中新增：
      - `default_addressing_mode: Literal["physical", "functional"] = "physical"`
    - _Requirements: 9.2_

  - [x] 1.3 更新 YAML 序列化/反序列化逻辑
    - 在 `load_flow_yaml` 中解析 `sub_flow` 相对路径为绝对路径（与 hook script_path 相同逻辑）
    - 在 `dump_flow_yaml` 中将 `sub_flow` 绝对路径转换回相对路径
    - 新增 `_resolve_sub_flow_path` 和 `_relativize_sub_flow_path` 辅助函数
    - _Requirements: 1.3, 7.1, 7.2, 7.3_

  - [x] 1.4 Write property test for send/transfer_data/sub_flow 互斥性
    - **Property 1: send/transfer_data/sub_flow 互斥性**
    - 使用 hypothesis 生成随机的 send/transfer_data/sub_flow 组合，验证恰好一个非空时通过校验，其他情况拒绝
    - **Validates: Requirements 1.2, 1.5, 3.1**

  - [x] 1.5 Write property test for YAML 序列化往返
    - **Property 2: 子流程 YAML 序列化往返**
    - 生成包含新字段的合法 FlowDefinition，验证 dump_flow_yaml → load_flow_yaml 往返一致
    - **Validates: Requirements 3.8, 7.1, 7.2, 7.3, 7.4, 9.6**

  - [x] 1.6 Write unit tests for schema 变更
    - 在 `tests/test_flow_schema.py` 中扩展测试：sub_flow step 创建、互斥校验、repeat 边界值、addressing_mode 默认值
    - _Requirements: 1.1, 1.2, 1.5, 3.1, 3.7, 9.1, 9.2, 9.7_

- [x] 2. Checkpoint - 确保 schema 变更测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. FlowEngine 子流程与重复执行逻辑
  - [x] 3.1 重构 `_run_flow` 提取 `_run_step` 方法
    - 在 `uds_mcp/flow/engine.py` 中将 step 执行逻辑从 `_run_flow` 提取到 `_run_step` 方法
    - `_run_step` 接收 `depth`、`repeat_index` 参数
    - 保持现有行为不变（向后兼容）
    - _Requirements: 2.1, 3.7_

  - [x] 3.2 实现 repeat 循环逻辑
    - 在 `_run_step` 中实现 `repeat > 1` 时的循环执行
    - 每次重复后执行 `delay_ms` 延时
    - 任何一次失败立即退出循环
    - trace 记录中添加 `repeat_index` 字段
    - _Requirements: 3.2, 3.3, 3.4, 3.6_

  - [x] 3.3 实现 `_run_sub_flow` 子流程递归执行
    - 新增 `_run_sub_flow` 方法，加载子流程 YAML 并递归执行
    - 子流程共享父流程 `variables`（同一 dict 引用）
    - 子流程 trace 合并到父流程 trace，带 `sub_flow_depth` 和 `sub_flow_name` 字段
    - 嵌套深度限制 10 层
    - 子流程继承父流程 `tester_present_policy`
    - 支持 `stop_requested` 信号传播
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 3.4 实现寻址模式解析 `_resolve_addressing_mode`
    - 新增辅助函数：step 的 `addressing_mode` 为 `"inherit"` 时使用 flow 的 `default_addressing_mode`
    - 在 `_run_step` 中调用 `UdsClientService.send()` 时传入解析后的寻址模式
    - 子流程内部 step 的 `"inherit"` 优先使用子流程自身的 `default_addressing_mode`
    - _Requirements: 9.3, 9.4, 9.5_

  - [x] 3.5 Write property test for 子流程变量共享
    - **Property 3: 子流程变量共享**
    - 验证子流程中对 variables 的修改反映到父流程
    - **Validates: Requirements 2.2**

  - [x] 3.6 Write property test for 重复执行 trace 正确性
    - **Property 6: 重复执行 trace 正确性**
    - 验证 repeat=N 时 trace 中 repeat_index 从 0 到 N-1 递增
    - **Validates: Requirements 3.2, 3.6, 3.7**

  - [x] 3.7 Write property test for 重复执行失败快速退出
    - **Property 7: 重复执行失败快速退出**
    - 验证第 K 次失败后无 K+1 及更大的 repeat_index 记录
    - **Validates: Requirements 3.4**

  - [x] 3.8 Write property test for 子流程 trace 合并与层级标识
    - **Property 4: 子流程 trace 合并与层级标识**
    - 验证子流程 trace 记录的 sub_flow_depth 等于实际嵌套深度
    - **Validates: Requirements 2.4**

  - [x] 3.9 Write property test for 子流程失败传播
    - **Property 5: 子流程失败传播**
    - 验证子流程 step 失败时父流程立即 FAILED，无后续 step 记录
    - **Validates: Requirements 2.6**

  - [x] 3.10 Write property test for 寻址模式解析
    - **Property 14: 寻址模式解析**
    - 验证 inherit 时使用 flow 默认值，显式指定时使用 step 自身值
    - **Validates: Requirements 9.3, 9.4, 9.7**

  - [x] 3.11 Write property test for 子流程寻址模式继承
    - **Property 15: 子流程寻址模式继承**
    - 验证子流程内部 step inherit 时优先使用子流程的 default_addressing_mode
    - **Validates: Requirements 9.5**

  - [x] 3.12 Write unit tests for 子流程和重复执行
    - 在 `tests/test_flow_engine.py` 中扩展测试：
      - 基本子流程执行、嵌套执行、变量共享、失败传播、stop 信号传播
      - repeat=1 向后兼容、repeat=N 正常执行、repeat 失败快速退出
      - 子流程 + repeat 组合、寻址模式解析
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 3.2, 3.4, 3.5, 3.7, 9.3, 9.4, 9.5_

- [x] 4. Checkpoint - 确保子流程和重复执行测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. EventStore 日志落盘
  - [x] 5.1 扩展 EventStore 支持 `persist_dir` 和 listener 机制
    - 在 `uds_mcp/logging/store.py` 中：
      - `__init__` 新增 `persist_dir: Path | None = None` 参数
      - 配置 persist_dir 时创建 JSON Lines 文件（文件名含时间戳）
      - `append()` 同时写入内存和磁盘（JSON Lines 格式，每行一个 JSON 对象）
      - 新增 `add_listener` / `remove_listener` 方法（用于 BLF 流式写入回调）
      - 新增 `close()` 方法关闭文件句柄
    - 未配置 persist_dir 时行为与当前完全一致
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 5.2 扩展 AppConfig 新增 `log_persist_dir` 配置项
    - 在 `uds_mcp/config.py` 的 `AppConfig` 中新增 `log_persist_dir: Path | None = None`
    - 支持环境变量 `UDS_MCP_LOG_PERSIST_DIR` 和 TOML `[app].log_persist_dir`
    - 更新 `from_env`、`from_toml_dict`、`to_toml`、`to_dict`、`resolve_paths` 方法
    - _Requirements: 6.6_

  - [x] 5.3 Write property test for EventStore 日志落盘往返
    - **Property 11: EventStore 日志落盘往返**
    - 验证 persist_dir 配置时内存和磁盘数据一致
    - **Validates: Requirements 6.2, 6.3**

  - [x] 5.4 Write property test for EventStore 无持久化向后兼容
    - **Property 12: EventStore 无持久化向后兼容**
    - 验证未配置 persist_dir 时行为不变且无磁盘文件
    - **Validates: Requirements 6.5**

  - [x] 5.5 Write unit tests for EventStore 落盘
    - 在 `tests/test_event_store.py` 中扩展测试：persist_dir 配置、JSON Lines 格式验证、close 方法、无配置时向后兼容
    - _Requirements: 6.1, 6.2, 6.3, 6.5_

- [x] 6. BLF 实时流式写入
  - [x] 6.1 扩展 BlfExporter 支持流式写入模式
    - 在 `uds_mcp/logging/exporters/blf.py` 中新增：
      - `start_streaming(output_path: Path)` 方法：创建 BLFWriter 实例
      - `on_event(event: LogEvent)` 方法：EventStore 监听器回调，将 CAN_TX/CAN_RX 事件写入 BLF
      - `stop_streaming()` 方法：关闭 BLFWriter
    - 通过 `EventStore.add_listener(blf_exporter.on_event)` 注册
    - 现有 `export()` 批量导出方法保持不变
    - _Requirements: 8.3, 8.4, 8.5, 8.6_

  - [x] 6.2 Write property test for BLF 流式写入完整性
    - **Property 13: BLF 流式写入完整性**
    - 验证流式写入期间所有 CAN_TX/CAN_RX 事件写入 BLF 文件
    - **Validates: Requirements 8.3**

  - [x] 6.3 Write unit tests for BLF 流式写入
    - 新增 `tests/test_blf_streaming.py`：start/stop streaming、事件捕获、流程结束自动关闭
    - _Requirements: 8.3, 8.4, 8.5_

- [x] 7. Checkpoint - 确保 EventStore 和 BLF 测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. flow_status 精简返回与 flow_trace_search
  - [x] 8.1 修改 FlowEngine.status() 返回精简摘要
    - 在 `uds_mcp/flow/engine.py` 中修改 `status()` 方法：
      - 返回 `run_id`、`flow_name`、`status`、`current_step`、`error`、`step_count`、`message_count`
      - 不再返回完整 `trace`
      - FAILED 时额外返回 `failed_step_trace`（最近 50 条）
    - 新增 `_count_steps(trace)` 辅助方法统计唯一 step 名称数量
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x] 8.2 实现 flow_trace_search 方法
    - 在 `uds_mcp/flow/engine.py` 中新增 `trace_search(run_id, pattern, limit=100)` 方法
    - 对每条 trace 记录的字符串字段值拼接后用 `re.search(pattern, ...)` 匹配
    - run_id 不存在时抛出 KeyError，正则语法错误时抛出 ValueError
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 8.3 Write property test for flow_status 精简返回结构
    - **Property 8: flow_status 精简返回结构**
    - 验证返回字典包含必要字段、不含 trace 键、FAILED 时含 failed_step_trace
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4**

  - [x] 8.4 Write property test for flow_trace_search 搜索正确性
    - **Property 9: flow_trace_search 搜索正确性**
    - 验证 `".*"` 搜索返回完整 trace（受 limit 限制）
    - **Validates: Requirements 5.2, 5.3**

  - [x] 8.5 Write property test for flow_trace_search limit 限制
    - **Property 10: flow_trace_search limit 限制**
    - 验证返回结果数量不超过 limit
    - **Validates: Requirements 5.4**

  - [x] 8.6 Write unit tests for flow_status 和 flow_trace_search
    - 新增 `tests/test_flow_status.py` 和 `tests/test_flow_trace_search.py`
    - 测试精简返回验证、FAILED 时 failed_step_trace、基本搜索、正则搜索、limit 限制、错误处理
    - _Requirements: 4.1, 4.2, 4.3, 5.1, 5.2, 5.4, 5.5, 5.6_

- [x] 9. Checkpoint - 确保 flow_status 和 trace_search 测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. MCP/CLI 接口更新与模板同步
  - [x] 10.1 更新 MCP server 工具
    - 在 `uds_mcp/server.py` 中：
      - `flow_start` 新增 `blf_output: str | None = None` 参数，启动 BLF 流式写入
      - 新增 `flow_trace_search` MCP 工具，调用 `FlowEngine.trace_search()`
      - `flow_register_inline` 校验 `sub_flow` 必须为绝对路径
      - `flow_capabilities` 更新能力描述（新增 `sub_flow`、`repeat`、`addressing_mode` 说明）
    - 更新 `AppState.__init__` 传入 `persist_dir` 到 EventStore
    - _Requirements: 1.6, 5.1, 8.1, 8.2_

  - [x] 10.2 更新 CLI flow-run 命令
    - 在 `uds_mcp/cli.py` 中：
      - `flow-run` 新增 `--blf-output` 可选参数
      - `flow-run` 新增 `--verbose` 参数（输出完整 trace 而非精简摘要）
      - 默认输出使用精简的 `flow_status` 返回
    - _Requirements: 8.1_

  - [x] 10.3 更新 flow_templates 模板生成
    - 在 `uds_mcp/flow/templates.py` 中：
      - `create_flow_template` 新增 `default_addressing_mode` 参数支持
    - _Requirements: 9.6_

  - [x] 10.4 Write property test for inline 注册子流程路径校验
    - **Property 16: inline 注册子流程路径校验**
    - 验证 `flow_register_inline` 中 sub_flow 为相对路径时注册失败
    - **Validates: Requirements 1.6**

  - [x] 10.5 Write unit tests for MCP/CLI 变更
    - 扩展现有测试覆盖 CLI 新参数、MCP 新工具、模板新参数
    - _Requirements: 1.6, 8.1, 8.2, 9.6_

- [x] 11. Final checkpoint - 确保所有测试通过
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- 每个任务完成后运行 `uv run ruff check && uv run ruff format && uv run pytest`
