import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}
def monitor_dict(m):
    return {'id':m.id,'name':m.name,'zone':m.zone,'baseline_job_id':m.baseline_job_id,
            'baseline_rate':m.baseline_rate,'baseline_arm_a':m.baseline_arm_a,'baseline_arm_b':m.baseline_arm_b,
            'baseline_snapshot':json.loads(m.baseline_snapshot or '{}'),'drop_threshold':m.drop_threshold,
            'consecutive_limit':m.consecutive_limit,'start_date':m.start_date,'epoch':m.epoch,
            'created_by':m.created_by,'created_at':m.created_at}
def flow_dict(f):
    return {'id':f.id,'monitor_id':f.monitor_id,'log_date':f.log_date,'arm':f.arm,
            'visitors':f.visitors,'completed':f.completed,'revision':f.revision,
            'updated_by':f.updated_by,'updated_at':f.updated_at}
def state_dict(s):
    return {'id':s.id,'monitor_id':s.monitor_id,'log_date':s.log_date,'status':s.status,
            'rate':s.rate,'rate_a':s.rate_a,'rate_b':s.rate_b,'streak':s.streak,
            'reason':s.reason,'epoch':s.epoch,'published_at':s.published_at}
def alert_dict(a):
    return {'id':a.id,'monitor_id':a.monitor_id,'status':a.status,'streak_start':a.streak_start,
            'streak_end':a.streak_end,'consecutive_days':a.consecutive_days,'basis':a.basis,
            'resolution':a.resolution,'epoch':a.epoch,'created_at':a.created_at,'resolved_at':a.resolved_at}
def confirmation_dict(c):
    return {'id':c.id,'alert_id':c.alert_id,'monitor_id':c.monitor_id,'opinion':c.opinion,
            'actor':c.actor,'at':c.at}
def recompute_dict(r):
    return {'id':r.id,'monitor_id':r.monitor_id,'status':r.status,'scope_start':r.scope_start,
            'scope_end':r.scope_end,'epoch':r.epoch,'cursor_date':r.cursor_date,'trigger':r.trigger,
            'error':r.error,'created_by':r.created_by,'created_at':r.created_at,'finished_at':r.finished_at}

