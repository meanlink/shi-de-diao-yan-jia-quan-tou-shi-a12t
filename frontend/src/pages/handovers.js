import {handovers} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {batchBadge, canReconcile} from '../components/common/batch.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
export async function handoverPage(id){
 const lists=await handovers.list();
 if(id)return detail(id,lists);
 const totals=lists.reduce((a,h)=>({passed:a.passed+h.totals.passed,observed:a.observed+h.totals.observed,claimed:a.claimed+h.totals.claimed_observed}),{passed:0,observed:0,claimed:0});
 return heading('02B / HANDOVER RECONCILIATION','纸质交接清单与日表对账','先把纸质清单逐项登记，再把日表批次逐行认领到清单项。累计认领不超清单，剩余合格不超剩余观察；整批吻合才开放分析。','<button class="button" id="refresh-handovers">刷新对账 ↻</button>')
 +'<section class="overview-hero recon-hero"><div class="narrative"><p class="eyebrow">当前对账口径</p><h2>清单是账，日表是凭证；一项清单可分给多批，但总量与行内计数必须对得上。</h2><p>缺项、余量与计数差异会逐项列出；撤销只退原认领量，已用于分析的认领冻结。</p></div><div class="numbers">'+[['清单总数',lists.length],['清单合格数',fmt(totals.passed)],['清单观察数',fmt(totals.observed)],['已认领观察数',fmt(totals.claimed)]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+v+'</strong></div>').join('')+'</div></section>'
 +'<div class="recon-grid"><section class="panel"><div class="section-title"><h2>已登记清单</h2><span>'+lists.length+' 份</span></div>'+(lists.length?'<div class="handover-list">'+lists.map(h=>'<a class="handover-row" href="#/handovers/'+h.id+'"><div><strong>'+esc(h.name)+'</strong><p>'+esc(h.source)+' · '+h.item_count+' 项 · '+date(h.created_at)+'</p></div><div class="handover-figures"><span>缺项 '+h.missing_count+'</span><span>余量 '+h.remainder_count+'</span><span>差异 '+h.difference_count+'</span>'+batchBadge(h.status)+'</div></a>').join('')+'</div>':'<p class="empty">尚无交接清单。先按日/队/区域登记纸质账目。</p>')+'</section>'
 +'<section class="panel"><div class="section-title"><h2>登记新清单</h2><span>逐行 JSON</span></div>'+listForm()+'</section></div>'
 +'<section class="panel"><div class="section-title"><h2>批次对账总览</h2><span>待对账 · 待补齐 · 已确认 · 清单去向</span></div><div id="batch-status-list">'+await batchStatusTable()+'</div></section>';
}
async function batchStatusTable(){
 const data=await handovers.statuses();
 return '<div class="status-strip">'+Object.entries(data.counts).map(([k,v])=>'<span>'+batchBadge(k)+' '+v+'</span>').join('')+'</div>'
  +table([{key:'name',label:'数据批次'},{key:'rows',label:'行数'},{key:'handover_name',label:'清单去向'},{key:'status_label',label:'对账状态'},{key:'claim_count',label:'认领数'},{key:'issue_count',label:'待处理项'}],data.datasets.map(d=>({...d,handover_name:d.handover_name||'—（旧批次）'})))
  +'<p class="hint">旧批次没有挂清单，照常进入工作台分析；挂了清单的批次需要负责人确认后才可分析。</p>';
}
function listForm(){
 return '<form id="handover-form"><label>清单名称<input name="name" required minlength="2" placeholder="例如：11 月纸质交接清单"></label><label>来源说明<input name="source" value="纸质交接清单" maxlength="200"></label><label>明细 JSON<textarea name="items" rows="8" required placeholder=\'[{"day":"2026-10-01","team":"A队","zone":"中心区","passed":9,"observed":10}]\'></textarea></label><p class="hint">每项含 day / team / zone / passed / observed；同日同队同区域不得重复，合格数不得超观察数。</p><button type="submit" class="primary" '+(canReconcile()?'':'disabled')+'>校验并登记清单</button></form>';
}
async function detail(id,lists){
 const h=await handovers.get(id);
 const batchRows=h.batches||[];
 return heading('02B / HANDOVER DETAIL',h.name,h.source+' · 登记于 '+date(h.created_at),'<a class="button" href="#/handovers">返回清单列表 ↗</a>')
  +'<div class="recon-summary"><div>'+batchBadge(h.status)+'<span class="summary-note">'+esc(h.status_label)+'</span></div>'
  +[['清单项',h.item_count],['缺项',h.missing_count],['余量',h.remainder_count],['计数差异',h.difference_count],['累计认领观察',fmt(h.totals.claimed_observed)+' / '+fmt(h.totals.observed)]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+v+'</strong></div>').join('')+'</div>'
  +'<div class="two-columns"><section class="panel"><div class="section-title"><h2>批次与清单去向</h2><span>一项清单可分给多批</span></div>'+(batchRows.length?batchRows.map(b=>'<article class="batch-link"><div><strong>'+esc(b.dataset_name)+'</strong><p>'+b.rows+' 行 · 已认领 '+b.claimed_rows+' 行 · 待处理 '+b.issue_count+' 项</p></div><div>'+batchBadge(b.status)+' <a class="text-link" href="#/reconcile/'+b.dataset_id+'">逐行认领 ↗</a></div></article>').join(''):'<p class="empty">尚未挂接日表批次。到数据接入页把批次挂到本清单。</p>')+'</section>'
  +'<section class="panel"><div class="section-title"><h2>缺项 / 余量 / 计数差异</h2><span>可逐项核对</span></div>'+issueLists(h)+'</section></div>'
  +'<section class="panel"><div class="section-title"><h2>清单去向</h2><span>认领落到哪一批的哪一行</span></div>'+destinationTable(h.destinations)+'</section>'
 +'<section class="panel"><div class="section-title"><h2>清单明细</h2><span>'+h.items.length+' 项</span></div>'+table([{key:'day',label:'观察日'},{key:'team',label:'采样队'},{key:'zone',label:'区域'},{key:'passed',label:'清单合格'},{key:'observed',label:'清单观察'},{key:'claimed_passed',label:'已认领合格'},{key:'claimed_observed',label:'已认领观察'},{key:'remaining_passed',label:'剩余合格'},{key:'remaining_observed',label:'剩余观察'}],h.items)+'</section>';
}
function destinationTable(destinations){
 if(!destinations?.length)return '<p class="empty">尚无认领去向。</p>';
 const rows=destinations.flatMap(d=>d.claims.map(c=>({item_id:d.item_id,dataset_name:c.dataset_name,row_number:c.row_number,passed:c.passed,observed:c.observed,state:c.state,claim_id:c.claim_id})));
 return table([{key:'item_id',label:'清单项'},{key:'dataset_name',label:'批次'},{key:'row_number',label:'日表行号'},{key:'passed',label:'认领合格'},{key:'observed',label:'认领观察'},{key:'state',label:'状态'}],rows);
}
function issueLists(h){
 const section=(title,rows,empty)=>'<h3 class="subhead">'+title+' <small>'+rows.length+'</small></h3>'+(rows.length?table([{key:'day',label:'观察日'},{key:'team',label:'采样队'},{key:'zone',label:'区域'},{key:'passed',label:'合格'},{key:'observed',label:'观察'},{key:'remaining_passed',label:'剩余合格'},{key:'remaining_observed',label:'剩余观察'}],rows):'<p class="empty">'+empty+'</p>');
 const differences=(h.count_differences||[]).map(d=>({day:d.day||'—',team:d.team||'—',zone:d.zone||'—',passed:d.passed,observed:d.observed,remaining_passed:d.passed_difference,remaining_observed:d.observed_difference,dataset_name:d.dataset_name,row_number:d.row_number}));
 return section('缺项（清单中尚未被任何批次认领）',h.missing,'没有缺项。')
  +section('余量（清单项尚未认领完）',h.remainder,'清单项已全部认领。')
  +'<h3 class="subhead">计数差异（日表行 − 已认领） <small>'+differences.length+'</small></h3>'+(differences.length?table([{key:'dataset_name',label:'批次'},{key:'row_number',label:'行号'},{key:'day',label:'观察日'},{key:'team',label:'采样队'},{key:'zone',label:'区域'},{key:'passed',label:'日表合格'},{key:'observed',label:'日表观察'},{key:'remaining_passed',label:'合格差'},{key:'remaining_observed',label:'观察差'}],differences):'<p class="empty">没有计数差异。</p>');
}
export function bindHandovers(refresh,id){
 document.querySelector('#refresh-handovers')?.addEventListener('click',refresh);
 const form=document.querySelector('#handover-form');
 if(!form||id)return;
 submit(form,async f=>{
   let items;
   try{items=JSON.parse(f.get('items'));}catch{throw new Error('明细必须是合法 JSON 数组');}
   const h=await handovers.create({name:f.get('name'),source:f.get('source'),items});
   notify('清单已登记，可到数据接入页挂接批次');
   location.hash='#/handovers/'+h.id;
 });
}
