"""展区完成率监测。

规则口径（与业务需求逐条对应）：
- 基准固定取登记时选择的「已获批核验」结果快照，之后不随客流更正改变。
- 每日须接入 A/B 两方案客流；任一缺报即为异常（missing），缺报不视作正常。
- 当日 A/B 合计完成率低于「基准 − 下降门槛」为下降异常（below）。
- 仅当异常（缺报或下降）连续达到 consecutive_limit 天才开告警；恢复（首个正常日）后解除；
  再次形成连续下降可重新告警（每段独立异常区间各自成警）。
- 更正历史客流会递增 monitor.epoch 并重算受影响日期；失去依据的告警撤销，原确认意见保留。
- 并发更正凭 FlowRecord.revision 乐观裁决：修订号不符则整笔拒绝。
- 重算任务带 epoch 围栏：旧执行晚回一律 superseded，不覆盖新修订；
  中断后按游标恢复、重试不重复开告警；全部受影响日期处理完成才在同一事务发布新状态。
"""
import json
import time
from datetime import date, timedelta
from fastapi import HTTPException
from sqlalchemy import select, delete
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Monitor, FlowRecord, DailyState, Alert, AlertConfirmation, Recompute, Job
from src.constants.enums import DailyStatus, AlertStatus, RecomputeStatus

RECOMPUTE_QUEUE = 'monitor:recompute'
CHUNK_DAYS = 40
ZONES = ('星空厅', '水世界', '机械坊')
ARMS = ('A', 'B')


# ---------------------------------------------------------------- 日期与纯函数
def today_str():
    return date.today().isoformat()

def daterange(start, end):
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += timedelta(days=1)

def _round(x):
    return round(float(x), 4)

def evaluate_window(monitor, flows, window_end):
    """纯函数：按 monitor 口径评估 start_date..window_end 每一天的状态与连续异常天数。

    monitor: dict，至少含 start_date/baseline_rate/drop_threshold/consecutive_limit。
    flows:   list[dict]，每项含 log_date/arm/visitors/completed。
    返回按日期升序的行字典列表；缺报日 rate 为 None，streak 仍累加。
    """
    index = {(f['log_date'], f['arm']): f for f in flows}
    floor = float(monitor['baseline_rate']) - float(monitor['drop_threshold'])
    # 只有在该监测接入过任何客流之后，未接入的日期才算缺报；起始日之前的空窗不追溯计异常
    first_flow = min((f['log_date'] for f in flows), default=None)
    rows, streak = [], 0
    for ds in daterange(monitor['start_date'], window_end):
        a, b = index.get((ds, 'A')), index.get((ds, 'B'))
        if not a or not b:
            if first_flow is None or ds < first_flow:
                rows.append({'log_date': ds, 'status': DailyStatus.missing.value, 'rate': None,
                             'rate_a': None, 'rate_b': None, 'streak': streak,
                             'visitors_a': None, 'completed_a': None, 'visitors_b': None, 'completed_b': None,
                             'reason': '监测尚未接入客流（空窗期，不计连续异常）'})
                continue
            streak += 1
            rows.append({'log_date': ds, 'status': DailyStatus.missing.value, 'rate': None,
                         'rate_a': None, 'rate_b': None, 'streak': streak,
                         'visitors_a': None, 'completed_a': None, 'visitors_b': None, 'completed_b': None,
                         'reason': '当日 A/B 客流缺报，缺报不视作正常'})
            continue
        rate_a = 100.0 * a['completed'] / a['visitors']
        rate_b = 100.0 * b['completed'] / b['visitors']
        rate = 100.0 * (a['completed'] + b['completed']) / (a['visitors'] + b['visitors'])
        if rate < floor:
            streak += 1
            status, reason = DailyStatus.below.value, (
                f'完成率 {rate:.2f}% 低于基准 {float(monitor["baseline_rate"]):.2f}% 减门槛 '
                f'{float(monitor["drop_threshold"]):g}pp 后的下限 {floor:.2f}%')
        else:
            streak = 0
            status, reason = DailyStatus.normal.value, f'完成率 {rate:.2f}% 未跌破告警下限 {floor:.2f}%'
        rows.append({'log_date': ds, 'status': status, 'rate': _round(rate),
                     'rate_a': _round(rate_a), 'rate_b': _round(rate_b), 'streak': streak,
                     'visitors_a': a['visitors'], 'completed_a': a['completed'],
                     'visitors_b': b['visitors'], 'completed_b': b['completed'], 'reason': reason})
    return rows

