# API 与配置

代码：
- `backend/api/main.py`：HTTP 接口与 CLI。
- `backend/vehicle/simulator.py`：车辆接口的逻辑。
- `.env.example`、`docker-compose.yml`。

单元测试：`backend/tests/test_vehicle_simulator.py`。

## 1. /chat

请求链路：快速通道 → 记忆读取 → `orchestrator.run(req, fast_path=False)` → 记忆写入 → 异步提炼驾驶员偏好。快速通道命中时，跳过记忆读取和偏好提炼（见 [快速通道](05-快速通道.md)）。

**请求**

| 字段 | 说明 |
|---|---|
| message | 语音识别后的文本 |
| user_id | 默认 anonymous，为空字符串时返回 400；一个用户一辆车，车辆按 user_id 定位 |
| conv_id | 为空时新建会话 |

**响应**（除回复、意图、角色、工具、置信度等基本字段外）

| 字段 | 说明 |
|---|---|
| route、fast_rule | 走了哪条通道（fast / slow），以及命中的快速通道规则 |
| sub_tasks、execution、assignments | 子任务、执行方式、各角色分到的子任务 |
| state_changes | 状态变化 `[{"field", "old", "new"}]` |
| blocked_actions | 被安全规则拒绝的操作 `[{"tool_name", "rule_id", "reason"}]` |
| vehicle_state | 执行后的完整车辆状态 |

## 2. 车辆接口

接口函数只负责转发，逻辑在 `vehicle/simulator.py` 的纯函数里，`ValueError` 一律转成 400。

这些接口直接读写进程内的车辆状态，**不经过安全规则**，用来模拟车速、电量、故障灯，或在演示和评测前把车重置到指定场景。驾驶员的车控指令仍然只走 `VehicleService.execute`。

| 接口 | 请求 | 响应 |
|---|---|---|
| `GET /vehicle/state` | `?user_id=` | `{state}` |
| `POST /vehicle/state` | `{user_id?, patch}` | `{state, changes}` |
| `GET /vehicle/scenarios` | — | `{default_scenario, scenarios: [{name, label, overrides}]}` |
| `POST /vehicle/scenario` | `{user_id?, scenario}` | `{scenario, state, changes}` |
| `POST /vehicle/reset` | `{user_id?}`，可省略 | 同上，场景为默认场景 |

- 车辆按 user_id 定位，不传 user_id 时操作 anonymous 的车。
- patch 按嵌套对象局部更新，例如 `{"speed_kmh": 110, "warning_lights": ["胎压报警"], "tire_pressure_kpa": {"fl": 180}}`。
- 出错时返回 400 和中文原因，状态保持不变：
  - user_id 为空字符串
  - 未知字段，如"未知的车辆状态字段: foo"
  - 类型不符或取值非法
  - 未知场景
- `changes` 的格式和工具返回的一致，field 是叶子字段的点分路径，列表整体算一个叶子。
- 6 个场景预设：parked、city_driving、highway、low_battery、tpms_warning、winter，默认场景由 `CABINWEAVE_VEHICLE_DEFAULT_SCENARIO` 决定。

## 3. CLI

`python api/main.py --cli` 的流程和 `/chat` 相同：
- 先试快速通道。
- 未命中再读记忆、走完整链路，并在后台提炼偏好。

每轮输出回复后，再输出所走通道；状态变化和被拒绝的操作只在有内容时输出：

```
CabinWeave [vehicle_control]: 好的，空调已调到22度。
  通道：快速通道 F02
  状态变化：climate.on false → true；climate.temp 24 → 22
```

CLI 进程和 HTTP 服务各有一份车辆状态，互不影响。

## 4. 配置

- **环境变量**：前缀统一为 `CABINWEAVE_`，如 `CABINWEAVE_SKILLS_DIR`、`CABINWEAVE_FAST_PATH`。
- **compose**：
  - 项目名 `cabinweave`，数据卷为 `cabinweave_redis-data`、`cabinweave_chromadb-data`、`cabinweave_backend-data`。
  - 端口默认 8000 和 5173，和其他服务冲突时在 `.env` 里改 `BACKEND_PORT`、`FRONTEND_PORT`。
  - 后端只读挂载 `backend/skills` 和 `backend/manuals`。
- **.env.example**：按代码实际读取的变量分组整理，每项一行中文说明和默认值。分组包括：
  - 模型凭据
  - 各角色模型覆盖
  - 意图识别
  - 汇总节点
  - 快速通道
  - 车辆默认场景
  - Skills
  - 车辆用户手册
  - 工具调用记录
  - 存储
  - 端口
  - 监控与日志

  可选项默认注释掉，因为部分配置留空值时会被当成空路径，或解析失败。
- **前端**：运行时配置变量为 `window.__CABINWEAVE_CONFIG__`，localStorage 键为 `cabinweave.frontend.settings`；界面见 [前端](09-前端.md)。

## 5. 已知局限

- 车辆状态存在进程内存里，后端重启后恢复为默认场景。
- `/vehicle/*` 没有鉴权，任何调用方都能指定 user_id 改任意用户的模拟车。
