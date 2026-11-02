# 记忆、知识库与 Skills

代码：
- `backend/memory/conversation_memory.py`：驾驶员偏好的提炼、规整、合并。
- `backend/mcp/knowledge_base.py`：手册同步、切块、检索。
- `backend/manuals/`：本地手册目录，包含 README.md 和抓取脚本 `fetch_manuals.py`。
- `backend/skills/`：4 份角色专属 Skill。
- `backend/agents/agent_orchestrator.py`、`backend/agents/tools.py`、`backend/api/main.py`：接入。

单元测试：`backend/tests/test_driver_memory.py`、`test_knowledge_base.py`、`test_skills.py`。

## 1. 驾驶员偏好

记忆分三级：
- 工作记忆在 Redis。
- 情景记忆在 ChromaDB。
- 第三级是驾驶员偏好。

### 1.1 格式

```json
{
  "preferences": {"climate_temp": 22, "seat_heat": "冬天常开", "music": ["轻音乐"], "drive_mode": "comfort"},
  "places": {"home": "…", "company": "…"},
  "notes": ["…"]
}
```

### 1.2 提炼

完整链路每轮结束后，`update_profile` 异步调模型，从最近 10 条对话里提炼偏好。prompt 由 `build_profile_prompt` 生成，规则如下：

- 只记明确表达或反复出现的稳定偏好，一次性指令不记。"我一般开22度"记为 climate_temp=22，"这次调到26度"不记。
- places 只在驾驶员明确说出家或公司的地址时记，不猜。
- 不记电话号码、证件号、银行卡号、车牌号。
- 已有偏好里没被新对话推翻的保留；助手说的话不算偏好。
- prompt 里只给空的 JSON 骨架，不给示例值，避免模型把示例值照抄成偏好。

模型输出先规整，再和已有偏好合并；没有变化时不写库。快速通道命中时不提炼。

### 1.3 规整与合并

`normalize_driver_profile` 在存库前和读出后各执行一次：

| 字段 | 规则 |
|---|---|
| preferences | 只接受对象。climate_temp 须是 16–32 的数字，drive_mode 须是 eco / comfort / sport，music 是字符串列表，seat_heat 是字符串，不合格的丢掉；其他键只留标量或字符串列表 |
| places | 只留 home、company，值为非空字符串 |
| notes | 字符串列表，去重保序，最多 10 条 |
| 敏感信息 | 含连续 11 位以上数字的字符串一律丢掉 |

`merge_driver_profile` 合并新旧偏好：preferences 和 places 按键合并，新值覆盖旧值，没提到的保留；notes 有新的就整体替换。合并放在代码里做，所以模型漏输出某个字段时，原有偏好也不会丢。

### 1.4 使用

| 去处 | 内容 | 用途 |
|---|---|---|
| `Request.profile` | 规整后的完整偏好，含 places | 工具读取，比如 plan_route 按偏好里的地址解析"回家""去公司" |
| 角色输入包 `driver_preferences` | 只含 preferences 和 notes，偏好非空时才有 | Agent 处理"按我习惯的温度""放点我喜欢的音乐" |
| 上下文 `[驾驶员偏好]` | 只含 preferences 和 notes，中文可读 | 背景信息 |

家和公司的具体地址只有工具能读到，prompt、导航状态和播报里都只出现"家""公司"。`/chat` 和 CLI 走完整链路时，都会把偏好传进 `Request.profile`。

车控遇到"按我习惯的温度""老样子"时参考 driver_preferences，没有相关偏好就追问具体数值。

## 2. 车辆用户手册知识库

### 2.1 内容来源

理想汽车的完整《用户手册》在车机里，官网没有公开版本。知识库用的是官网帮助中心按车型公开的页面，只取纯电车型 i6、i8、MEGA：

| 页面 | 内容 | 文件 |
|---|---|---|
| 用车须知 | 质保、保养、账号实名、车控管理权、流量服务 | 每款车型各一份 |
| 道路救援须知 | 服务范围、服务方式、费用、例外情形 | 每款车型各一份 |
| 车辆售后指南 | 售后门店分布、服务保障 | 每款车型各一份 |
| 充电服务 | 充电服务常见问题 | 三款车型内容相同，合并为"理想纯电车型-充电服务" |
| 家用充电桩 | 申请安装流程 | 三款车型内容相同，合并为"理想纯电车型-家用充电桩" |

共 11 个文件，切成约 90 个片段。这些页面不含仪表指示灯、辅助驾驶、空调等操作说明，故障灯由故障灯表回答（见 [工具](04-工具.md) 第 6 节）。

页面版权归理想汽车，抓取结果只放在本地：
- `backend/manuals/` 下只有 README.md 和 `fetch_manuals.py` 进 git，其余文件都被 `.gitignore` 忽略。
- `.dockerignore` 也排除了这个目录，Docker 里以只读方式挂载到 `/app/manuals`。
- 目录位置可用 `CABINWEAVE_MANUALS_DIR` 覆盖。

### 2.2 抓取