def build_episodes(rows, limit):
    """纯函数：把逐日状态归并为异常区间，返回达到连续天数门槛的告警片段（确定性）。"""
    episodes, i = [], 0
    while i < len(rows):
        if rows[i]['status'] == DailyStatus.normal.value:
            i += 1
            continue
        start = i
        while i < len(rows) and rows[i]['status'] != DailyStatus.normal.value:
            i += 1
        run, normal_index = rows[start:i], i
        if len(run) >= limit:
            kinds = '/'.join('缺报' if r['status'] == DailyStatus.missing.value else '下降' for r in run)
            episodes.append({'streak_start': run[0]['log_date'], 'streak_end': run[limit - 1]['log_date'],
                             'run_end': run[-1]['log_date'], 'days': len(run), 'kinds': kinds,
                             'resolved': normal_index < len(rows),
                             'resolve_date': rows[normal_index]['log_date'] if normal_index < len(rows) else None})
    return episodes


# ---------------------------------------------------------------- 监测登记
def baseline_for(db, job_id, zone):
    j = db.get(Job, job_id)
    if not j:
        raise HTTPException(404, '获批核验任务不存在')
    if j.status != 'completed' or not j.approved:
        raise HTTPException(409, '基准必须取自已完成且获批准的核验结果')
    if zone not in ZONES:
        raise HTTPException(422, '监测展区须为具体展区（星空厅/水世界/机械坊）')
    picked = [r for r in (j.result or {}).get('table', {}).get('rows', []) if r.get('zone') == zone and r.get('arm') in ARMS]
    by_arm = {r['arm']: r for r in picked}
    if set(by_arm) != set(ARMS):
        raise HTTPException(409, f'获批核验中缺少展区 {zone} 的 A/B 分区结果，无法固定基准')
    va, ca = int(by_arm['A']['visitors']), int(by_arm['A']['completed'])
    vb, cb = int(by_arm['B']['visitors']), int(by_arm['B']['completed'])
    rate_a, rate_b = 100.0 * ca / va, 100.0 * cb / vb
    rate = 100.0 * (ca + cb) / (va + vb)
    snapshot = {'job_id': j.id, 'job_name': j.name, 'zone': zone,
                'arm_a': {'visitors': va, 'completed': ca, 'rate': _round(rate_a)},
                'arm_b': {'visitors': vb, 'completed': cb, 'rate': _round(rate_b)},
                'combined_rate': _round(rate)}
    return rate, rate_a, rate_b, snapshot

def create_monitor(db, data, user):
    try:
        start = date.fromisoformat(data.start_date)
    except ValueError:
        raise HTTPException(422, '起始日期格式须为 YYYY-MM-DD')
    if start > date.today():
        raise HTTPException(422, '监测起始日不能晚于今天')
    rate, rate_a, rate_b, snapshot = baseline_for(db, data.baseline_job_id, data.zone)
    m = Monitor(name=data.name.strip(), zone=data.zone, baseline_job_id=data.baseline_job_id,
                baseline_rate=_round(rate), baseline_arm_a=_round(rate_a), baseline_arm_b=_round(rate_b),
                baseline_snapshot=json.dumps(snapshot, ensure_ascii=False),
                drop_threshold=float(data.drop_threshold), consecutive_limit=int(data.consecutive_limit),
                start_date=data.start_date, epoch=1, created_by=user.id, created_at=time.time())
    db.add(m); db.flush()
    # 登记时尚无客流：初始重算只评估起始日（空窗不计异常），避免把未来空窗误判为缺报
    rc = _open_recompute(db, m, data.start_date, data.start_date, trigger='monitor.register', actor=user.email)
    return m, rc


