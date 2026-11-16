"""展区完成率监测演示数据（确定性，可重复执行，幂等）。

固定以 2026-11-16 为演示「今天」，三展区各登记一条监测，覆盖：
连续下降开告警、恢复后解除、缺报计异常、补报、更正后重算撤销并保留确认意见。
所有完成数按固定完成率确定性构造，避免随机抽样导致场景不稳定。
"""
import time
import json
from datetime import date, timedelta
from sqlalchemy import select, func
from src.config.database import Session
from src.models import Monitor, FlowRecord, Recompute, Alert, AlertConfirmation, User, Job
from src.services.monitoring import run_recompute_sync, ZONES, ARMS
from src.services.audit import record

ANCHOR = date(2026, 11, 16)          # 演示锚定日；与演示剧本同期
DAYS = 14
# 各展区监测参数：下降门槛（百分点）、连续异常天数门槛
MONITOR_SPECS = {
    '星空厅': {'threshold': 5.0, 'limit': 3},
    '水世界': {'threshold': 8.0, 'limit': 3},
    '机械坊': {'threshold': 10.0, 'limit': 3},
}
# 场景剧本（日序 0..13 对应 11-03..11-16）：
# 星空厅：日 2/3/4（11-05..07）连续三日下降→开告警；日 5 恢复→自动解除；
#         日 9（11-12）缺报计异常但单日不连警；更正日 4 后原连续下降断裂→撤销告警。
# 水世界：日 11/12/13（11-14..16）连续三日下降且持续到窗口末尾→告警生效中（open）。
# 机械坊：全部正常，仅日 10 缺报单日，无告警。
BELOW_DAYS = {'星空厅': {2, 3, 4}, '水世界': {11, 12, 13}, '机械坊': set()}
MISSING_DAYS = {'星空厅': {9}, '水世界': set(), '机械坊': {10}}
# 下降日相对基准再压低的幅度（百分点），确保严格跌破「基准−门槛」
BELOW_MARGIN = 8.0
# 更正剧本：修复星空厅第 4 日（11-07）的偏低完成数，使其回到正常，连续下降断裂
CORRECTION = {'zone': '星空厅', 'day_index': 4, 'target_rate_pct': None}  # 目标率在运行时按基准计算


def _gen_rows(baseline_rate, zone):
    """确定性生成 DAYS 天 A/B 客流（不含缺报日），返回 {(idx,arm): (visitors, completed)}。"""
    spec = MONITOR_SPECS[zone]
    rows = {}
    for idx in range(DAYS):
        visitors = 80  # 固定样本量，保证结果可复现
        if idx in BELOW_DAYS[zone]:
            rate = max(0.05, (baseline_rate - spec['threshold'] - BELOW_MARGIN) / 100.0)
        else:
            rate = (baseline_rate + 2.0) / 100.0  # 正常日略高于基准，稳定不触线
        completed = round(visitors * rate)
        for arm in ARMS:
            rows[(idx, arm)] = (visitors, completed)
    return rows