`python manuals/fetch_manuals.py` 只用标准库，流程如下：
1. 抓取上述 15 个页面，只取正文区域 `div#article`。
2. h1–h4 转成二级标题，丢掉图片和脚本。
3. 三款车型正文相同（车型名替换后一致）的页面合并成一份。
4. 每个文件开头注明来源地址、抓取日期和版权说明。

### 2.3 同步

后端启动时，KnowledgeBase 调用 `sync_manuals(dir)`，让知识库和目录内容保持一致：

| 情况 | 处理 |
|---|---|
| 新文件 | 切块导入 |
| 同名文件、内容哈希相同且片段齐全 | 跳过 |
| 内容哈希变了，或片段不齐 | 删掉旧片段后重新导入 |
| 目录里已经没有的文件 | 删掉它的片段 |
| 目录不存在或为空 | 不导入，知识库可以为空 |

- 支持 `.md`、`.txt`、`.html` / `.htm`，递归扫描，跳过 README.md 和隐藏文件。
- 文本依次按 UTF-8、GB18030 解码。
- HTML 用标准库 html.parser 提取正文：去掉 script、style、nav、header、footer 等，h1–h4 转成 markdown 标题；页面里有 main 或 article 时只取其中的内容。
- 只动 `kind="manual"` 的片段；经 `/knowledge/add`、`/knowledge/upload` 上传的文档标记为 `kind="upload"`，不受同步影响。`/knowledge/upload` 也支持 HTML。
- 单个文件出错只计入 failed，不影响其他文件。同步结果记日志：新增、更新、跳过、删除、失败的文件数和片段总数。

### 2.4 切块

`chunk_document` 由手册和上传文档共用：
1. 按 markdown 标题分节，节内按空行分段。
2. 同一节的段落依次合并，每块（含前缀）不超过 500 字；超长段落按句末标点切开。
3. 每块前面加"【文件名｜最近两级标题】"前缀，如"【理想i8-用车须知｜理想i8 用车须知 / 2.车辆保养】"。标题和小节名也参与向量化，能提高命中率。

### 2.5 检索

用车顾问调用 `search_knowledge_base`，经 tool_manager 做查询改写（3 个子查询）、并行召回、去重和重排。改写和重排的模型调用关闭思考模式，实测一次手册问答约 4–6 秒：
- 检索为空时返回 `found: false` 和"手册里没有找到相关内容，建议查看车机中的用户手册或联系售后"。
- 知识库不可用时，降级结果提示"请以车辆用户手册为准"。
- 用车顾问 Skill 规定：按片段前缀说明出处，内容提炼成一两句；手册里没有的如实说没查到，不凭常识编造数值、步骤或按钮位置。

ChromaDB 默认的 embedding 模型 all-MiniLM-L6-v2 以英文语料为主，中文语义检索效果一般。片段前缀、查询改写和重排能弥补一部分。

## 3. Skills

共 4 份角色专属 Skill：

| 文件 | 角色 | 字数（中文） | 要点 |
|---|---|---|---|
| skills/vehicle_control/SKILL.md | vehicle_control | 约 1470 | 相对调节默认幅度（温度 1 度、风量 1 档、座椅加热 1 档）；不说位置默认主驾；"有点冷""玻璃起雾"等模糊表达的默认处理；安全规则 S01–S07 的播报口径；不支持功能也要调用工具确认；播报模板；禁止事项 |
| skills/navigation/SKILL.md | navigation | 约 790 | 目的地澄清；家和公司从偏好取，缺地址时追问；多候选最多播报 3 个；充电站按空闲桩、距离、功率推荐；播报只说"家""公司" |
| skills/vehicle_advisor/SKILL.md | advisor | 约 1020 | 问题分类；故障灯按紧急 / 严重 / 注意分级，紧急和严重先提醒安全停车；不下最终诊断结论；续航估算口径；手册引用方式 |
| skills/cabin_assistant/SKILL.md | cabin | 约 760 | 音量默认 ±3；电话是模拟拨号；不能发消息；天气播报；闲聊边界；不支持转人工；行驶中不推荐分心内容；澄清话术 |

- 不设关键词（`keywords` 为空），对对应角色始终注入。"开窗"这类短指令很难命中关键词。
- `agents` 可选值为 vehicle_control、navigation、advisor、cabin。安全节点不调模型，不用 Skill。
- 车控 Skill 控制在 1500 字左右，减少首字延迟；4 份都在单个 Skill 3200 字的截断上限以内。
- 内容以代码为准：工具名、参数、安全规则编号、故障灯分级都和实现一致。
- 支持热加载，调用 `POST /skills/reload` 即可。

## 4. 已知局限

- 知识库内容只有官网公开的服务类页面，问到具体功能怎么操作时，通常只能回答"手册里没查到"。
- 偏好合并后，模型不再输出某个键并不会删掉这条偏好，只能被新值覆盖。
- 偏好在背景信息和角色输入包里各出现一次，有少量 token 重复。
- 提炼偏好时会把已有的家和公司地址发给模型，这是合并所需。
