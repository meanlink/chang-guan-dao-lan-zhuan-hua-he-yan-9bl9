import {monitors,jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {DailyStatus,AlertStatus,RecomputeStatus} from '../constants/enums.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const canWrite=()=>session.user.role==='admin'||session.user.role==='analyst';
const pill=(map,v)=>'<span class="pill '+esc(v)+'">'+esc(map[v]||v)+'</span>';
const zoneOptions=()=>(workspace.meta.parameters.find(p=>p.key==='zone')?.options||[]).map(o=>o.value).filter(v=>v!=='全部');
const todayISO=()=>new Date().toISOString().slice(0,10);

export async function monitorPage(){
 const [list,allJobs]=await Promise.all([monitors.list(),jobs.list()]);
 workspace.monitors=list;workspace.jobs=allJobs;
 const approved=allJobs.filter(j=>j.approved&&j.status==='completed');
 const selected=list.find(m=>m.id===workspace.selectedMonitor)||list[0]||null;
 workspace.selectedMonitor=selected?.id??null;
 const detail=selected?await monitors.get(selected.id):null;
 return heading('06 / MONITORING','展区完成率持续监测','以获批核验固定为基准，按下降门槛与连续异常天数逐日评估 A/B 客流；缺报不视作正常，连续下降才开告警，更正重算后撤销失去依据的告警并保留确认意见。')
 +'<div class="monitor-grid"><section class="panel">'
 +'<div class="section-title"><h2>监测登记</h2><span>'+list.length+' 个展区</span></div>'
 +'<div class="batch-list">'+(list.length?list.map(m=>'<button class="batch-item'+(m.id===workspace.selectedMonitor?' active':'')+'" data-pick="'+m.id+'"><strong>'+esc(m.name)+'</strong><span>'+esc(m.zone)+' · 基准 '+fmt(m.baseline_rate)+'% · 门槛 '+fmt(m.drop_threshold)+'pp · 连续 '+m.consecutive_limit+' 天</span><small>修订围栏 epoch '+m.epoch+' · 起始 '+esc(m.start_date)+'</small></button>').join(''):'<p class="empty">尚未登记监测。</p>')+'</div>'
 +'<form id="monitor-form"><div class="section-title"><h2>登记新监测</h2></div>'
 +'<label>监测名称<input name="name" required minlength="2" placeholder="例如：水世界完成率持续监测"></label>'
 +'<label>展区<select name="zone" required>'+zoneOptions().map(z=>'<option>'+esc(z)+'</option>').join('')+'</select></label>'
 +'<label>固定基准（已获批核验）<select name="baseline_job_id" required '+(approved.length?'':'disabled')+'>'+(approved.length?approved.map(j=>'<option value="'+j.id+'">'+esc(j.name)+'</option>').join(''):'<option>暂无已获批核验，请先在结果复核中批准</option>')+'</select></label>'
 +'<div class="parameter-grid"><label>下降门槛（百分点）<input name="drop_threshold" type="number" step="0.1" min="0.1" max="100" value="5" required></label>'
 +'<label>连续异常天数<input name="consecutive_limit" type="number" step="1" min="2" max="30" value="3" required></label></div>'
 +'<label>监测起始日<input name="start_date" type="date" max="'+todayISO()+'" value="'+todayISO()+'" required></label>'
 +'<button type="submit" class="primary" '+(canWrite()&&approved.length?'':'disabled')+'>登记并建立基准</button>'
 +'<p class="hint">基准快照取自获批核验的分区 A/B 完成率，登记后固定，不随后续客流更正改变。观察员只读。</p></form></section>'
 +(detail?detailPanel(detail):'<section class="panel empty">选择或登记一个监测后查看逐日状态、告警与修订依据。</section>')+'</div>';
}

function detailPanel(d){
 const floor=d.baseline_rate-d.drop_threshold;
 const lastDate=d.states.length?d.states[d.states.length-1].log_date:todayISO();
 const stateCols=[{key:'log_date',label:'日期'},{key:'status',label:'状态'},{key:'rate_a',label:'A 完成率%'},{key:'rate_b',label:'B 完成率%'},{key:'rate',label:'合计完成率%'},{key:'streak',label:'连续异常'},{key:'epoch',label:'修订'},{key:'reason',label:'判定依据'}];
 const stateRows=d.states.slice().reverse().map(s=>({...s,status:DailyStatus[s.status]||s.status,rate_a:s.rate_a??'—',rate_b:s.rate_b??'—',rate:s.rate??'缺报'}));
 return '<section><div class="panel monitor-head"><div><p class="eyebrow">FIXED BASELINE / '+esc(d.zone)+'</p><h2>'+esc(d.name)+'</h2>'
 +'<p class="hint">基准 A '+fmt(d.baseline_arm_a)+'% · B '+fmt(d.baseline_arm_b)+'% · 合计 '+fmt(d.baseline_rate)+'% ；告警下限 = 基准 − '+fmt(d.drop_threshold)+'pp = <strong>'+fmt(floor)+'%</strong> ；连续 '+d.consecutive_limit+' 个异常日（含缺报）才开告警。</p>'
 +'<p class="hint">基准来源：'+esc(d.baseline_snapshot.job_name||('核验 #'+d.baseline_job_id))+'（快照固定，epoch '+d.epoch+'）</p></div>'
 +'<button class="button small" data-recompute="'+d.id+'" '+(canWrite()?'':'disabled')+'>手动重算 ↻</button></div>'
 +'<div class="two-columns"><section class="panel"><div class="section-title"><h2>每日客流接入 · 补报 / 更正</h2><span>A/B 必须同报</span></div>'
 +'<form id="flow-form"><label>客流日期<input name="log_date" type="date" min="'+d.start_date+'" max="'+todayISO()+'" value="'+esc(lastDate)+'" required></label>'
 +'<div id="revision-hint" class="warning" style="display:none"></div>'
 +'<div class="parameter-grid"><label>A 方案进区<input name="va" type="number" min="1" step="1" required></label><label>A 完成讲解<input name="ca" type="number" min="0" step="1" required></label></div>'
 +'<div class="parameter-grid"><label>B 方案进区<input name="vb" type="number" min="1" step="1" required></label><label>B 完成讲解<input name="cb" type="number" min="0" step="1" required></label></div>'
 +'<input type="hidden" name="expected_revision" value="">'
 +'<button type="submit" class="primary" '+(canWrite()?'':'disabled')+'>提交当日客流</button>'
 +'<p class="hint">该日无数据时为<b>补报</b>；已有数据时按所见修订号提交<b>更正</b>，修订号过期将整笔拒绝。缺报任意方案当日即判异常。</p></form></section>'
 +'<section class="panel"><div class="section-title"><h2>重算与修订追踪</h2><span>受影响日期全部完成才发布</span></div>'
 +(d.recomputes.length?'<div class="recompute-list">'+d.recomputes.map(r=>'<article><strong>'+esc(RecomputeStatus[r.status]||r.status)+'</strong><p>'+esc(r.scope_start)+' → '+esc(r.scope_end)+' · 游标 '+(r.cursor_date?esc(r.cursor_date):'—')+'</p><small>epoch '+r.epoch+' · '+esc(r.trigger)+(r.error?' · '+esc(r.error):'')+'</small></article>').join('')+'</div>':'<p class="empty">尚无重算任务。</p>')+'</section></div>'
 +'<section class="panel"><div class="section-title"><h2>逐日完成率状态</h2><span>'+d.states.length+' 天 · 缺报不视作正常</span></div>'
 +table(stateCols,stateRows,'暂无已发布状态，等待重算完成。')+'</section>'
 +'<section class="panel"><div class="section-title"><h2>告警与确认追查</h2><span>生效依据 / 解除或撤销依据</span></div>'
 +(d.alerts.length?d.alerts.map(a=>alertCard(a,d.id)).join(''):'<p class="empty">尚未形成连续异常，无告警。</p>')+'</section></section>';
}

function alertCard(a,monitorId){
 const confirms=a.confirmations||[];
 return '<article class="alert-card '+esc(a.status)+'"><div class="alert-top"><div>'+pill(AlertStatus,a.status)+'<strong>'+esc(a.streak_start)+' 至 '+esc(a.streak_end)+'（连续 '+a.consecutive_days+' 天 · epoch '+a.epoch+'）</strong></div>'
 +(a.status==='open'&&session.user.role==='admin'?'<button class="button small" data-confirm="'+a.id+'" data-monitor="'+monitorId+'">登记确认意见</button>':'')+'</div>'
 +'<p class="basis"><b>生效依据：</b>'+esc(a.basis)+'</p>'
 +(a.resolution?'<p class="basis resolve"><b>'+(a.status==='revoked'?'撤销依据：':'解除依据：')+'</b>'+esc(a.resolution)+(a.resolved_at?' · '+date(a.resolved_at):'')+'</p>':'')
 +(confirms.length?'<div class="confirm-list">'+confirms.map(c=>'<div class="confirmation"><strong>确认意见 · '+esc(c.actor)+'</strong><span>'+date(c.at)+'</span><p>'+esc(c.opinion)+'</p></div>').join('')+'</div>':'<p class="hint">暂无确认意见。告警若被更正撤销，已有确认意见仍在此保留。</p>')+'</article>';
}

export function bindMonitors(refresh){
 document.querySelectorAll('[data-pick]').forEach(b=>b.onclick=()=>{workspace.selectedMonitor=Number(b.dataset.pick);refresh();});
 document.querySelector('[data-recompute]')?.addEventListener('click',async e=>{try{await monitors.recompute(e.currentTarget.dataset.recompute);notify('已发起重算，后台处理完成后刷新查看');setTimeout(refresh,1200);}catch(err){notify(err.message,true);}});
 const mf=document.querySelector('#monitor-form');
 if(mf)submit(mf,async f=>{await monitors.create({name:f.get('name'),zone:f.get('zone'),baseline_job_id:Number(f.get('baseline_job_id')),drop_threshold:Number(f.get('drop_threshold')),consecutive_limit:Number(f.get('consecutive_limit')),start_date:f.get('start_date')});notify('监测已登记，基准已固定，正在评估起始日以来状态');await refresh();});
 const ff=document.querySelector('#flow-form');
 if(!ff)return;
 const detail=workspace.monitors.find(m=>m.id===workspace.selectedMonitor);
 const syncRevision=async()=>{
   const d=await monitors.get(detail.id);workspace.monitors=workspace.monitors.map(x=>x.id===d.id?{...x,epoch:d.epoch}:x);
   const day=ff.log_date.value,by={};d.flows.forEach(f=>{if(f.log_date===day)by[f.arm]=f;});
   const hint=document.querySelector('#revision-hint');
   if(by.A&&by.B){
     ff.expected_revision.value=by.A.revision;
     hint.style.display='block';hint.textContent='该日已有客流（修订号 '+by.A.revision+'），本次提交为更正，将生成修订号 '+(by.A.revision+1)+'；并发修订号不符会整笔拒绝。';
     ff.va.value=by.A.visitors;ff.ca.value=by.A.completed;ff.vb.value=by.B.visitors;ff.cb.value=by.B.completed;
   }else{
     ff.expected_revision.value='';hint.style.display='none';
     ['va','ca','vb','cb'].forEach(k=>{if(!by[k[0].toUpperCase()])ff[k].value='';});
     if(Object.keys(by).length)hint.style.display='block',hint.textContent='该日仅接入部分方案，属缺报异常；补报将补齐 A/B 后重新评估。';
   }
 };
 ff.log_date.addEventListener('change',syncRevision);syncRevision();
 submit(ff,async f=>{
   const payload={log_date:f.get('log_date'),expected_revision:f.get('expected_revision')?Number(f.get('expected_revision')):null,
     items:[{arm:'A',visitors:Number(f.get('va')),completed:Number(f.get('ca'))},{arm:'B',visitors:Number(f.get('vb')),completed:Number(f.get('cb'))}]};
   if(payload.items.some(i=>i.completed>i.visitors))throw new Error('完成讲解人数不能超过进区人数');
   const res=await monitors.ingest(detail.id,payload);
   notify((res.action==='flow.backfill'?'补报已写入':'更正已写入（新修订 epoch '+res.epoch+'）')+'，受影响日期重算完成后发布新状态');
   setTimeout(refresh,1200);
 });
 document.querySelectorAll('[data-confirm]').forEach(b=>b.onclick=async()=>{
   const opinion=prompt('登记确认意见（撤销后仍保留）：');
   if(opinion===null)return;
   if(opinion.trim().length<2){notify('确认意见至少两个字',true);return;}
   try{await monitors.confirm(b.dataset.confirm,opinion.trim());notify('确认意见已登记并随告警留痕');await refresh();}catch(e){notify(e.message,true);}
 });
}
