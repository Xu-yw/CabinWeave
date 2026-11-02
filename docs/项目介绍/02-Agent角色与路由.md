# Agent 角色与路由

代码：`backend/agents/agent_orchestrator.py`（角色、路由、执行、汇总）、`backend/agents/tools.py`（工具定义）、`backend/tests/test_agent_orchestrator.py`（单元测试）。

## 1. 定位与接口

编排器接收一次请求，完成"意图识别 → 路由 → 执行 → 汇总"。API 层只读取记忆、构造请求，意图识别在编排器内部完成。

```python
result = await orchestrator.run(Request(message, user_id, conv_id, context=..., history=...))
```

`Request.intent_result` 为空时，`run()` 调用意图识别器；评测或测试可以直接传入识别结果。

## 2. 五个角色

| AgentType | 角色 | 职责 | 工具白名单（tool_scope） | 温度 / max_tokens |
|---|---|---|---|---|
| vehicle_control | 车控 | 解析控制对象、动作、参数和音区，调用控制工具，按工具结果播报 | set_climate、set_body、set_lighting、set_drive_mode | 0.0 / 400 |
| navigation | 导航 | 确认目的地或类别，检索地点或规划路线，多候选时播报前 3 个 | search_poi、plan_route、get_route_status | 0.1 / 500 |
| advisor | 用车顾问 | 区分车况、故障、知识类问题，按严重度给建议 | lookup_warning、estimate_range、search_knowledge_base | 0.1 / 700 |
| cabin | 座舱助手 | 影音、电话、天气、闲聊、追问澄清 | control_media、set_volume、make_call、get_weather、suggest_required_fields | 0.4 / 400 |
| safety | 安全节点 | 不调模型：紧急求助、道路救援 | — | — |

- 每个 LLM 角色有自己的 system prompt 和角色契约（职责、流程、输入、输出、可用工具）。
- 所有 LLM 角色统一追加播报要求：一般不超过 2 句、口语化、不用列表和 Markdown；执行结果以工具返回为准，被拒绝就说明原因；缺关键信息时只追问一个；只处理分给自己的任务；不声称执行了没有工具结果支撑的操作。
- 按角色配模型：`CABINWEAVE_{AGENT_TYPE 大写}_MODEL`，例如座舱助手配快模型、用车顾问配强模型。
- 思考模式：车控、导航、座舱助手关闭（请求经 `extra_body` 带 `thinking: disabled`，常量 `THINKING_DISABLED` 在 `core/llm_utils.py`），用车顾问保留。意图识别和汇总节点不受影响。手册检索的查询改写和重排同样关闭（见 [记忆、知识库与 Skills](06-记忆知识库Skills.md)）。

## 3. 工具白名单

- 编排器维护一个工具注册表（名字 → 工具定义），`register_tools()` 合并进注册表并下发给各 Agent。
- 每个 Agent 只能看到注册表里名字在自己 tool_scope 内的工具。所以 RAG（search_knowledge_base）只挂给用车顾问，车控即使模型想检索也拿不到这个工具。
- 模型调用了不在白名单里的工具时，不执行，只把错误回传给模型。工具参数按 JSON Schema 校验（必填、类型、不允许多余字段）。
- 编排器构造时注册全部 15 个工具，各工具的参数和返回见 [工具](04-工具.md)。

## 4. 路由规则

意图到角色的映射写在 `INTENT_AGENT`（14 个意图全覆盖，见 [意图识别](01-意图识别.md) 第 2 节的表）。路由由纯函数 `decide_route(intent_result, message)` 完成，只看识别结果，不区分来自 LLM 还是关键词兜底：

1. **安全优先**：主意图或任一子任务是安全类 → 只派给安全节点，非安全类子任务放进 `deferred_tasks`，在回复里说明暂缓。
2. **按映射分配**：没有子任务时，整句作为主意图的唯一任务；有子任务时，按 `INTENT_AGENT` 分到各角色，各自处理分到的部分。同一角色的多个子任务合并成一次派发，由模型在一轮里发出多个工具调用。派发顺序按子任务首次出现的顺序。
3. **网络兜底**：LLM 失败或超时、且关键词层也没有候选时，不调用任何模型，直接回复"网络不太好，请稍后再试。"

每个角色只有一个实例。

例：

| 输入 | 派发 | 执行方式 |
|---|---|---|
| 空调调到二十二度，再放首周杰伦的歌 | 车控（调温）、座舱助手（放歌） | 并行 |
| 打开车窗，再把空调打开 | 车控（两个子任务合并派发） | — |
| 把音乐关了，右前轮爆胎了帮我叫救援 | 安全节点（救援），关音乐暂缓 | — |
| 看看电量够不够，够的话就导航去公司 | 用车顾问（查电量）、导航 | 串行 |

## 5. 角色输入包

每个 Agent 拿到一份复制出来的请求。交给模型的指令只放分给它的原话片段（多个片段用"；"连接），完整原话和其他信息放在角色输入包里作参考，避免回应别的 Agent 负责的部分。角色输入包结构：

```json
{
  "agent_type": "vehicle_control",
  "assigned_tasks": [{"intent": "climate_control", "text": "空调调到22度"}],
  "full_utterance": "空调调到22度，再放首周杰伦的歌",
  "execution_mode": "parallel",
  "previous_results": [],
  "vehicle_snapshot": {"speed_kmh": 0, "gear": "P"},
  "driver_preferences": {"preferences": {"climate_temp": 22}}
}
```

`driver_preferences` 只在驾驶员偏好非空时出现，只含 preferences 和 notes，家和公司地址由工具从 `Request.profile` 读取（见 [记忆、知识库与 Skills](06-记忆知识库Skills.md)）。

