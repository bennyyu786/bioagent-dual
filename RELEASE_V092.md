# Biomni Dual Center v0.9.2 · 白色界面、持久历史与实时事件

基于 v0.9.1 增量升级，沿用 `biomni_dual/` 主包；没有重建数据库或覆盖部署配置。

## 功能

- 白底黑字贯穿统一对话首页及 Task/DAG、Capability、Worker、日志等专业视图。
- 左侧「最近 · 历史会话」和「提交的任务」：真实 SQLite 工作区记录，按需分页加载更早历史，切换会话或跳转任务。
- 任务完成时统一对话调用 `present_result` 展示 JSON / Unicode 转义和有效 UTF-8 十六进制转义结果；保留 Worker 原始证据和 SHA256，不篡改存档。
- 实时事件与日志：默认所有事件、筛选任务/调度和 Worker；历史事件倒序分页；WebSocket 收到新事件后更新，断线自动重新连接；日志不再只显示 `worker_log`。
- 初始化 WebSocket 从最新事件 ID 继续，避免新浏览器反复重放全部历史。

## 新增接口

- `GET /api/v2/{workspace}/assistant/history?kind=sessions|tasks&offset=0&limit=80`
- `GET /api/v2/{workspace}/events/history?limit=100&before=<id>`

以上接口全部使用既有 Bearer 身份认证及工作区隔离，响应不暴露密钥。

## 升级

先备份项目与 `state/`；将源码合并到你实际运行的项目中，保留 `.env.service` 和本地 `team_server.py` 端口 3001 补丁；`pip install -e ".[server,test,agents]"` 后重启同一服务。浏览器强制刷新 `/control`。

## 验证

- `pytest -q`：382 passed（首轮完整回归）；最终小范围前端/会话修订后 17 项专项测试通过，第二次全量运行在约 75% 时遭执行超时中断
- `node --check`：前端 JS 语法检查通过
- 真实 Claude/Codex 及 Worker 在线执行尚未在目标服务器上验证。

## 边界

会话和任务数据库记录不会因为前端翻页而删除；对话窗口默认加载最近 200 条消息，旧消息仍然保存在 SQLite 数据库中。Worker 未主动上报详细日志时，事件页仍能显示任务与调度事件，但不会伪造 Worker 内部输出。
