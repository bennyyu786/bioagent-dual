# v0.8.6 — 三级结果验收与 Web 审核中心

## 解决的问题

v0.8.5 中 Worker 完成后，派发状态停在 `review_required`，任务仍是
`running`。人工审核入口只在“双中心拓扑”的派发列表里，且使用临时弹框，
用户不容易发现。`planning-policy.auto_approve_trusted` 只审批执行，
**不是结果验收**。

## 新增 Web 菜单：结果审核

登录 `/control`，在左侧点击 **结果审核**：

- 只显示当前工作区 `review_required` 派发记录（最多最近 500 条）；管理员才能访问；
- 查看任务、派发 ID、Worker、分级和结果 SHA256；
- 点击 **查看完整结果 / 人工审核**，核对原文，填写审核依据，确认后批准或拒绝；
- 审核通过调用原有 `/dispatch/{id}/review`，完成任务并释放资源；
- 审核拒绝将任务标为失败；验证不确定时保留原状态；
- 操作通过既有协作事件和派发审计机制记录。

## 三个验收等级

| 级别 | 规则 | 是否自动完成 |
|---|---|---|
| L1 普通问答 | 只接受有限白名单问句、管理员允许的已批准低风险执行能力、Worker `completed`、非空合理文本和固定制品 SHA256 | 管理员开启验收策略和 L1 能力白名单后，可自动完成 **交付检查**；不是事实真实性保证 |
| L2 代码、数据分析和未识别类型 | 服务器事先安装的独立域验证器返回 `(True, evidence_ref)` | 管理员开启 L2 自动验收且验证通过后自动完成；没有验证器不得批准 |
| L3 科研、临床、基因、生产部署和高风险任务 | 独立验证器通过，**再由管理员核对并手动批准** | 绝不自动批准 |

等级由任务内容和申明的执行能力分类，Worker 不得自己声明更低的等级。
该分类是保守的规则引擎，不是可靠的完整风险识别模型；正式生产应结合
Skill/MCP 权限、任务来源、外部副作用和结构化任务元数据加入规则。

### 配置（管理员）

新安装或升级默认 `enabled=false`，避免悄悄改变已有任务的验收规则。
进入 **结果审核 → 三级验收策略**，可开启策略、自动 L1、自动 L2，
并填写允许低风险自动通过的**具体能力 ID**（如 `qa`）。

后端 API：

- `GET/PUT /api/v2/{ws}/acceptance/policy`（管理员）
- `GET /api/v2/{ws}/acceptance/queue`（管理员）
- `POST /api/v2/{ws}/acceptance/{dispatch_id}/evaluate`（管理员）
- 原有 `GET /api/v2/{ws}/dispatch/{dispatch_id}/evidence`
- 原有 `POST /api/v2/{ws}/dispatch/{dispatch_id}/review`

政策启用后，新执行结果上报会尝试相应自动验收。**已有** `review_required`
记录可在审核中心点“按当前策略尝试自动验证”，或人工检查并审核。
未审核的结果不会仅因等待时间较长自动批准。

### 独立验证器（L2/L3）

服务器安装可信的 Python 模块实现：

```python
# lab_validation.py 仅是接口示意，请实现真正独立的测试/证据检查
# 返回的引用应指向经过独立核验且可追溯的测试结果。
def validate(task, result: str) -> tuple[bool, str]:
    raise NotImplementedError('Configure a task-specific independent validator')
```

然后以受信任的服务器环境启动：

```bash
export BIOMNI_RESULT_VALIDATOR='lab_validation:validate'
python scripts/team_server.py --task-db state/tasks.sqlite
```

或用参数 `--result-validator lab_validation:validate`。不得把验证器模块路径或
代码放到不受信任的 Web 输入中。验证器需要在不信任 Worker 自述的条件下
核验制品，尤其代码测试、数据质量、科研可复现性应使用独立隔离环境。

**在没有正确配置独立验证器时，L2/L3 的人工“审核通过”会返回 503，
而不是虚构独立验证证据。** 用户始终可以拒绝明显错误结果。

## 迁移与验证

1. 停止原服务器，备份完整 `state/`，尤其 `tasks.sqlite`、协作与资源数据库。
2. 升级代码，重启时继续使用原数据库路径和相同工作区。
3. 打开 `/control` → 结果审核；历史待审核任务保持原状并可审核。
4. 如需 L1 自动验收，在管理员明确授权后开启并设置可信能力白名单。
5. 配置 L2/L3 的独立服务器验证器后，在真实部署环境验收。

`pytest -q` 在离线测试环境中执行；本次只验证本地 HTTP/SQLite 和模拟 Worker 的自动化路径。
