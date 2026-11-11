# v0.8.7 统一任务结果显示 / Result Presentation

修复 v0.8.6 的结果审核原始字符串显示问题，以及验收后无法在 Web 找到历史结果的问题。

## UI 入口

- **结果审核**：点击待审核工单 → 阅读模式默认显示可读文字，可切换 **原始证据** 以审查 Worker 原始返回值。支持一键复制当前视图；审核仍基于原始结果 SHA256。
- **已完成结果**：新增左侧菜单，列出当前工作区可见的、已验收完成的任务。可查看和复制完整结果，或切换原始记录。
- **任务 DAG**：选择 completed 节点，点击 **查看最终结果**，自动跳转到相同结果视图。

## 解码约定（只在展示时转换）

- 正常 Unicode 文本不改变；兼容 JSON string、JSON 对象 `answer/result/output/final/text/content/message/stdout/response` 包装和文字 content block 数组。
- 将合法 `\\uXXXX` 形式的转义序列显示为中文等 Unicode 字符；保留无效转义原文。
- 只做有限层数 JSON 解包，格式未知时展示原文或 JSON；不会执行 HTML/JavaScript，前端用 `textContent` 安全显示。
- **严禁回写展示结果到任务库和审核证据**。原始 `result`、`sha256` 和 Worker 验证逻辑不变。
- 文本“能读”不是“正确”，L1/L2/L3 审核策略不发生变化。

## API

- 旧接口 `GET /api/v2/{ws}/dispatch/{dispatch_id}/evidence` 继续保留 `result` 原始字符串、`sha256` 等字段，新增 `presentation: {text, format, transformed}`。仅管理员可访问。
- 新增 `GET /api/v2/{ws}/results`：工作区可见的已完成任务元数据列表。
- 新增 `GET /api/v2/{ws}/tasks/{task_id}/result`：已完成任务的原始 `result` 和 `presentation`。任务不在授权工作区内为 404，尚未验收为 409；按现有成员/任务可见性规则授权。

## 升级

1. 备份旧版 `state/` 目录（含所有 SQLite），不要覆盖数据库。
2. 用新工程代码替换旧程序，使用**原有 state/ 目录和启动参数**重新启动 FastAPI。
3. 浏览器硬刷新（Mac: Cmd+Shift+R）以加载新版 HTML。
4. 打开“结果审核”检查历史未验收工单；验收后到“已完成结果”或 DAG 查看完整回答。
5. 如使用 Docker Compose，重新构建并启动 `control` 容器，同时继续挂载原数据卷。

本更新不是独立的前端渲染代理，也没有进行真实远程 Claude/Codex/GPU Worker 联调。
