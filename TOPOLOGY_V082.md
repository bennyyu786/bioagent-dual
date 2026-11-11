# v0.8.2 双中心执行拓扑

## 新功能

登录 Control Center 后选择 **双中心拓扑**，在同一张 SVG 画布中查看任务、能力和 Worker。支持搜索、缩放、选中详情、任务轨迹跳转，并随已有 WebSocket 工作区事件刷新（连接断开时使用原有轮询）。

新增只读 `GET /api/v2/{workspace}/topology`。必须提供工作区成员 Bearer token；接口返回 `nodes`, `edges`, `counts`, `scope`, `semantics`, `hidden_dependency_count`。

### 关系语义（不可混淆）

- `depends_on`: 同一工作区**可见**的任务依赖，隐藏其他/未知任务的 ID。
- `requires`: TaskCenter 声明需要此能力。**不**意味着能力已安装或健康。
- `selected`: TaskCenter 记录了所选择的能力。**不**意味着能力调用成功。
- `reported_on`: 已认证 Worker 的日志明确引用了任务 ID，**不**意味着该 Worker 当前被派发任务。

能力健康状态一律保守展示为 `unknown`，直至接入工作区隔离、可信 Capability Registry 状态来源。故障/阻塞任务以状态标识；Remediation Orchestrator 的完整工单和资源配额尚未并入本拓扑。

### 安全

工作区访问权限在服务端检查。TaskCenter 原始行仅在任务属于当前工作区时读取；只输出能力名称和关联类别，不返回任意 Worker 日志文本、凭据、推理内容、结果正文。Worker 只显示当前工作区注册的 Worker。

### 限制

SVG 节点布局为客户端稳定列式布局，面向中等规模任务图；尚非上千节点图的增量渲染方案。当前使用原有短票据 WebSocket 通知触发全量刷新，并非每次事件推送拓扑增量。原有轮询策略仍有效。

## 启动

```bash
pip install -e '.[server,test]'
python scripts/team_server.py
# 访问 http://127.0.0.1:8766/control 并登录
```

## 测试

```bash
pytest -q
node --check /path/to/extracted_frontend_script.js
```

真实远程 Worker、Claude/Codex、Biomni、Docker 和生产 WebSocket 负载测试仍需部署环境验收。
