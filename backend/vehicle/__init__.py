"""
CabinWeave 模拟车辆

车辆服务接口的抽象边界：现在接进程内模拟存储，将来接真实车控服务时只替换这一层。
  - state.py          车辆状态、场景预设、按 user_id 隔离的进程内存储（一个用户一辆车）
  - safety.py         写操作的安全规则 S01–S07
  - service.py        车辆服务接口 VehicleService.execute：校验 → 安全规则 → 写状态 → 返回变化
  - simulator.py      模拟控制：局部修改状态、套用场景、重置（不经过安全规则，
                      供 /vehicle/* 接口和对话评测使用）
  - mock_services.py  模拟地图、天气、媒体、联系人（确定性数据，不访问网络）
  - warning_lights.py 故障灯表与查询（示例编码，通用说明）
"""
from vehicle.safety import SafetyDecision, SafetyPolicy
from vehicle.service import COMMANDS, UNSUPPORTED_REASON, VehicleService, make_command
from vehicle.state import (
    DEFAULT_STATE,
    SCENARIO_LABELS,
    SCENARIOS,
    VehicleStateStore,
    build_state,
    diff_states,
)

__all__ = [
    "COMMANDS",
    "DEFAULT_STATE",
    "SCENARIO_LABELS",
    "SCENARIOS",
    "UNSUPPORTED_REASON",
    "SafetyDecision",
    "SafetyPolicy",
    "VehicleService",
    "VehicleStateStore",
    "build_state",
    "diff_states",
    "make_command",
]
