from sqlalchemy import String, Double, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
class Monitor(Base):
    __tablename__ = 'monitors'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    zone: Mapped[str] = mapped_column(String(80))
    baseline_job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    # 固定获批核验快照：基准完成率（A/B 合计与分方案），登记后不随客流更正改变
    baseline_rate: Mapped[float] = mapped_column(Double)
    baseline_arm_a: Mapped[float] = mapped_column(Double)
    baseline_arm_b: Mapped[float] = mapped_column(Double)
    baseline_snapshot: Mapped[dict] = mapped_column(String(2000), default='{}')
    drop_threshold: Mapped[float] = mapped_column(Double)      # 下降门槛（百分点），低于 基准-门槛 即异常
    consecutive_limit: Mapped[int] = mapped_column(Integer)    # 连续异常天数门槛
    start_date: Mapped[str] = mapped_column(String(10))        # 监测起始日 YYYY-MM-DD
    epoch: Mapped[int] = mapped_column(Integer, default=1)     # 修订围栏：每次更正递增，旧重算结果不得覆盖新修订
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
class FlowRecord(Base):
    __tablename__ = 'flow_records'
    __table_args__ = (UniqueConstraint('monitor_id','log_date','arm',name='uq_flow_monitor_date_arm'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey('monitors.id'))
    log_date: Mapped[str] = mapped_column(String(10))
    arm: Mapped[str] = mapped_column(String(4))
    visitors: Mapped[int] = mapped_column(Integer)
    completed: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)  # 乐观锁；并发更正按所见修订裁决
    updated_by: Mapped[str] = mapped_column(String(150), default='')
    updated_at: Mapped[float] = mapped_column(Double)
class DailyState(Base):
    __tablename__ = 'daily_states'
    __table_args__ = (UniqueConstraint('monitor_id','log_date',name='uq_state_monitor_date'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey('monitors.id'))
    log_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12))            # normal / missing / below
    rate: Mapped[float | None] = mapped_column(Double, nullable=True)
    rate_a: Mapped[float | None] = mapped_column(Double, nullable=True)
    rate_b: Mapped[float | None] = mapped_column(Double, nullable=True)
    streak: Mapped[int] = mapped_column(Integer, default=0)    # 截至当日连续异常天数；缺报也算异常
    reason: Mapped[str] = mapped_column(String(300), default='')
    epoch: Mapped[int] = mapped_column(Integer, default=1)     # 发布该状态所依据的修订围栏
    published_at: Mapped[float] = mapped_column(Double)
class Alert(Base):
    __tablename__ = 'alerts'
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey('monitors.id'))
    status: Mapped[str] = mapped_column(String(12))            # open / resolved / revoked
    streak_start: Mapped[str] = mapped_column(String(10))      # 连续异常起始日
    streak_end: Mapped[str] = mapped_column(String(10))        # 触发日（连续天数达到门槛当日）
    consecutive_days: Mapped[int] = mapped_column(Integer)
    basis: Mapped[str] = mapped_column(String(1000), default='')   # 生效依据
    resolution: Mapped[str] = mapped_column(String(1000), default='')  # 解除/撤销依据
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Double)
    resolved_at: Mapped[float | None] = mapped_column(Double, nullable=True)
class AlertConfirmation(Base):
    __tablename__ = 'alert_confirmations'
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey('alerts.id'))
    monitor_id: Mapped[int] = mapped_column(ForeignKey('monitors.id'))
    opinion: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(150))
    at: Mapped[float] = mapped_column(Double)
class Recompute(Base):
    __tablename__ = 'recomputes'
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey('monitors.id'))
    status: Mapped[str] = mapped_column(String(12))            # queued / running / completed / failed / superseded
    scope_start: Mapped[str] = mapped_column(String(10))       # 受影响起始日（更正日）
    scope_end: Mapped[str] = mapped_column(String(10))         # 受影响截止日（评估窗口最后一天）
    epoch: Mapped[int] = mapped_column(Integer)                # 发起时围栏；晚回的旧执行不得覆盖新修订
    cursor_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 中断恢复游标
    trigger: Mapped[str] = mapped_column(String(40), default='flow.correction')
    error: Mapped[str] = mapped_column(String(1000), default='')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
