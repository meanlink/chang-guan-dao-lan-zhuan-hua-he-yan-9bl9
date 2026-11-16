"""监测功能全链路集成测试（SQLite 内存库，无需 Postgres/Redis）。

重算统一调用 run_recompute_sync 驱动，覆盖：
监测登记固定基准、补报、并发更正乐观裁决、过期整笔拒绝、缺报异常、
连续下降开告警/恢复解除/再告警、更正撤销且确认意见保留、epoch 围栏与旧执行作废、权限。
"""
import os, time, pathlib
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'   # 本机无 Redis：入队失败被吞，SQL 状态兜底
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['ARTIFACT_DIR'] = str(pathlib.Path(__file__).resolve().parent / '.artifacts')

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from httpx import ASGITransport, AsyncClient

import src.config.database as dbmod
engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                       poolclass=StaticPool)
dbmod.engine = engine
dbmod.Session.configure(bind=engine)

from src.config.database import Base, Session
import src.models as _models  # 导入即注册全部 ORM 表
from src.models import User, Job, Monitor, FlowRecord, DailyState, Alert, AlertConfirmation, Recompute
from src.utils.security import hash_password, token_for
from src.services.monitoring import run_recompute_sync

assert _models is not None
Base.metadata.create_all(engine)

ZONE = '水世界'
def make_baseline_job(rate_a=60.0, rate_b=64.0):
    va, ca = 200, round(200 * rate_a / 100)
    vb, cb = 200, round(200 * rate_b / 100)
    result = {'table': {'rows': [
        {'zone': ZONE, 'arm': 'A', 'visitors': va, 'completed': ca, 'rate': rate_a},
        {'zone': ZONE, 'arm': 'B', 'visitors': vb, 'completed': cb, 'rate': rate_b},
        {'zone': '星空厅', 'arm': 'A', 'visitors': 10, 'completed': 4, 'rate': 40.0},
        {'zone': '星空厅', 'arm': 'B', 'visitors': 10, 'completed': 4, 'rate': 40.0}]}}
    with Session() as db:
        u = db.scalar(__import__('sqlalchemy').select(User).where(User.role == 'admin'))
        j = Job(name='基线核验', dataset_id=1, params={}, status='completed', result=result,
                approved=True, created_by=u.id, created_at=time.time())
        db.add(j); db.commit()
        return j.id

@pytest.fixture(scope='session')
def anyio_backend():
    return 'asyncio'

@pytest.fixture(scope='session', autouse=True)
def users():
    with Session() as db:
        for role, name in [('admin', '负责人'), ('analyst', '分析员'), ('viewer', '观察员')]:
            db.add(User(email=f'{role}@t.local', name=name, role=role, password=hash_password('pw123456')))
        db.commit()

@pytest.fixture
def client():
    from src.main import app
    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url='http://t')
    yield c
    # 每个测试隔离：清理监测相关数据（保留演示用户）
    with Session() as db:
        for model in (AlertConfirmation, Alert, DailyState, FlowRecord, Recompute, Monitor, Job):
            db.query(model).delete()
        db.commit()

def auth(role):
    with Session() as db:
        u = db.scalar(__import__('sqlalchemy').select(User).where(User.email == f'{role}@t.local'))
        return {'Authorization': f'Bearer {token_for(u)}'}

def drain(monitor_id):
    for _ in range(10):
        with Session() as db:
            rcs = db.query(Recompute).filter(
                Recompute.monitor_id == monitor_id,
                Recompute.status.in_(['queued', 'running'])).all()
            ids = [r.id for r in rcs]
        if not ids:
            break
        for rid in ids:
            run_recompute_sync(rid)

async def register(client, start='2026-11-01', threshold=10.0, limit=3, role='admin'):
    job_id = make_baseline_job()
    return await client.post('/monitors', headers=auth(role), json={
        'name': f'{ZONE}监测-{time.time_ns()}', 'zone': ZONE, 'baseline_job_id': job_id,
        'drop_threshold': threshold, 'consecutive_limit': limit, 'start_date': start}), job_id

