# 交接对账验收记录

本记录只写实际执行过的检查与结果。真实容器（Docker Compose / PostgreSQL / Redis / Nginx）本次未启动，故不宣称容器验收通过。

## 1. 单元与流程测试

命令：`cd backend && python -m pytest tests/test_handovers.py -q`

结果：`15 passed`（Python 3.11、SQLite、FastAPI TestClient，Redis 指向不可达地址以验证数据库兜底路径）。

覆盖项：

| 检查 | 结论 |
|---|---|
| 清单重复日/队/区域、合格数超观察数拒绝 | 通过 |
| viewer 不能认领；analyst 不能确认（服务端权限） | 通过 |
| 未确认前分析返回 409；确认后分析可创建 | 通过 |
| 认领一半时为待补齐；全部认领后为可确认 | 通过 |
| 同一批次行重复认领 409 | 通过 |
| 确认后认领冻结，撤销返回 409 | 通过 |
| 旧批次（legacy）照常分析 | 通过 |
| 一项清单分给两批，两批各自确认后清单已确认 | 通过 |
| 剩余合格数将超剩余观察数时拒绝认领 | 通过 |
| 撤销只退原认领量；重复撤销 409 且不多退 | 通过 |
| 认领幂等键重试不重复扣减、`replayed=true` | 通过 |
| 同一幂等键换一组明细返回 409 | 通过 |
| 4 线程并发认领同一行：1 个 201、3 个 409，账目只扣一次 | 通过 |
| 4 线程并发认领不同行：余额与总量精确 | 通过 |
| 确认中断（confirming）恢复：账目不变、退回草案、单批次重试成功 | 通过 |
| 整批不齐时确认返回 409，并列出缺项与计数差异 | 通过 |
| 逐行明细为未认领行反查清单项及剩余余额 | 通过 |
| 清单详情暴露缺项、余量与逐行计数差异 | 通过 |

## 2. 真实 HTTP 联调（Uvicorn + SQLite + 演示种子）

`SEED_DEMO=true` 启动后，用 urllib 逐接口调用（Redis 不可用，健康检查按设计返回 503；业务接口不受影响）：

| 步骤 | 观察 |
|---|---|
| 登录 admin | 200 |
| `GET /handovers` | 1 份清单，`pending_complete`，缺项 113 |
| `GET /batches/status` | legacy 1、pending_complete 1 |
| `GET /overview` | 含 handovers=1 与 batch_status 计数 |
| `GET /datasets/2/reconciliation/rows` | 120 行，未认领行均带清单项与余额 |
| `GET /handovers/1/reconciliation` | 120 行明细、112 行差异、8 条去向 |
| 认领剩余 112 行 | 201 |
| 批次状态 | `ready`，issue_count 0 |
| 确认（幂等键 live-c1） | 200，`confirmed` |
| 同键重试 | 200，`already_confirmed=true` |
| 提交分析 | 201，`queued` |
| 清单状态 | `ready`（清单仍余 2026-09-30 一项 8/9 未认领，符合“一项可分多批”） |

## 3. 前端

- `node --check` 遍历 `frontend/src/**/*.js`：全部通过。
- 用 Node 桩化 `fetch`/`localStorage` 直接渲染 `handovers.js`、`reconcile.js`：
  - 清单列表含状态条与状态徽标；
  - 清单详情含“清单去向”表；
  - 逐行页在 `ready` 时渲染“整批吻合，确认开放分析”按钮，在 `pending_complete` 时渲染认领按钮。

## 4. 未执行项

- Docker Compose 五服务健康检查、PostgreSQL 部分唯一索引在真实库上的并发行为、Nginx 反向代理、浏览器截图与端到端点击：本次环境无 Docker，未执行。
- 生产级“确认过程中进程被杀”的故障注入：仅通过把状态置为 `confirming` 后调用恢复函数模拟，未做真实 kill -9 测试。