# ---------------------------------------------------------------- 客流接入 / 补报 / 更正
def _validate_items(items):
    arms = {x.arm for x in items}
    if arms != set(ARMS):
        raise HTTPException(422, '每日须同时接入 A、B 两方案客流，缺报不视作正常')
    cleaned = {}
    for x in items:
        if not isinstance(x.visitors, int) or not isinstance(x.completed, int):
            raise HTTPException(422, '进区与完成人数须为整数')
        if x.visitors <= 0:
            raise HTTPException(422, '进区人数须为正整数')
        if not 0 <= x.completed <= x.visitors:
            raise HTTPException(422, '完成讲解人数须满足 0 ≤ 完成 ≤ 进区')
        cleaned[x.arm] = (x.visitors, x.completed)
    return cleaned

def ingest_flow(db, monitor, data, user):
    if not (monitor.start_date <= data.log_date <= today_str()):
        raise HTTPException(422, '客流日期须落在监测起始日与今天之间，未来数据拒绝接入')
    cleaned = _validate_items(data.items)
    existing = {f.arm: f for f in db.scalars(select(FlowRecord).where(
        FlowRecord.monitor_id == monitor.id, FlowRecord.log_date == data.log_date))}
    now = time.time()
    if data.expected_revision is None:
        if existing:  # 补报撞车：该日已有数据，整笔拒绝，要求走更正
            rev = next(iter(existing.values())).revision
            raise HTTPException(409, f'该日客流已存在（当前修订号 {rev}），请按修订号提交更正，整笔未写入')
        for arm in ARMS:
            v, c = cleaned[arm]
            db.add(FlowRecord(monitor_id=monitor.id, log_date=data.log_date, arm=arm,
                              visitors=v, completed=c, revision=1, updated_by=user.email, updated_at=now))
        action = 'flow.backfill'
    else:
        if set(existing) != set(ARMS):
            raise HTTPException(409, '该日客流不完整，无法按修订号更正，请先补报缺失方案')
        seen = {f.arm: f.revision for f in existing.values()}
        if any(rev != data.expected_revision for rev in seen.values()):
            raise HTTPException(409, f'修订已过期（所见 {data.expected_revision}，现行 {sorted(set(seen.values()))}），整笔拒绝，请刷新后按最新修订号重试')
        for arm in ARMS:
            v, c = cleaned[arm]
            f = existing[arm]
            f.visitors, f.completed = v, c
            f.revision = data.expected_revision + 1
            f.updated_by, f.updated_at = user.email, now
        action = 'flow.correction'
    monitor.epoch += 1
    db.flush()
    # 评估窗口截止到「已接入客流的最大日期」：之后的日期数据尚未到期，不追溯判缺报
    latest = db.scalar(select(FlowRecord.log_date).where(
        FlowRecord.monitor_id == monitor.id).order_by(FlowRecord.log_date.desc()).limit(1))
    window_end = max(latest or data.log_date, data.log_date)
    rc = _open_recompute(db, monitor, data.log_date, window_end, trigger=action, actor=user.email)
    return action, rc

def _open_recompute(db, monitor, scope_start, scope_end, trigger, actor):
    rc = Recompute(monitor_id=monitor.id, status='queued', scope_start=scope_start, scope_end=scope_end,
                   epoch=monitor.epoch, cursor_date=None, trigger=trigger, created_by=actor, created_at=time.time())
    db.add(rc); db.flush()
    try:
        r = Redis.from_url(REDIS_URL, socket_timeout=2)
        r.rpush(RECOMPUTE_QUEUE, rc.id); r.close()
    except Exception:
        pass  # SQL queued 状态兜底，worker tick 会捞起
    return rc

