# v0.8.9 自动完成与网页结果呈现补丁

Worker 成功 report 后，后端自动核验工作区、运行中任务、Worker 已持有的租约、report SHA256 和非空有效文本；然后 TaskCenter 标记 completed、资源账本释放配额、WebSocket 发出 task_completed 事件。网页已有 DAG 完成结果组件，会自动通过 `/api/v2/{ws}/tasks/{task_id}/result` 读取并展示回答。

Worker 明确失败、空结果或状态占位符会标记 failed，不会伪造答案。过期租约、并发状态冲突、跨库不一致隔离为 uncertain，不自动重放副作用。

此处的 completed 表示执行交付完成，不代表医学、科研结果或事实正确性已经独立验证。执行前审批和 Worker 鉴权保持不变。

**兼容限制：** 已经处于 `review_required` 的旧历史记录没有自动迁移，避免在未确认原始执行上下文时改变历史任务。既有 legacy `/review` 接口保留作为旧任务清理与兼容操作，前端不再显示人工审核导航。

需要真实 Worker 完成一次端到端执行验证；不能仅凭 `/control` 返回 HTTP 200 判断完成。

## 启用与升级

默认 `create_app(..., auto_delivery=True)` 自动完成；已有自动/人工验收旧测试可显式使用 `auto_delivery=False` 检查兼容模式。普通部署无需新环境变量。重启真实运行的服务、强制刷新浏览器，并使用已批准的 Worker 完成一次任务。对已有 `review_required` 历史任务，使用旧兼容接口安全收尾，不自动批量放行。

在 Linux 3001 部署上先备份原项目和 `state/`，对比源码再更新，避免覆盖 `.env.service`、`server_3001.pid`、日志及本地专有 `team_server.py` 端口补丁。此归档不修改用户远程服务器。

### 专项回归

`python -m pytest -q tests/test_auto_delivery_v089.py tests/test_v089_routing.py tests/test_v089_model_probe.py tests/test_dispatch_runtime.py tests/test_result_acceptance.py tests/test_result_presentation.py`

### 状态语义

- `delivered`：报告已持久化，服务端已写入任务完成记录、释放账本资源，可通过 `/tasks/{id}/result` 读取回答，**不等于独立事实/科研验证**。
- `failed`：Worker 明确失败、结果为空或状态占位符，任务失败且正常释放资源。
- `uncertain`：过期租约、状态不一致、跨库转换错误，保留隔离状态，不自动重放。