def seed_monitoring():
    with Session() as db:
        baseline = db.scalar(select(Job).where(Job.approved == True).order_by(Job.id))
        admin = db.scalar(select(User).where(User.role == 'admin'))
        if not baseline or not admin:
            print('monitoring seed skipped: baseline job/admin missing; run scripts.seed_demo first')
            return
        if db.scalar(select(Monitor).where(Monitor.zone == '星空厅')):
            print('monitoring seed skipped: monitors already present')
            return
        all_dates = [(ANCHOR - timedelta(days=DAYS - 1 - i)).isoformat() for i in range(DAYS)]
        for zone in ZONES:
            spec = MONITOR_SPECS[zone]
            picked = [r for r in baseline.result['table']['rows'] if r['zone'] == zone and r['arm'] in ARMS]
            by_arm = {r['arm']: r for r in picked}
            rate = 100.0 * sum(int(r['completed']) for r in picked) / sum(int(r['visitors']) for r in picked)
            snapshot = {'job_id': baseline.id, 'job_name': baseline.name, 'zone': zone,
                        'arm_a': {'visitors': int(by_arm['A']['visitors']), 'completed': int(by_arm['A']['completed']), 'rate': float(by_arm['A']['rate'])},
                        'arm_b': {'visitors': int(by_arm['B']['visitors']), 'completed': int(by_arm['B']['completed']), 'rate': float(by_arm['B']['rate'])},
                        'combined_rate': round(rate, 4)}
            m = Monitor(name=f'{zone}完成率持续监测', zone=zone, baseline_job_id=baseline.id,
                        baseline_rate=round(rate, 4),
                        baseline_arm_a=round(float(by_arm['A']['rate']), 4),
                        baseline_arm_b=round(float(by_arm['B']['rate']), 4),
                        baseline_snapshot=json.dumps(snapshot, ensure_ascii=False), drop_threshold=spec['threshold'],
                        consecutive_limit=spec['limit'], start_date=all_dates[0], epoch=1,
                        created_by=admin.id, created_at=time.time())
            db.add(m); db.flush()
            rows = _gen_rows(rate, zone)
            for idx, ds in enumerate(all_dates):
                if idx in MISSING_DAYS[zone]:
                    continue  # 演示缺报：不写任何方案客流，重算时判 missing
                for arm in ARMS:
                    visitors, completed = rows[(idx, arm)]
                    db.add(FlowRecord(monitor_id=m.id, log_date=ds, arm=arm, visitors=visitors,
                                      completed=completed, revision=1, updated_by='seed',
                                      updated_at=time.time()))
            rc = Recompute(monitor_id=m.id, status='queued', scope_start=m.start_date,
                           scope_end=all_dates[-1], epoch=1, trigger='monitor.register',
                           created_by='seed', created_at=time.time())
            db.add(rc); db.flush()
            run_recompute_sync(rc.id, db=db)
            record(db, 'seed', 'monitor.seed', {'id': m.id, 'zone': zone})
        # 更正剧本：修复星空厅第 4 日（11-07）的偏低完成数 → 连续下降断裂，原告警失去依据被撤销
        star = db.scalar(select(Monitor).where(Monitor.zone == '星空厅'))
        target_date = all_dates[CORRECTION['day_index']]
        # 先给仍生效/已解除的告警补负责人确认意见，验证撤销后意见保留
        for a in db.scalars(select(Alert).where(Alert.monitor_id == star.id)):
            if a.status in ('open', 'resolved'):
                db.add(AlertConfirmation(alert_id=a.id, monitor_id=star.id,
                                         opinion='已联系展区主管核查现场讲解排班，持续跟进完成率变化。',
                                         actor=admin.email, at=time.time()))
        # 更正为正常水平（基准+2pp），A/B 同步修订，修订号 1→2
        normal_completed = round(80 * (star.baseline_rate + 2.0) / 100.0)
        for f in db.scalars(select(FlowRecord).where(FlowRecord.monitor_id == star.id,
                                                     FlowRecord.log_date == target_date)):
            f.completed = normal_completed
            f.revision, f.updated_by = 2, 'seed.correction'
        star.epoch = 2
        rc2 = Recompute(monitor_id=star.id, status='queued', scope_start=target_date,
                        scope_end=all_dates[-1], epoch=2, trigger='flow.correction',
                        created_by='seed', created_at=time.time())
        db.add(rc2); db.flush()
        run_recompute_sync(rc2.id, db=db)
        record(db, 'seed', 'monitor.correction.demo', {'monitor': star.id, 'date': target_date})
        db.commit()
        counts = {t: db.scalar(select(func.count()).select_from(model))
                  for t, model in [('monitors', Monitor), ('flows', FlowRecord),
                                   ('alerts', Alert), ('confirmations', AlertConfirmation)]}
        print(counts)


if __name__ == '__main__':
    seed_monitoring()
