import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, Recompute
from src.services.business import analyze
from src.services.reports import deliver
from src.services.monitoring import run_recompute, RECOMPUTE_QUEUE
from src.services.audit import record
logging.basicConfig(level=logging.INFO)
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def run_recompute_with_audit(rc_id):
    before=None
    with Session() as db:
        before=db.get(Recompute,rc_id)
        prior=before.status if before else None
    run_recompute(rc_id)  # 内部自带会话与围栏，异常向上抛由主循环重试
    with Session() as db:
        after=db.get(Recompute,rc_id)
        if after and prior in ('queued','running') and after.status in ('completed','superseded'):
            record(db,'worker','recompute.'+after.status,{'id':rc_id,'monitor':after.monitor_id,'epoch':after.epoch})
            db.commit()
def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    with Session() as db:
        # SQL 兜底：Redis 丢失或 worker 崩溃后，排队/中断的重算仍会被捞起；不重复开告警（幂等对账）
        due=list(db.scalars(select(Recompute.id).where(Recompute.status.in_(['queued','running'])).order_by(Recompute.id).limit(2)))
    for rc_id in due:
        try: run_recompute_with_audit(rc_id)
        except Exception: logging.exception('Recompute retry for %s',rc_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
        db.commit()
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        # running 重算保留状态：游标已持久化，tick 会从断点继续直至全部受影响日期处理完
        for rc in db.scalars(select(Recompute).where(Recompute.status=='running')):
            record(db,'worker','recompute.recovered',{'id':rc.id,'monitor':rc.monitor_id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            rc_item=r.blpop(RECOMPUTE_QUEUE,timeout=1)
            if rc_item:
                try: run_recompute_with_audit(int(rc_item[1]))
                except Exception: logging.exception('Recompute failed; queued fallback will retry')
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