def enqueue_full_recompute(db, monitor, actor, trigger='manual.retry'):
    """人工发起：对监测全窗口（起始日至最新客流日）重算，epoch 围栏取当前监测值。"""
    latest = db.scalar(select(FlowRecord.log_date).where(
        FlowRecord.monitor_id == monitor.id).order_by(FlowRecord.log_date.desc()).limit(1))
    scope_end = latest or monitor.start_date
    return _open_recompute(db, monitor, monitor.start_date, scope_end, trigger, actor)


# ---------------------------------------------------------------- 重算执行（可恢复 + 围栏）
def run_recompute(rc_id):
    """供 worker 调用：每个循环只推进一个分片后返回，cursor_date 持久化以支持中断恢复。"""
    with _session() as db:
        rc = db.get(Recompute, rc_id)
        if not rc or rc.status not in (RecomputeStatus.queued.value, RecomputeStatus.running.value):
            return
        monitor = db.get(Monitor, rc.monitor_id)
        if not monitor or monitor.epoch != rc.epoch:
            rc.status, rc.finished_at, rc.error = RecomputeStatus.superseded.value, time.time(), \
                '监测已产生更新的修订（epoch 围栏过期），旧执行不覆盖新修订'
            db.commit(); return
        rc.status = RecomputeStatus.running.value
        begin = rc.cursor_date or rc.scope_start
        dates = list(daterange(begin, rc.scope_end))
        processed = 0
        for ds in dates:
            if processed >= CHUNK_DAYS:
                break
            rc.cursor_date = ds  # 逐日推进；崩溃后从游标之后恢复
            processed += 1
        db.commit()
        if rc.cursor_date == rc.scope_end:
            _finalize(db, rc)

def run_recompute_sync(rc_id, db=None):
    """供 seed/测试与人工重试同步驱动：循环推进直到完成、失败或被围栏作废。

    可传入外部会话（seed 在同一事务内建数据后立即重算）；否则自建会话。
    """
    own = db is None
    if own:
        db = _session()
    try:
        rc = db.get(Recompute, rc_id)
        if not rc:
            raise RuntimeError('重算任务不存在')
        monitor = db.get(Monitor, rc.monitor_id)
        if not monitor:
            raise RuntimeError('监测不存在')
        if monitor.epoch != rc.epoch:
            rc.status, rc.finished_at, rc.error = RecomputeStatus.superseded.value, time.time(), \
                '监测已产生更新的修订（epoch 围栏过期），旧执行不覆盖新修订'
            if own: db.commit()
            return rc
        rc.status, rc.error = RecomputeStatus.running.value, ''
        if own: db.commit()
        now = _publish_results(db, rc, monitor)
        rc.status, rc.cursor_date, rc.finished_at = RecomputeStatus.completed.value, rc.scope_end, now
        if own: db.commit()
        return rc
    finally:
        if own: db.close()

def _publish_results(db, rc, monitor):
    """共享发布核心：评估窗口、覆盖受影响日期状态、对账告警。不负责提交与终态设置。"""
    flows = [{'log_date': f.log_date, 'arm': f.arm, 'visitors': f.visitors, 'completed': f.completed}
             for f in db.scalars(select(FlowRecord).where(
                 FlowRecord.monitor_id == monitor.id,
                 FlowRecord.log_date.between(monitor.start_date, rc.scope_end)).order_by(FlowRecord.log_date))]
    mcfg = {'start_date': monitor.start_date, 'baseline_rate': monitor.baseline_rate,
            'drop_threshold': monitor.drop_threshold, 'consecutive_limit': monitor.consecutive_limit}
    rows = evaluate_window(mcfg, flows, rc.scope_end)
    now = time.time()
    db.execute(delete(DailyState).where(DailyState.monitor_id == monitor.id,
                                        DailyState.log_date <= rc.scope_end))
    for r in rows:
        db.add(DailyState(monitor_id=monitor.id, log_date=r['log_date'], status=r['status'],
                          rate=r['rate'], rate_a=r['rate_a'], rate_b=r['rate_b'], streak=r['streak'],
                          reason=r['reason'], epoch=monitor.epoch, published_at=now))
    _reconcile_alerts(db, monitor, rows, rc)
    return now

