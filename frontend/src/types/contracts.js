/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{id:number,name:string,zone:string,baseline_job_id:number,baseline_rate:number,baseline_arm_a:number,baseline_arm_b:number,baseline_snapshot:Object,drop_threshold:number,consecutive_limit:number,start_date:string,epoch:number,created_at:number}} Monitor */
/** @typedef {{id:number,monitor_id:number,log_date:string,arm:string,visitors:number,completed:number,revision:number,updated_by:string,updated_at:number}} FlowRecord */
/** @typedef {{id:number,log_date:string,status:string,rate:number|null,rate_a:number|null,rate_b:number|null,streak:number,reason:string,epoch:number}} DailyState */
/** @typedef {{id:number,monitor_id:number,status:string,streak_start:string,streak_end:string,consecutive_days:number,basis:string,resolution:string,epoch:number,created_at:number,resolved_at:number|null,confirmations?:Array}} Alert */
/** @typedef {{id:number,status:string,scope_start:string,scope_end:string,epoch:number,cursor_date:string|null,trigger:string,error:string}} Recompute */
export {};
