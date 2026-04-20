# Step / Hook 设计评审

> 评审范围：`uds_mcp/flow/schema.py`、`uds_mcp/flow/engine.py`
> 日期：2026-04-20
> 目的：评估 Step 与 Hook 设计的合理性、易用性与学习曲线，列出修改方向（不考虑向后兼容）

---

## 整体评价

整体设计**功能强、覆盖全**，但**学习曲线偏陡**——主要源于：
1. 概念过载（Step 类型多 + Hook 类型多 + 各种 inherit/override 三态）
2. Hook 多种返回字段共存（同一目的有多种写法）
3. Hook context/return 是裸 dict，缺乏类型提示

---

## 一、Step 设计

### 优点
- 用 `kind` 做 discriminated union（uds/transfer/subflow/can/wait），扩展性好
- `_BaseStep` 把通用字段（repeat/timeout/delay/breakpoint/tester_present/addressing_mode）统一抽出
- `tester_present` / `addressing_mode` 用 `inherit/off/physical/functional` 字面量表达三态语义，比 `Optional[bool]` 清晰

### 问题

#### 1. Step 种类切分粒度不一致
- `WaitStep` 几乎只是 `delay_ms`，但所有 step 已经有 `delay_ms` 字段——`WaitStep` 是冗余抽象
- `CanStep` 复用 `can_tx_hook`——hook 既是"伴随 UDS 的副作用通道"，又是"独立 step 的核心动作"，语义重叠

**改进方向**
- 删 `WaitStep`，让 `delay_ms` 单独成立
- `CanStep` 用专门的 `can_frames`（静态）+ `can_frames_hook`（动态），不要复用 `can_tx_hook`

#### 2. TransferStep 与 UdsStep 字段大量重复
`skipped_response` / `expect` / 4 个 hook 字段两边都写一遍。

**改进方向**：提一个 `_RequestStep` 中间基类承载这些字段，schema 更紧凑、文档更短。

#### 3. `expect.apply_each_response` 与 `transfer_data.check_each_response` 语义割裂
- `apply_each_response` 控制 assertions 何时跑
- `check_each_response` 控制 response matcher 何时跑
- 同一类问题两种表达，用户必须分别记忆

**改进方向**：合并为单字段 `expect.scope: "each" | "last"`（默认 last）。

#### 4. `tester_present` 真值表过于复杂
`_resolve_tester_present_mode` 是 4×3 组合（step 4 种 × flow policy 3 种），用户经常分不清"step.tester_present='inherit' 时是否会跑 TP"。

**改进方向**
- step 级简化为 `tester_present: bool | None`（None=继承），addressing 单独从 `addressing_mode` 推
- 或者文档放显式真值表

#### 5. Step name 没有唯一性校验
`patch_step` / `set_breakpoint` 都是首个匹配，重名 step 会静默踩坑。

**改进方向**：在 `FlowDefinition` 校验器中检查 name 唯一性。

---

## 二、Hook 设计

### 优点
- Pydantic 强类型返回模型（`BeforeHookReturn` 等），错误能尽早抛
- 4 个钩子点（before/message/can_tx/after）+ `segments_hook` 覆盖了绝大多数场景
- 提供 `inline` vs `script+function` 两种写法

### 问题（学习曲线最陡的部分）

#### 1. Hook 种类过多 + 触发时机难记
当前命名：
- `before_hook`：step 前一次
- `message_hook`：每条 request 前
- `can_tx_hook`：每条 request 后或独立 step
- `after_hook`：step 末

用户最常问："before vs message 有啥区别？"——答案是"是否在 request 序列展开后逐条触发"，但名字完全没体现频率。

**改进方向**：改名以体现触发频率
| 旧名 | 新名 |
|---|---|
| `before_hook` | `on_step_start` |
| `message_hook` | `on_each_request` |
| `can_tx_hook` | `on_each_response_can_tx` |
| `after_hook` | `on_step_end` |