`vehicle_snapshot` 从车辆状态里按角色裁剪顶层键（`SNAPSHOT_KEYS`），省掉"先调工具读状态"的一轮模型调用：

| 角色 | 快照键 |
|---|---|
| 车控 | speed_kmh、gear、drive_mode、climate、seats、body、lights |
| 导航 | location、navigation、battery_pct、range_km |
| 用车顾问 | speed_kmh、gear、drive_mode、battery_pct、range_km、tire_pressure_kpa、warning_lights、next_service_km、outside_temp_c、climate、seats、body、lights、media |
| 座舱助手 | media、speed_kmh |

快照默认从车辆服务取当前用户那辆车的状态（见 [模拟车辆](03-模拟车辆.md) 第 6 节）；调用方显式传入 vehicle_state 时原样使用。

## 6. 执行

- **parallel**：各 Agent 同时执行（`asyncio.gather`），结果按分配顺序返回。
- **sequential**：按分配顺序逐个执行，前面 Agent 的回复和工具返回值（`{"agent_type", "content", "tool_results"}`）追加进后一个的 `previous_results`。上一步失败时也会传下去，下游能知道没成功。
- 工具调用记录放在每次 `handle` 的局部变量里，同一个 Agent 被多个请求并发调用时互不覆盖。
- **失败**：Agent 失败（模型调用异常、超时、超过工具轮数）时不转交其他角色，直接返回按分到的意图生成的失败短句（`failure_reply`），不调模型；失败前已执行的工具调用和状态变化保留在记录里。并行时某个 Agent 抛出异常，同样换成它的失败短句，不会从结果里丢掉。

| 分到的意图 | 失败短句 |
|---|---|
| 执行类：空调、车身、驾驶设置、导航、搜地点、影音、电话 | "{动作}失败，请手动操作或稍后重试。"，动作名依次为调空调、车身操作、驾驶设置、导航、搜索地点、影音操作、打电话 |
| 查询类：车况、故障、手册 | "{动作}失败，请稍后重试。"，动作名为查询车况、查询故障、查询手册 |
| 闲聊、other | "抱歉，刚才没处理好，请再说一次。" |

同一 Agent 分到多个意图时，动作名用"、"连接，如"调空调、车身操作失败，请手动操作或稍后重试。"

## 7. 安全节点

不调模型，按分到的第一个安全类意图生成单号（`SOS-` / `RSA-` 加 request_id）并播报固定话术：

| 意图 | 播报要点 |
|---|---|
| emergency_sos | 已发起紧急求助、单号；已打开双闪，停车时并解锁车门；确认人员安全，受伤拨 120，事故拨 122 |
| roadside_assist | 已生成救援单、单号；已打开双闪；停到安全位置、人员撤到护栏外、放警示牌 |

有暂缓的指令时，末尾加"其他指令先暂缓，处理完再说。"。单据（单号、类型、意图、request_id、原话）放在结果的 `ticket` 字段。开双闪、解锁车门经车辆服务执行，同样过安全规则，播报按实际结果说（见 [模拟车辆](03-模拟车辆.md) 第 6 节）。

## 8. 汇总分级

只有一个 Agent 时直接返回它的回复，多个 Agent 才进汇总节点：

| 情况 | 处理 |
|---|---|
| 参与汇总的子任务全是执行类（climate_control、body_control、drive_setting、media_control、phone_call） | 模板拼接，不调模型：第一条原样保留，后面每条去掉开头的"好的"再接上 |
| 含问答类结果 | 调模型合并：不超过 3 句、口语化、不提 Agent；执行结果以工具为准，被拒绝的操作不能说成已完成；安全信息放最前 |
| 模型调用失败 | 按顺序直接拼接，缺句末标点时补句号 |
| 有 Agent 失败 | 成功的部分按上面处理，失败短句按顺序接在后面，不交给模型改写；全部失败时只播失败短句，相同的只播一次。例如"好的，空调已调到22度。影音操作失败，请手动操作或稍后重试。" |

## 9. 输出结构

**OrchestratorResult** 主要字段：

| 字段 | 说明 |
|---|---|
| response | 最终播报文本 |
| agent_type、agent_types | 第一个派发的角色（单角色时就是回复的角色）、参与处理的全部角色（按子任务顺序） |
| intent、intent_result | 主意图和完整识别结果 |
| sub_tasks、execution、assignments、deferred_tasks | 子任务、执行方式、各角色分到的任务、暂缓的任务 |
| tools_used、tool_traces | 用到的工具和每次调用的记录 |
| escalated、ticket | 是否由安全节点处理、生成的单据 |
| routing_reason | 一行路由说明，如 `intent=climate_control, agents=vehicle_control+cabin, execution=parallel` |

`/chat` 响应里的 `sub_tasks`、`execution`、`assignments` 来自这里；置信度等级和 sources 从 `intent_result` 取。

## 10. 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| CABINWEAVE_{AGENT_TYPE 大写}_MODEL | 空（沿用全局模型） | 按角色覆盖模型，安全节点不读 |
| CABINWEAVE_COMPOSER_MAX_TOKENS | 400 | 汇总节点 max_tokens |
| CABINWEAVE_COMPOSER_TEMPERATURE | 0.1 | 汇总节点温度 |
| CABINWEAVE_TOOL_TRACE_MAX | 200 | 内存中保留的工具调用记录条数 |

## 11. 已知局限

- 模型回复有时超过 2 句，播报偏长。
- 串行时把上游的完整工具返回值传给下游，检索结果较多时输入包会变长。
