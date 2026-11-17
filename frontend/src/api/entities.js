import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const handovers={
 list:()=>api('/handovers'),
 get:id=>api('/handovers/'+id),
 reconciliation:id=>api('/handovers/'+id+'/reconciliation'),
 create:data=>api('/handovers',{method:'POST',body:JSON.stringify(data)}),
 attach:(datasetId,handoverId)=>api('/datasets/'+datasetId+'/handover',{method:'POST',body:JSON.stringify({handover_id:handoverId})}),
 batch:id=>api('/datasets/'+id+'/reconciliation'),
 rows:id=>api('/datasets/'+id+'/reconciliation/rows'),
 claim:(id,lines,key)=>api('/datasets/'+id+'/claims',{method:'POST',body:JSON.stringify({lines,idempotency_key:key})}),
 revoke:id=>api('/claims/'+id+'/revoke',{method:'POST'}),
 confirm:(id,key)=>api('/datasets/'+id+'/confirm',{method:'POST',body:JSON.stringify({idempotency_key:key})}),
 statuses:()=>api('/batches/status')};