def ingest(client, mid, ds, va, ca, vb, cb, rev=None, role='analyst'):
    return client.post(f'/monitors/{mid}/flows', headers=auth(role), json={
        'log_date': ds, 'expected_revision': rev,
        'items': [{'arm': 'A', 'visitors': va, 'completed': ca},
                  {'arm': 'B', 'visitors': vb, 'completed': cb}]})

def states(mid):
    with Session() as db:
        return {s.log_date: s for s in db.query(DailyState).filter_by(monitor_id=mid).order_by(DailyState.log_date)}

@pytest.mark.anyio
async def test_register_requires_approved_baseline_and_fixes_it(client):
    # 未批准的核验不能作为基准
    with Session() as db:
        u = db.scalar(__import__('sqlalchemy').select(User).where(User.role == 'admin'))
        j = Job(name='未批准', dataset_id=1, params={}, status='completed', result={'table': {'rows': []}},
                approved=False, created_by=u.id, created_at=time.time())
        db.add(j); db.commit(); bad = j.id
    r = await client.post('/monitors', headers=auth('admin'), json={
        'name': '坏基准监测', 'zone': ZONE, 'baseline_job_id': bad,
        'drop_threshold': 5, 'consecutive_limit': 3, 'start_date': '2026-11-01'})
    assert r.status_code == 409
    r, job_id = await register(client)
    assert r.status_code == 201, r.text
    mid = r.json()['id']
    drain(mid)
    with Session() as db:
        m = db.get(Monitor, mid)
        assert abs(m.baseline_rate - 62.0) < 0.01   # (120+128)/400
        assert m.epoch == 1
    # 基准不随后续更正改变（断言快照字段已固定）
    assert r.json()['baseline_snapshot']['job_id'] == job_id

@pytest.mark.anyio
async def test_backfill_then_missing_then_alert_recovery_realert(client):
    r, _ = await register(client, start='2026-11-01')
    mid = r.json()['id']
    # 11-01、11-02 两天下降（合计完成率 30% < 下限 52%）
    for ds in ('2026-11-01', '2026-11-02'):
        rr = await ingest(client, mid, ds, 100, 30, 100, 30)
        assert rr.status_code == 201 and rr.json()['action'] == 'flow.backfill'
        drain(mid)
    assert not db_query_alerts(mid)  # 仅 2 日，不开告警
    # 11-03 缺报：不补数据，直接重算 → missing 且连续 3 日 → 开告警
    with Session() as db:
        m = db.get(Monitor, mid)
        from src.services.monitoring import _open_recompute
        rc = _open_recompute(db, m, '2026-11-03', '2026-11-03', 'manual.retry', 'tester')
        db.commit(); rid = rc.id
    run_recompute_sync(rid)
    alerts = db_query_alerts(mid)
    assert len(alerts) == 1 and alerts[0].status == 'open'
    assert states(mid)['2026-11-03'].status == 'missing'
    # 11-04 恢复 → 自动解除
    rr = await ingest(client, mid, '2026-11-04', 100, 70, 100, 70)
    drain(mid)
    a = db_query_alerts(mid)[0]
    assert a.status == 'resolved' and '恢复' in a.resolution
    # 11-05..07 再次连续三日下降 → 重新开一条告警
    for ds in ('2026-11-05', '2026-11-06', '2026-11-07'):
        await ingest(client, mid, ds, 100, 25, 100, 25)
        drain(mid)
    assert len(db_query_alerts(mid)) == 2
    assert db_query_alerts(mid)[-1].streak_start == '2026-11-05'

