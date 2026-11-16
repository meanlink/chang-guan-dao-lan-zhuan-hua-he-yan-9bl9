from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Monitor, FlowRecord, DailyState, Alert, AlertConfirmation, Recompute
from src.schemas.monitor import MonitorInput, FlowInput, ConfirmationInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import monitoring
from src.services.audit import record
from src.utils.serialization import (monitor_dict, flow_dict, state_dict, alert_dict,
                                     confirmation_dict, recompute_dict)
router = APIRouter(prefix='/monitors')

@router.get('')
def list_monitors(db=Depends(get_db), u=Depends(current_user)):
    return [monitor_dict(m) for m in db.scalars(select(Monitor).order_by(Monitor.id.desc()))]

@router.post('', status_code=201)
def create_monitor(data: MonitorInput, db=Depends(get_db), u=Depends(can_write)):
    m, rc = monitoring.create_monitor(db, data, u)
    record(db, u.email, 'monitor.create', {'id': m.id, 'zone': m.zone, 'recompute': rc.id})
    db.commit()
    return monitor_dict(m)

@router.get('/{mid}')
def monitor_detail(mid: int, db=Depends(get_db), u=Depends(current_user)):
    m = db.get(Monitor, mid)
    if not m:
        raise HTTPException(404, '监测不存在')
    flows = [flow_dict(f) for f in db.scalars(select(FlowRecord).where(
        FlowRecord.monitor_id == mid).order_by(FlowRecord.log_date, FlowRecord.arm))]
    states = [state_dict(s) for s in db.scalars(select(DailyState).where(
        DailyState.monitor_id == mid).order_by(DailyState.log_date))]
    alerts = [alert_dict(a) for a in db.scalars(select(Alert).where(
        Alert.monitor_id == mid).order_by(Alert.id.desc()))]
    for a in alerts:
        a['confirmations'] = [confirmation_dict(c) for c in db.scalars(
            select(AlertConfirmation).where(AlertConfirmation.alert_id == a['id']).order_by(AlertConfirmation.id))]
    confirms = [confirmation_dict(c) for c in db.scalars(select(AlertConfirmation).where(
        AlertConfirmation.monitor_id == mid).order_by(AlertConfirmation.id.desc()))]
    recomputes = [recompute_dict(r) for r in db.scalars(select(Recompute).where(
        Recompute.monitor_id == mid).order_by(Recompute.id.desc()).limit(30))]
    return {**monitor_dict(m), 'flows': flows, 'states': states, 'alerts': alerts,
            'confirmations': confirms, 'recomputes': recomputes}

@router.post('/{mid}/flows', status_code=201)
def ingest(mid: int, data: FlowInput, db=Depends(get_db), u=Depends(can_write)):
    m = db.get(Monitor, mid)
    if not m:
        raise HTTPException(404, '监测不存在')
    action, rc = monitoring.ingest_flow(db, m, data, u)
    record(db, u.email, action, {'monitor': mid, 'date': data.log_date,
                                 'expected_revision': data.expected_revision, 'recompute': rc.id,
                                 'epoch': m.epoch})
    db.commit()
    return {'action': action, 'epoch': m.epoch, 'recompute_id': rc.id,
            'flows': [flow_dict(f) for f in db.scalars(select(FlowRecord).where(
                FlowRecord.monitor_id == mid, FlowRecord.log_date == data.log_date))]}

@router.post('/{mid}/recompute', status_code=202)
def manual_recompute(mid: int, db=Depends(get_db), u=Depends(can_write)):
    m = db.get(Monitor, mid)
    if not m:
        raise HTTPException(404, '监测不存在')
    rc = monitoring.enqueue_full_recompute(db, m, u.email)
    record(db, u.email, 'monitor.recompute', {'id': rc.id, 'monitor': mid, 'epoch': m.epoch})
    db.commit()
    return recompute_dict(rc)

@router.get('/{mid}/alerts')
def list_alerts(mid: int, db=Depends(get_db), u=Depends(current_user)):
    if not db.get(Monitor, mid):
        raise HTTPException(404, '监测不存在')
    rows = []
    for a in db.scalars(select(Alert).where(Alert.monitor_id == mid).order_by(Alert.id.desc())):
        d = alert_dict(a)
        d['confirmations'] = [confirmation_dict(c) for c in db.scalars(
            select(AlertConfirmation).where(AlertConfirmation.alert_id == a.id).order_by(AlertConfirmation.id))]
        rows.append(d)
    return rows

@router.post('/alerts/{aid}/confirm', status_code=201)
def confirm_alert(aid: int, data: ConfirmationInput, db=Depends(get_db), u=Depends(admin_only)):
    a = db.get(Alert, aid)
    if not a:
        raise HTTPException(404, '告警不存在')
    monitoring.confirm_alert(db, a, data.opinion, u)
    record(db, u.email, 'alert.confirm', {'alert': aid, 'monitor': a.monitor_id, 'opinion': data.opinion})
    db.commit()
    return alert_dict(a)
