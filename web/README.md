# KinArc Demo Web-UI

## 介绍

基于 Vue 3、TypeScript 和 Vite 的 Agent 对话与配置平台，对接本仓库的 Runtime API。
支持历史会话、流式回复、工具调用、任务清单和多模态附件，以及 Agent、Skill、知识库、
评测和自进化任务管理。

## 快速启动

需要 Node.js 22 和 npm 10，版本要求见 [.nvmrc](.nvmrc) 和 [package.json](package.json)。
先按[项目 README](../README.md)准备依赖服务和后端配置，在仓库根目录启动后端：

```bash
uv run agentsystem
```

另开终端，从仓库根目录启动前端：

```bash
cd web
npm ci
npm run dev
```

访问 `http://127.0.0.1:10061`。开发服务器默认将 `/v1` 请求代理到
`http://127.0.0.1:10060`。

需要调整地址时，将 [.env.example](.env.example) 复制为 `.env.local`：
`DEV_PROXY_TARGET` 设置开发代理目标，`VITE_API_BASE_URL` 设置浏览器请求的 API 地址。
模型密钥和服务凭证只配置在后端，不写入前端环境变量。

## 代码结构

```text
src/
├── main.ts         # 应用启动
├── App.vue         # 应用入口与导航
├── router.ts       # 页面路由
├── config.ts       # 前端运行配置
├── api/            # HTTP 请求与接口映射
├── chat/           # 对话事件投影与展示数据
├── components/     # 对话组件与交互
├── composables/    # 会话、附件等状态与生命周期
├── domain/         # 领域类型
├── pages/          # 对话页面
├── platform/       # 配置与任务管理页面
├── sse/            # 流式事件解析
├── storage/        # 浏览器检查点存储
├── styles.css      # 样式入口
└── styles/         # 主题、组件与响应式样式
```

测试文件与对应模块放在一起，命名为 `*.test.ts`。

## 验证

在 `web/` 目录运行：

```bash
npm run check
```

该命令依次执行 Prettier 格式检查、Vitest 测试、TypeScript 类型检查和 Vite 构建。
也可分别运行 `npm run format:check`、`npm test` 和 `npm run build`。
