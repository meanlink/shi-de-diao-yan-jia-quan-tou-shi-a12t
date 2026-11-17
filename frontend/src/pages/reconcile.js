import {handovers} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {batchBadge, canReconcile, canConfirm} from '../components/common/batch.js';
import {ClaimState} from '../constants/enums.js';
import {esc,fmt,notify} from '../utils/dom.js';
export async function reconcilePage(id){
 const data=await handovers.rows(id);
 const r=data.reconciliation||{},rows=data.rows||[];
 if(!data.handover)return heading('02C / ROW RECONCILIATION',data.dataset.name,'旧批次无需对账，可直接进入分析工作台。')+'<div class="panel empty">该批次没有挂接交接清单。'+(canReconcile()?'<a href="#/datasets">到数据接入页挂接清单 ↗</a>':'')+'<a href="#/workbench">直接发起分析 ↗</a></div>';
 const claimable=canReconcile(), confirmable=canConfirm();
 const claimed=rows.filter(x=>x.claimed).length;
 return heading('02C / ROW RECONCILIATION',data.dataset.name,'逐行把日表批次认领到交接清单项。一项清单可分给多批，累计不超清单；剩余合格数不超剩余观察数。')
 +'<div class="recon-summary">'+batchBadge(r.status)
 +[['清单',data.handover?data.handover.name:'未挂接'],['日表行数',rows.length],['已认领行',claimed],['待处理',r.issue_count||0]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+esc(v)+'</strong></div>').join('')
 +'<div class="recon-actions">'+(r.status==='ready'&&confirmable?'<button class="primary small" id="confirm-batch">整批吻合，确认开放分析</button>':'')+(r.status==='confirmed'?'<span class="approval">已确认 · 分析开放</span>':'')+(r.status==='legacy'?'<span class="hint">旧批次无需对账，照常使用。</span>':'')+'</div></div>'
 +(!data.handover?'<section class="panel empty">该批次尚未挂接交接清单。请到数据接入页选择清单后认领。</section>':
  (r.status==='confirmed'?'<p class="hint">批次已确认；认领已冻结，撤销不再开放。可继续查看逐行依据。</p>':'<section class="panel"><div class="section-title"><h2>待认领行</h2><span>勾选后批量认领到对应清单项</span></div>'+claimTable(rows,claimable)+'<div class="claim-actions"><button class="primary" id="claim-selected" '+(claimable?'':'disabled')+'>认领所选行</button><button class="button" id="claim-auto" '+(claimable?'':'disabled')+'>按同键自动认领全部未认领行</button><span class="hint">认领量默认取日表行计数；清单余额不足时会指出具体缺口。</span></div></section>')
  +'<section class="panel"><div class="section-title"><h2>已认领与差异</h2><span>撤销只退原认领量</span></div>'+claimList(rows,claimable)+'</section>');
}
function claimTable(rows,claimable){
 const open=rows.filter(r=>!r.claimed);
 if(!open.length)return '<p class="empty">所有行都已认领。</p>';
 return '<div class="table-wrap"><table><thead><tr><th>选择</th><th>行号</th><th>观察日</th><th>采样队</th><th>区域</th><th>日表合格</th><th>日表观察</th><th>清单项</th><th>剩余合格/观察</th></tr></thead><tbody>'
  +open.map(r=>'<tr><td><input type="checkbox" class="claim-check" data-row="'+r.row_index+'" '+(r.item_match?'':'disabled')+'></td><td>'+r.row_number+'</td><td>'+esc(r.day)+'</td><td>'+esc(r.team)+'</td><td>'+esc(r.zone)+'</td><td>'+fmt(r.passed)+'</td><td>'+fmt(r.observed)+'</td><td>'+(r.item?'#'+r.item.id:'<span class="warn-text">无匹配清单项</span>')+'</td><td>'+(r.item?fmt(r.item.remaining_passed)+' / '+fmt(r.item.remaining_observed):'—')+'</td></tr>').join('')
  +'</tbody></table></div>'+(claimable?'':'<p class="hint">观察员只读，无法认领。</p>');
}
function claimList(rows,claimable){
 const claimed=rows.filter(r=>r.claimed);
 if(!claimed.length)return '<p class="empty">尚无认领记录。</p>';
 return '<div class="table-wrap"><table><thead><tr><th>行号</th><th>观察日</th><th>采样队</th><th>区域</th><th>认领合格</th><th>认领观察</th><th>合格差</th><th>观察差</th><th>状态</th><th>操作</th></tr></thead><tbody>'
  +claimed.map(r=>{const c=r.claim;return '<tr><td>'+r.row_number+'</td><td>'+esc(r.day)+'</td><td>'+esc(r.team)+'</td><td>'+esc(r.zone)+'</td><td>'+fmt(c?c.passed:'—')+'</td><td>'+fmt(c?c.observed:'—')+'</td><td>'+fmt(r.passed_difference)+'</td><td>'+fmt(r.observed_difference)+'</td><td>'+esc(c?(ClaimState[c.state]||c.state):'多条有效认领')+'</td><td>'+(claimable&&c&&c.state==='active'?'<button class="text-button" data-revoke="'+c.id+'">撤销</button>':'—')+'</td></tr>';}).join('')
  +'</tbody></table></div>';
}
export function bindReconcile(refresh,id){
 document.querySelector('#confirm-batch')?.addEventListener('click',async()=>{
   try{const key='confirm-'+id+'-'+Date.now();const out=await handovers.confirm(id,key);notify(out.already_confirmed?'已确认过，未重复扣减':'整批吻合，分析已开放');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelector('#claim-selected')?.addEventListener('click',async()=>{
   const lines=[...document.querySelectorAll('.claim-check:checked')].map(c=>({row_index:Number(c.dataset.row)}));
   await runClaim(id,lines,'认领所选行',refresh);
 });
 document.querySelector('#claim-auto')?.addEventListener('click',async()=>{
   const data=await handovers.rows(id);
   const lines=(data.rows||[]).filter(r=>!r.claimed&&r.item_match).map(r=>({row_index:r.row_index}));
   await runClaim(id,lines,'自动认领',refresh);
 });
 document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{
   try{await handovers.revoke(b.dataset.revoke);notify('已撤销，仅退还原认领量');await refresh();}catch(e){notify(e.message,true);}
 });
}
async function runClaim(id,lines,label,refresh){
 if(!lines.length){notify('没有可认领的行');return;}
 try{const key=label+'-'+id+'-'+Date.now();const out=await handovers.claim(id,lines,key);notify((out.replayed?'重试命中已有认领：':'')+label+' '+out.claims.length+' 行');await refresh();}catch(e){notify(e.message,true);}
}