@pytest.mark.anyio
async def test_concurrent_correction_optimistic_rejects_stale_whole_batch(client):
    r, _ = await register(client, start='2026-11-01')
    mid = r.json()['id']
    await ingest(client, mid, '2026-11-01', 100, 30, 100, 30)
    drain(mid)
    # 第一次更正（所见修订号 1）成功；登记 epoch=1、首日报入 epoch=2，更正后 epoch=3
    r1 = await ingest(client, mid, '2026-11-01', 100, 40, 100, 40, rev=1)
    assert r1.status_code == 201 and r1.json()['epoch'] == 3
    drain(mid)
    # 并发的旧修订（仍带 1）整笔拒绝，数据不被覆盖
    r2 = await ingest(client, mid, '2026-11-01', 100, 99, 100, 99, rev=1)
    assert r2.status_code == 409 and '整笔拒绝' in r2.json()['detail']
    with Session() as db:
        fs = db.query(FlowRecord).filter_by(monitor_id=mid, log_date='2026-11-01').all()
        assert {f.revision for f in fs} == {2}
        assert {f.completed for f in fs} == {40}
    # 补报撞车：无修订号提交已存在日期 → 409，要求走更正
    r3 = await ingest(client, mid, '2026-11-01', 100, 50, 100, 50)
    assert r3.status_code == 409

@pytest.mark.anyio
async def test_correction_revokes_alert_but_keeps_confirmation(client):
    r, _ = await register(client, start='2026-11-01')
    mid = r.json()['id']
    for ds in ('2026-11-01', '2026-11-02', '2026-11-03'):
        await ingest(client, mid, ds, 100, 20, 100, 20)  # 20% 远低于 52% 下限
        drain(mid)
    alert = db_query_alerts(mid)[0]
    assert alert.status == 'open'
    # 负责人登记确认意见
    rc = await client.post(f'/monitors/alerts/{alert.id}/confirm', headers=auth('admin'),
                           json={'opinion': '已现场核查，讲解设备故障，持续跟进。'})
    assert rc.status_code == 201
    # 更正 11-02 恢复正常 → 连续下降断裂，原告警失去依据被撤销
    rr = await ingest(client, mid, '2026-11-02', 100, 80, 100, 80, rev=1)
    assert rr.status_code == 201
    drain(mid)
    with Session() as db:
        a = db.get(Alert, alert.id)
        assert a.status == 'revoked' and '撤销' in a.resolution
        conf = db.query(AlertConfirmation).filter_by(alert_id=alert.id).one()
        assert '讲解设备故障' in conf.opinion  # 原确认意见保留
    # 已撤销告警不能再确认
    again = await client.post(f'/monitors/alerts/{alert.id}/confirm', headers=auth('admin'),
                              json={'opinion': '试图补充意见'})
    assert again.status_code == 409

@pytest.mark.anyio
async def test_epoch_fence_supersedes_stale_execution(client):
    r, _ = await register(client, start='2026-11-01')
    mid = r.json()['id']
    await ingest(client, mid, '2026-11-01', 100, 30, 100, 30)
    with Session() as db:
        m = db.get(Monitor, mid)
        from src.services.monitoring import _open_recompute
        stale = _open_recompute(db, m, '2026-11-01', '2026-11-01', 'manual.retry', 'old')
        db.commit(); stale_id = stale.id; stale_epoch = stale.epoch
    # 新更正把 epoch 推进后，旧重算晚回 → superseded，不发布任何状态
    await ingest(client, mid, '2026-11-01', 100, 35, 100, 35, rev=1)
    run_recompute_sync(stale_id)
    with Session() as db:
        assert db.get(Recompute, stale_id).status == 'superseded'
        assert db.query(DailyState).filter_by(monitor_id=mid, epoch=stale_epoch).count() == 0
        m = db.get(Monitor, mid)
        assert m.epoch == 3  # 登记 1 → 首日报入 2 → 更正 3
    # 最新重算正常完成
    drain(mid)
    with Session() as db:
        assert db.query(Recompute).filter_by(monitor_id=mid, status='completed').count() >= 1
        assert db.query(DailyState).filter_by(monitor_id=mid).count() >= 1