#### 2. Before/Message Hook 返回字段三选一过于灵活
返回值有三种互斥写法：`request_hex` / `request_sequence_hex` / `request_items`，`_resolve_request_dispatch_items` 要按优先级判。

并且 schema 没禁止同时存在多个，用户写错时不会报错——只是某些字段被静默忽略。

**改进方向**：只保留 `request_items`（单条情况就是长度 1 的 list），加一个 helper：
```python
def make_request(hex_str, *, skipped=False) -> RequestItem: ...
```

#### 3. Hook context 是裸 dict
- `context["variables"]` 是可变 dict
- `context["trace"]` 是只读 tuple
- `context["assertions"]` 是富对象
三种风格混杂。用户得记住哪些字段可写、哪些只读、哪些方法能调，IDE 也无法补全。

**改进方向**：包一个 `HookContext` dataclass，字段类型清晰，IDE 能补全。返回值也用 builder 模式：
```python
def my_hook(ctx: HookContext) -> HookResult:
    return ctx.result().with_request("22F190").set_variable("did", "F190")
```

#### 4. `assertions` 对象 API 冗长且双轨
- 通用：`byte_eq(hex_value=..., index=..., value=...)`
- 简版：`response_byte_eq(index, value)`
两套 API 学起来重复。`bytes_int_range` 名字晦涩。

**改进方向**：只留简版（`response_byte_eq` / `response_uint`），通用版让用户用 Python 自己写后调 `assert_true(condition, message=...)`。

#### 5. Inline 与 script+function 混合定义
`HookConfig._validate_source` 让用户摸索半天（互斥规则三条）。

**改进方向**：改 discriminated union：
```python
class InlineHook: type: Literal["inline"]; code: str
class ScriptHook: type: Literal["script"]; script: str; function: str
HookConfig = InlineHook | ScriptHook
```

#### 6. Hook 返回 None vs `{}` 都合法
`_run_hook` 里 `updates = {}` 默认，用户分不清"我返回 None 是不是没生效"。

**改进方向**：明确返回值必须是 dict 或 `HookResult`，否则报错。或允许 None 但文档明示语义=不修改。

#### 7. `segments_hook` 不允许 assertions
行为靠运行时报错（`"segments_hook assertions are not supported"`），违反"hooks 都有 assertions"的直觉。

**改进方向**：要么允许、要么把 `segments_hook` 单独建模为非"hook"概念（如 `transfer_data.segments_provider`）。

---

## 三、改进优先级

| 优先级 | 改动 | 价值 |
|---|---|---|
| 高 | Hook 改名 + 用 `HookContext` 类 + 返回值 builder | 学习曲线陡降 |
| 高 | 删 `WaitStep`、合并 `expect.scope`、合并 `request_items` 三选一 | 减少认知负担 |
| 中 | 提 `_RequestStep` 基类，TransferStep/UdsStep 共享字段声明 | schema 文档变短 |
| 中 | `tester_present` 三态简化 + 真值表文档 | 减少踩坑 |
| 中 | `HookConfig` 改 discriminated union | schema 校验更清晰 |
| 低 | step name 唯一性校验 | 防误用 |
| 低 | assertion API 收敛到一套 | 减重复 |
| 低 | `segments_hook` 独立建模或允许 assertions | 一致性 |

---

## 四、学习曲线判断

- **入门**（写一个简单 UDS step）：平缓，YAML 看起来直观
- **进阶**（加 expect + 1 个 hook）：中等，主要卡在 hook 选哪个、返回什么
- **高阶**（transfer + 多 hook + tester_present 控制）：陡，需要同时掌握：
  - 4 个 hook 触发时机
  - TP 三态真值表
  - `apply_each_response` 与 `check_each_response` 的区别
  - 三种返回字段的优先级

---

## 五、核心改进总结

> **减字段、改命名贴近触发时机、把 hook context/return 从 dict 升级为有类型的对象**

如果只能做一件事，建议先做 **Hook 改名 + HookContext 类型化**——这是用户感知最强的痛点。
