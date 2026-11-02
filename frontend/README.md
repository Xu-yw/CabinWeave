# CabinWeave Frontend

Vue 3 + Vite 实现的车机风格界面，分座舱、评测、系统三个页面，回复只用文字。设计说明见 [项目介绍 09-前端](../docs/项目介绍/09-前端.md)。

```bash
npm ci
npm run dev
npm run build
```

- 开发服务默认监听本机 5173，把 `/api/python` 代理到 `http://127.0.0.1:8000`；容器里由 Nginx 把同一路径代理到 `backend:8000`。
- 后端地址取 `public/runtime-config.js` 里的 `window.__CABINWEAVE_CONFIG__.pythonApiUrl`，只放公开的连接配置。
- 浏览器存储只保存用户 ID、会话 ID 和链路追踪栏的展开状态，刷新后不恢复对话气泡。
