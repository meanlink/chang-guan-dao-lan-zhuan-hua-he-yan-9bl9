from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
class DailyStatus(str, Enum):
    normal = 'normal'        # 正常：两方案客流齐备且完成率未跌破门槛
    missing = 'missing'      # 缺报：当日 A/B 客流不完整，缺报不视作正常
    below = 'below'          # 下降：完成率低于 基准-下降门槛
class AlertStatus(str, Enum):
    open = 'open'            # 告警生效中
    resolved = 'resolved'    # 恢复后解除
    revoked = 'revoked'      # 更正重算后失去依据而撤销
class RecomputeStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
    superseded = 'superseded'  # 围栏过期：旧执行晚回，不覆盖新修订
