# 场馆导览转化核验

分类独立性检验的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

## 展区完成率持续监测
导览改版后对各展区完成率做持续追踪（页面「完成率监测」）。
- **固定基准**：登记监测时选择一个已获批核验（Job），把该展区 A/B 及合计完成率快照固定为基准，之后不随客流更正改变。
- **逐日评估**：每日接入 A/B 两方案客流，合计完成率低于「基准 − 下降门槛」记为下降（below）；任一方案缺报记为缺报（missing），**缺报不视作正常**，两者都计入连续异常天数。
- **告警口径**：异常连续达到设定天数才开告警；首个正常日恢复即自动解除；恢复后再次连续下降可重新告警（每段异常区间独立成警）。
- **补报与更正**：无数据日提交为补报；已有数据按所见修订号（`FlowRecord.revision`）更正。并发更正凭修订号乐观裁决，过期整笔拒绝；每次更正递增监测 `epoch` 并重算受影响日期。
- **重算可靠性**：`Recompute` 带 epoch 围栏与游标，分片执行、中断后从断点恢复；旧执行晚回一律 `superseded` 不覆盖新修订；重试幂等不重复开告警；全部受影响日期处理完才在同一事务发布新状态。
- **撤销与留痕**：更正后失去依据的告警置为 revoked，原确认意见（`AlertConfirmation`）永久保留；每条告警记录生效依据与解除/撤销依据，页面可追查。
新增实体：Monitor 监测登记、FlowRecord 每日 A/B 客流、DailyState 逐日状态、Alert 告警、AlertConfirmation 确认意见、Recompute 重算任务。后端入口 `src/routes/monitors.py`、规则引擎 `src/services/monitoring.py`；演示数据 `python -m scripts.seed_monitoring`（随 seed_demo 自动执行）。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18204；后端文档 http://localhost:19204/docs；数据库宿主端口 17704。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=guide-chi；FRONTEND_PORT=18204；BACKEND_PORT=19204；DB_PORT=17704；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1005/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
