## Plan: UDS MCP 从零到一架构与实施

面向你当前需求，建议先做 `STDIO MCP + 单ECU单流程MVP`，采用 `YAML流程定义 + Python扩展注入` 的双层编排模型；通信层固定采用 `py-uds + python-can`（其中 `python-can` 负责常见CAN设备抽象，`py-uds` 负责UDS会话与服务能力），流程执行层实现异步观测、可停止、可断点与会话保活（3E），日志层先落地 BLF 导出并预留错误帧/CAN TP测试扩展点。

**Steps**
1. Phase 1: 基线架构与运行骨架
1. 定义系统分层：`MCP接口层`、`UDS/CAN通信层`、`流程引擎层`、`日志与持久化层`（后续步骤全部依赖此分层）
2. 选定 MCP 形态为 `STDIO`，并基于官方 Python MCP SDK `mcp`（`FastMCP`）实现服务，再设计工具接口：`uds.send`、`can.send`、`can.tail`、`flow.start`、`flow.status`、`flow.stop`、`flow.breakpoint.*`、`flow.inject.*`、`flow.save/load`、`log.export`
3. 规划配置体系：运行配置（CAN通道/波特率/地址）、流程仓库路径、扩展函数白名单策略

2. Phase 2: 通信与会话能力（*depends on Phase 1*）
1. 基于 `python-can` 实现通信接入层：统一 `send/recv/subscribe`，通过 `bustype/channel/bitrate` 配置复用其现有硬件抽象
2. 基于 `py-uds` 封装 UDS 会话客户端：请求发送、响应解析、NRC分类、超时控制
3. 建立 3E TesterPresent 会话保活机制：按会话上下文自动维持，支持在断点停留期间持续保活
4. 增加报文日志采集通道：原始CAN帧 + UDS语义事件双轨记录

3. Phase 3: 流程引擎与编排 DSL（*depends on Phase 2*）
1. 定义 YAML DSL：步骤包含 `send`、`timeout`、`expect`、`on_nrc`、`retry`、`variables`、`hooks`
2. 构建异步执行器：流程实例状态机（`RUNNING/PAUSED/BREAKPOINT/STOPPED/FAILED/DONE`）+ 步骤级事件流
3. 支持断点与注入：命中断点后允许 `单报文注入`、`修改后续步骤发送内容`、`修改期望回复`
4. 设计 Python 扩展注入：支持 `文件路径 + 函数名 + 代码片段` 组合，运行时可读取上下文动态填充数据
5. 增加安全沙箱策略（MVP级）：限制可导入模块、执行超时、显式输入输出契约

4. Phase 4: 流程共享、日志导出与可观测性（*parallel with early testing in Phase 3*）
1. 流程持久化格式：`flow.yaml + metadata.json`，包含版本、作者、兼容性、扩展函数依赖摘要
2. 流程仓库机制：支持导入/导出（本地目录或压缩包）以便团队共享
3. 日志导出：实现按起止时间过滤并导出 BLF，保留 ASC/CSV 扩展接口
4. 执行追踪：支持查询运行步骤时间线、异常点、最后N条CAN/UDS消息

5. Phase 5: 验证与未来扩展位（*depends on Phase 2-4*）
1. 建立分层测试：DSL解析测试、流程状态机测试、UDS响应匹配测试、日志过滤导出测试
2. 接入仿真与真机双环境回归：Linux vcan + Windows USB-CAN
3. 预留扩展接口：错误帧记录模型、CAN TP一致性测试任务模型、批量测试套件入口

**Relevant files**
- `/Users/poera/Project/Poera/uds_mcp/main.py` — 重构为启动入口，仅做依赖装配与服务启动
- `/Users/poera/Project/Poera/uds_mcp/pyproject.toml` — 增补官方 Python MCP SDK `mcp`（建议 `mcp[cli]`）、CAN后端、日志导出、测试工具依赖
- `/Users/poera/Project/Poera/uds_mcp/README.md` — 增加架构说明、流程DSL说明、运行/调试/导出指南
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/server.py` — MCP工具注册与请求路由
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/can/interface.py` — 基于 `python-can` 的统一CAN接口封装（设备差异由 `python-can` 处理）
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/can/config.py` — `bustype/channel/bitrate` 等CAN配置与校验
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/uds/client.py` — UDS请求/响应封装与超时策略
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/flow/schema.py` — YAML DSL模型与校验
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/flow/engine.py` — 流程状态机、执行器、断点与注入控制
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/extensions/runtime.py` — Python扩展函数加载与沙箱执行
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/logging/store.py` — 统一日志写入与检索
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/logging/exporters/blf.py` — BLF导出实现
- `/Users/poera/Project/Poera/uds_mcp/src/uds_mcp/models/events.py` — CAN/UDS/流程事件统一数据模型

**Verification**
1. 启动验证：在本地通过 MCP 客户端调用 `can.send`、`uds.send` 并确认返回与日志入库一致。
2. 流程验证：加载示例 YAML，执行 `flow.start`，轮询 `flow.status`，确认可见步骤级事件与超时处理。
3. 断点验证：在指定步骤打断点，触发暂停后注入单独 UDS 报文并继续执行，检查 3E 保活不中断。
4. 注入验证：使用 Python 扩展函数动态生成请求字段，确认可重复、可审计、异常可追踪。
5. 导出验证：指定时间窗口导出 BLF，回放校验消息数量、时间戳范围、通道信息正确。
6. 兼容验证：Windows USB-CAN 真机跑通核心链路，Linux vcan 跑通自动化回归。

**Decisions**
- 已确认：`STDIO MCP` 优先，`单ECU单流程` 为 MVP。
- 已确认：MCP 实现采用官方 Python SDK：`mcp`（`FastMCP`）。
- 已确认：流程定义采用 `YAML + Python扩展` 双模式；断点控制通过 MCP 命令先落地。
- 已确认：日志导出首版优先 `BLF`。
- 已确认：通信基础库固定为 `py-uds`（UDS能力）+ `python-can`（CAN总线与设备抽象）。
- 已确认：与 Rust 侧 `rmcp` 保持协议语义与能力对齐，避免跨语言实现偏差。
- 范围包含：UDS/CAN收发、流程编排、异步状态查询、停止、断点、注入、流程共享、时间窗日志导出。
- 范围暂不包含：Web UI、多ECU并发调度、高级安全沙箱（仅做MVP级限制）。

**Further Considerations**
1. Windows USB-CAN 选型建议优先覆盖 `python-can` 已稳定支持的后端，并通过配置约束首批官方支持设备，减少首版验证矩阵。
2. Python扩展注入建议增加“签名校验 + 项目级白名单”，防止共享流程携带高风险代码。
3. 错误帧与CAN TP测试建议作为 Phase 6 独立能力包，复用当前事件模型与流程引擎，不侵入MVP主链路。