def _finalize(db, rc):
    monitor = db.get(Monitor, rc.monitor_id)
    if monitor.epoch != rc.epoch:  # 提交前最后一次围栏校验：晚回的旧执行绝不发布
        rc.status, rc.finished_at, rc.error = RecomputeStatus.superseded.value, time.time(), \
            '发布前发现更新修订，旧结果作废'
        db.commit(); return
    now = _publish_results(db, rc, monitor)
    rc.status, rc.finished_at = RecomputeStatus.completed.value, now
    db.commit()

def _reconcile_alerts(db, monitor, rows, rc):
    """全窗口确定性对账：缺依据的撤销、新片段开警、已恢复的解除；已撤销告警与确认永不改动。"""
    episodes = build_episodes(rows, monitor.consecutive_limit)
    live = list(db.scalars(select(Alert).where(Alert.monitor_id == monitor.id,
                                               Alert.status != AlertStatus.revoked.value)))
    by_start = {a.streak_start: a for a in live}
    ep_starts = set()
    for ep in episodes:
        ep_starts.add(ep['streak_start'])
        alert = by_start.get(ep['streak_start'])
        basis = (f"自 {ep['streak_start']} 起连续 {monitor.consecutive_limit} 日异常（{ep['kinds']}），"
                 f"{ep['streak_end']} 达到门槛；基准 {monitor.baseline_rate:.2f}%，"
                 f"下降门槛 {monitor.drop_threshold:g}pp（修订 epoch {monitor.epoch}）")
        if alert is None:  # 重试时同 streak_start 已存在即不会重复开警
            db.add(Alert(monitor_id=monitor.id,
                         status=AlertStatus.resolved.value if ep['resolved'] else AlertStatus.open.value,
                         streak_start=ep['streak_start'], streak_end=ep['streak_end'],
                         consecutive_days=ep['days'], basis=basis,
                         resolution=(f"{ep['resolve_date']} 客流恢复且完成率回到告警下限之上，连续下降中断，自动解除"
                                     if ep['resolved'] else ''),
                         epoch=monitor.epoch, created_at=time.time(),
                         resolved_at=time.time() if ep['resolved'] else None))
        else:
            alert.streak_end, alert.consecutive_days, alert.basis, alert.epoch = \
                ep['streak_end'], ep['days'], basis, monitor.epoch
            if ep['resolved'] and alert.status == AlertStatus.open.value:
                alert.status = AlertStatus.resolved.value
                alert.resolution = f"{ep['resolve_date']} 客流恢复且完成率回到告警下限之上，连续下降中断，自动解除"
                alert.resolved_at = time.time()
            elif not ep['resolved'] and alert.status == AlertStatus.resolved.value:
                # 恢复日被更正为异常，原异常区间延续：重新生效（确认意见仍保留在该告警下）
                alert.status = AlertStatus.open.value
                alert.resolution = ''
                alert.resolved_at = None
    for alert in live:  # 更正后原连续下降不再成立 → 撤销，确认意见留在 AlertConfirmation
        if alert.streak_start not in ep_starts:
            alert.status = AlertStatus.revoked.value
            alert.epoch = monitor.epoch
            alert.resolution = (f"客流更正后重算 {rc.scope_start} 起受影响日期，原连续下降依据不再成立"
                                f"（修订 epoch {monitor.epoch}），撤销告警；原确认意见保留")
            alert.resolved_at = time.time()

def _session():
    return Session()


# ---------------------------------------------------------------- 人工确认
def confirm_alert(db, alert, opinion, user):
    if alert.status == AlertStatus.revoked.value:
        raise HTTPException(409, '该告警已随更正撤销，不能再确认；原确认意见仍保留可查')
    db.add(AlertConfirmation(alert_id=alert.id, monitor_id=alert.monitor_id,
                             opinion=opinion, actor=user.email, at=time.time()))
    db.flush()
