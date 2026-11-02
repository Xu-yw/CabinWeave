# CabinWeave 后端

FastAPI 后端：意图识别与多意图拆分、5 个角色的路由与执行、模拟车辆与安全规则、快慢双通道、对话记忆与驾驶员偏好、手册知识库、Skills、监控和离线评测。整体说明见 [仓库说明](../README.md)。

## 目录

| 目录 | 内容 |
| --- | --- |
| api/main.py | HTTP 接口与 CLI（`python api/main.py --cli`） |
| agents | 角色定义、路由、多意图执行、汇总节点（agent_orchestrator.py）与 Agent 工具（tools.py） |
| core | 意图识别（intent_recognizer.py）、快速通道（fast_path.py）、Skills 加载 |
| vehicle | 车辆状态与场景（state.py）、安全规则（safety.py）、车辆服务（service.py）、模拟控制（simulator.py）、模拟外部服务、故障灯表 |
| memory | Redis 工作记忆、ChromaDB 情景记忆与驾驶员偏好 |
| mcp | 手册知识库（knowledge_base.py）与工具执行管理 |
| monitor | 运行指标、告警与快慢通道统计 |
| manuals | 车辆用户手册目录（只提交 README 和抓取脚本） |
| skills | 4 份角色专属 Skill |
| evaluation | 意图识别评测（样本、评测脚本、正式报告） |
| tests | 单元测试（假客户端，不调用真实模型） |

## 主要接口

- `POST /chat`：对话；一个用户一辆车，按 `user_id` 定位，响应含 `route`、`fast_rule`、`sub_tasks`、`state_changes`、`blocked_actions`、`vehicle_state`
- `GET/POST /vehicle/state`、`GET /vehicle/scenarios`、`POST /vehicle/scenario`、`POST /vehicle/reset`：查看和模拟车辆状态（不经过安全规则）
- `GET /profile`：驾驶员偏好；`GET /eval/report`：意图评测的正式报告
- 其他：`/health`、`/skills`、`/skills/reload`、`/monitor`、`/metrics`、`/trace/tools`、`/search`、`/knowledge/*`

完整参数以 `/docs` 为准。

## 运行与测试

仓库根目录运行 `docker compose up -d --build`，后端监听本机 8000。单独运行（Python 3.12，需要可访问的 Redis）：

```bash
cd backend
python -m pip install -r requirements-dev.txt
python -m uvicorn api.main:app --port 8000
python api/main.py --cli
python -m pytest -q
```

## 配置

环境变量统一使用 `CABINWEAVE_` 前缀，完整列表和默认值见根目录 [.env.example](../.env.example)。真实模型凭据只放在根目录的 `.env`，不得提交。
