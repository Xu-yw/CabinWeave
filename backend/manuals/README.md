# 车辆用户手册目录

用车顾问的 `search_knowledge_base` 检索这里的手册。后端启动时，知识库按目录同步（`mcp/knowledge_base.py` 的 `sync_manuals`）：新文件导入，内容变了的重新导入，删掉的文件从库里移除。

- 支持 `.md`、`.txt`、`.html` / `.htm`，可以放子目录；README.md 和隐藏文件不导入。
- 目录可用环境变量 `CABINWEAVE_MANUALS_DIR` 改；Docker 里以只读方式挂载到 `/app/manuals`。

## 内容来源

理想汽车的完整《用户手册》在车机里，官网没有公开版本。这里收录的是官网帮助中心按车型公开的页面，只取纯电车型（理想 i6、i8、MEGA）：

| 页面 | 理想 i6 | 理想 i8 | 理想 MEGA |
|---|---|---|---|
| 用车须知（质保、保养、账号、车控管理权、流量） | [5468](https://www.lixiang.com/help/support/5453/5468.html) | [5449](https://www.lixiang.com/help/support/5433/5449.html) | [5375](https://www.lixiang.com/help/support/5355/5375.html) |
| 道路救援须知 | [5467](https://www.lixiang.com/help/support/5453/5467.html) | [5448](https://www.lixiang.com/help/support/5433/5448.html) | [5374](https://www.lixiang.com/help/support/5355/5374.html) |
| 车辆售后指南 | [5466](https://www.lixiang.com/help/support/5453/5466.html) | [5447](https://www.lixiang.com/help/support/5433/5447.html) | [5373](https://www.lixiang.com/help/support/5355/5373.html) |
| 充电服务 | [5469](https://www.lixiang.com/help/support/5453/5469.html) | [5450](https://www.lixiang.com/help/support/5433/5450.html) | [5376](https://www.lixiang.com/help/support/5355/5376.html) |
| 家用充电桩 | [5465](https://www.lixiang.com/help/support/5453/5465.html) | [5446](https://www.lixiang.com/help/support/5433/5446.html) | [5372](https://www.lixiang.com/help/support/5355/5372.html) |

充电服务和家用充电桩三款车型内容相同，各合并成一份，共 11 个文件。每个文件开头注明来源地址和抓取日期，页面版权归理想汽车：

```
理想i6-用车须知.md      理想i6-道路救援须知.md      理想i6-车辆售后指南.md
理想i8-用车须知.md      理想i8-道路救援须知.md      理想i8-车辆售后指南.md
理想MEGA-用车须知.md    理想MEGA-道路救援须知.md    理想MEGA-车辆售后指南.md
理想纯电车型-充电服务.md  理想纯电车型-家用充电桩.md
```

这些页面讲质保、保养、救援、充电和售后，不含仪表指示灯、辅助驾驶、空调等操作说明；故障灯由故障灯表（`vehicle/warning_lights.py`）回答。

## 补充资料

把页面正文保存为 `.md` / `.txt`，或直接保存网页为 `.html` 放进本目录，重启后端即可导入。
