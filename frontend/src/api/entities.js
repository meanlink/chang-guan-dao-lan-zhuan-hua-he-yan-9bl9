import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const monitors={
 list:()=>api('/monitors'),
 get:id=>api('/monitors/'+id),
 create:data=>api('/monitors',{method:'POST',body:JSON.stringify(data)}),
 ingest:(id,data)=>api('/monitors/'+id+'/flows',{method:'POST',body:JSON.stringify(data)}),
 recompute:id=>api('/monitors/'+id+'/recompute',{method:'POST'}),
 alerts:id=>api('/monitors/'+id+'/alerts'),
 confirm:(alertId,opinion)=>api('/monitors/alerts/'+alertId+'/confirm',{method:'POST',body:JSON.stringify({opinion})})
};
