# v0.8.3 运行时关系图与资源调度可视化

## 实现范围

本版在 `biomni_dual/` 统一主包新增 `runtime_relations.py`，为工作区内的任务、Worker 和能力提供可审计的关系证据：

- Worker 容量：管理员设置 `cpu_milli`（毫核）、`memory_mb`（MiB）、`gpu_units`（整数 GPU 配额）。这些数字是**配置容量**，不是实际硬件利用率。
- 资源预留：管理员明确提交、全局数据库事务内配额检查、幂等 `request_id`、每个任务最多一个活动预留。
- Worker 租约：凭 Worker 专用密钥领取预留；只允许领取本 Worker 的预留，禁止重复领取。
- 执行回执：Worker 报告完成或失败。它是**Worker 自报**，不能替代外部执行证明、TaskCenter 验收或科学结果的独立验证。
- 异常恢复：租约逾期进入 `uncertain`，配额仍占用，必须管理员核实外部副作用后调用恢复接口，防止盲目重派。
- 能力生命周期：存储带证据引用的人工登记状态 `discovered / sandbox_verified / approved / retired`。登记本身不证明环境健康。
- 修复依赖：记录同工作区两个任务的修复关联；并非已自动完成修复。
- 双中心拓扑：除 `requires`/`selected`/`reported_on` 外，新增加 `reservation`/`repair_dependency`。终态预留仅出现在审计列表，不再画成当前派发。
- 运行资源看板：按 Worker 展示 CPU/内存/GPU 配置容量、预留量、可用配额和派发/修复记录，工作区 WebSocket 事件触发刷新，10 秒轮询回退。

## HTTP 接口

所有 `/api/v2/{ws}/runtime/...` 读接口要求当前工作区 Viewer 以上身份；配额、预留、能力证据和修复链的写接口要求 Admin/Owner。Worker 领取与回执要求 Worker 共享密钥，且工作区与资源预留关联严格匹配。

| 接口 | 访问 | 功能 |
|---|---|---|
| `GET /api/v2/{ws}/runtime/resources` | 成员 | 配额、活动/历史预留、能力生命周期、修复关系 |
| `PUT /api/v2/{ws}/runtime/workers/{worker}/capacity` | 管理员 | 配置资源配额 |
| `POST /api/v2/{ws}/runtime/reservations` | 管理员 | 提交任务资源预留 |
| `POST /api/v2/worker/{worker}/runtime/reservations/{id}/claim` | Worker | 认证领取租约 |
| `POST /api/v2/worker/{worker}/runtime/reservations/{id}/finish` | Worker | 提交执行回执 |
| `POST /api/v2/{ws}/runtime/leases/scan` | 管理员 | 隔离逾期租约 |
| `POST /api/v2/{ws}/runtime/reservations/{id}/reconcile` | 管理员 | 人工核对不确定租约后释放配额 |
| `PUT /api/v2/{ws}/runtime/capabilities/{id}` | 管理员 | 登记能力阶段和证据引用 |
| `POST /api/v2/{ws}/runtime/repairs` | 管理员 | 登记修复任务依赖 |
| `GET /api/v2/{ws}/topology` | 成员 | 合并拓扑与运行时资源证据 |

### 示例

```
PUT /api/v2/main/runtime/workers/codex-01/capacity
{"cpu_milli":8000,"memory_mb":16384,"gpu_units":1}

POST /api/v2/main/runtime/reservations
{"task_id":"<已有且处于 pending 的任务ID>","worker":"codex-01",
 "request_id":"unique-request-01",
 "resources":{"cpu_milli":2000,"memory_mb":4096,"gpu_units":1}}
```

**注意：** 首次使用应先用 Admin 注册 Worker 并保存其一次性密钥。此版本的 `POST /reservations` 只进行资源预留，不调用 `TaskCenter.claim`，不会在 Worker 上实际运行工具。后续的可信远程执行器需另行连接任务领取、作业心跳和独立验证结果。

## 安全与一致性

1. 通过 SQLite `BEGIN IMMEDIATE` 做跨线程/进程的原子配额检查，无法跨 SQLite 文件或跨集群节点实现原子性。生产高可用应迁移至 PostgreSQL 或其他支持事务和租约的一致性存储。
2. 每个工作区各自拥有独立容量、任务关联及权限。API 检查资源预留 task_id 对应 TaskCenter 的 tenant，修复关系的两个任务也必须都归属于当前工作区。
3. 活动状态是 `reserved, claimed, uncertain`，其容量计入已使用。`completed, failed, reconciled` 为已释放配额的终态。
4. 租约超时不会自动重新排队，外部动作状态未知时必须人工核验。
5. `approved` 能力阶段是带证据引用的管理员登记，不等于经过当前环境实时健康检查；日志 `reported_on` 不代表调度领取；Worker 的 `completed` 回执不代表科研结果验收通过。
6. Web 控制平面依然需要 TLS、专业密钥保管、操作审计、数据库备份及真实远程 Worker 集成后才能作为生产系统部署。

## 测试

`pytest -q`。新增 `tests/test_runtime_relations.py` 检查并发配额、幂等、租约逾期、跨工作区 RBAC、Worker 认证和拓扑状态。