@pytest.mark.anyio
async def test_recompute_retry_is_idempotent_no_duplicate_alerts(client):
    r, _ = await register(client, start='2026-11-01')
    mid = r.json()['id']
    for ds in ('2026-11-01', '2026-11-02', '2026-11-03'):
        await ingest(client, mid, ds, 100, 20, 100, 20)
        drain(mid)
    assert len(db_query_alerts(mid)) == 1
    # 手动再发起两次全窗口重算（模拟重试），告警不重复
    for _ in range(2):
        rr = await client.post(f'/monitors/{mid}/recompute', headers=auth('analyst'))
        assert rr.status_code == 202
        drain(mid)
    assert len(db_query_alerts(mid)) == 1

@pytest.mark.anyio
async def test_validation_and_permissions(client):
    r, _ = await register(client, role='analyst')   # analyst 可登记
    assert r.status_code == 201
    mid = r.json()['id']
    # viewer 只读
    rr = await client.post(f'/monitors/{mid}/flows', headers=auth('viewer'), json={
        'log_date': '2026-11-01', 'expected_revision': None,
        'items': [{'arm': 'A', 'visitors': 100, 'completed': 30}, {'arm': 'B', 'visitors': 100, 'completed': 30}]})
    assert rr.status_code == 403
    # 非法计数：完成 > 进区
    bad = await client.post(f'/monitors/{mid}/flows', headers=auth('analyst'), json={
        'log_date': '2026-11-01', 'expected_revision': None,
        'items': [{'arm': 'A', 'visitors': 10, 'completed': 130}, {'arm': 'B', 'visitors': 10, 'completed': 3}]})
    assert bad.status_code in (422,)
    # 缺一个方案 → 422
    only_a = await client.post(f'/monitors/{mid}/flows', headers=auth('analyst'), json={
        'log_date': '2026-11-01', 'expected_revision': None,
        'items': [{'arm': 'A', 'visitors': 100, 'completed': 30}, {'arm': 'A', 'visitors': 100, 'completed': 30}]})
    assert only_a.status_code == 422
    # 确认告警仅 admin
    for ds in ('2026-11-01', '2026-11-02', '2026-11-03'):
        await ingest(client, mid, ds, 100, 20, 100, 20)
        drain(mid)
    aid = db_query_alerts(mid)[0].id
    forbid = await client.post(f'/monitors/alerts/{aid}/confirm', headers=auth('analyst'), json={'opinion': '分析员意见'})
    assert forbid.status_code == 403
    ok = await client.post(f'/monitors/alerts/{aid}/confirm', headers=auth('admin'), json={'opinion': '负责人确认'})
    assert ok.status_code == 201

@pytest.mark.anyio
async def test_correction_of_recovery_day_reopens_resolved_alert(client):
    r, _ = await register(client, start='2026-11-01')
    mid = r.json()['id']
    for ds in ('2026-11-01', '2026-11-02', '2026-11-03'):
        await ingest(client, mid, ds, 100, 20, 100, 20)   # 连续三日下降 → 开告警
        drain(mid)
    await ingest(client, mid, '2026-11-04', 100, 70, 100, 70)  # 恢复 → 解除
    drain(mid)
    alerts = db_query_alerts(mid)
    assert len(alerts) == 1 and alerts[0].status == 'resolved'
    alert_id = alerts[0].id
    # 更正恢复日 11-04 为下降：原异常区间延续，同一告警重新生效而非新开一条
    rr = await ingest(client, mid, '2026-11-04', 100, 20, 100, 20, rev=1)
    assert rr.status_code == 201
    drain(mid)
    again = db_query_alerts(mid)
    assert len(again) == 1 and again[0].id == alert_id and again[0].status == 'open'
    assert again[0].resolution == '' and again[0].resolved_at is None

def db_query_alerts(mid):
    with Session() as db:
        return db.query(Alert).filter_by(monitor_id=mid).order_by(Alert.id).all()
