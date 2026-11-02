# CabinWeave Skills

Skills 是可以热加载的处理规范。服务启动时从 `CABINWEAVE_SKILLS_DIR` 读取（默认 `backend/skills/`，容器内为 `/app/skills`，由 docker compose 只读挂载），Agent 调用模型时把匹配的 Skill 拼进 system prompt 的"[动态 Skills]"部分。适合放指令解析约定、播报口径、澄清话术、禁止事项这类需要经常调整、又不必改代码的规则。

## 当前的 4 份 Skill

| 文件 | 适用 Agent | 主要内容 |
|---|---|---|
| `vehicle_control/SKILL.md` | `vehicle_control` | 指令解析（相对调节默认幅度、默认主驾、模糊表达的默认处理）、安全规则 S01–S07 的播报口径、不支持功能如实说明、播报模板、禁止事项 |
| `navigation/SKILL.md` | `navigation` | 目的地澄清（家和公司从驾驶员偏好取）、多候选播报（最多 3 个）、充电站推荐口径（空闲桩、距离、功率）、地址隐私 |
| `vehicle_advisor/SKILL.md` | `advisor` | 故障灯分级处理（紧急、严重、注意）、不下最终诊断结论、续航估算口径、手册检索结果的引用方式 |
| `cabin_assistant/SKILL.md` | `cabin` | 影音和音量、模拟拨号的说法、天气播报、闲聊边界、行驶中不推荐分心内容、澄清话术 |

四份都是角色专属 Skill，不设关键词，对各自的 Agent 每次都注入。

## 文件格式

每份 Skill 一个目录，主文件命名为 `SKILL.md`，顶部是简单的 front matter：

```markdown
---
name: 车控处理规范
description: 车控模块的指令解析、安全规则播报口径、不支持功能的处理、播报模板和禁止事项
keywords:
agents: vehicle_control
enabled: true
---

# 车控处理规范

正文……
```

字段说明：

- `name`：Skill 名称，注入 prompt 时作为小标题；正文第一行如果是同名标题会被去掉，避免重复。
- `description`：一句话说明，方便在 `/skills` 接口里排查。
- `keywords`：触发关键词，英文或中文逗号分隔，消息里命中任意一个才注入；留空表示对适用的 Agent 始终注入。
- `agents`：适用的 Agent，可选 `vehicle_control`、`navigation`、`advisor`、`cabin`，多个用逗号分隔；留空表示所有 Agent。安全节点（`safety`）不调用模型，不使用 Skill。
- `enabled`：是否启用，`true` / `false`。

除了 `<目录>/SKILL.md`，也可以直接放单个 `.md`、`.txt` 或 `.json` 文件（JSON 用 `content` 或 `instructions` 字段放正文）；`README.md` 和以 `.` 开头的文件不会被加载。

## 编写约定

- **角色专属 Skill 不设关键词**：车载指令往往很短（"开窗""有点冷"），很难命中关键词，所以对该 Agent 始终注入。只有针对特定话题、不需要每次都注入的 Skill 才设关键词。
- **长度**：单份 Skill 正文超过 3200 个字符会被截断，一次请求注入的 Skill 合计不超过 `CABINWEAVE_SKILLS_MAX_PROMPT_CHARS`（默认 5000）。车控 Skill 控制在 1500 字左右，减少首字延迟；其他每份不超过 2500 字。重要规则放在前面。
- **与代码一致**：工具名、参数、安全规则编号（S01–S07）、故障灯分级都以代码为准，不写系统做不到的事；改了工具、安全规则或故障灯表，要同步修改对应的 Skill。
- **只写本角色的事**：每个 Agent 只能调用自己白名单里的工具，Skill 里不要让它调用别的角色的工具。
- **播报示例符合语音要求**：简短、口语化、先说结果，不念工具名、参数名和规则编号。
- Skill 与系统角色设定冲突时，以系统角色和安全边界为准。

## 热加载

修改 Skill 文件后不需要重启服务：

```bash
curl -X POST http://localhost:8000/skills/reload
```

查看加载结果和解析错误：

```bash
curl http://localhost:8000/skills
```
