import {session} from '../../stores/session.js';
import {BatchStatus} from '../../constants/enums.js';
import {esc} from '../../utils/dom.js';
export const batchBadge=v=>'<span class="batch-badge '+esc(v||'legacy')+'">'+esc(BatchStatus[v]||v||'旧批次')+'</span>';
export const batchCompact=s=>'<span class="batch-badge '+esc(s?.status||'legacy')+'" title="'+esc(s?.status_label||'')+'">'+esc(s?.status_label||BatchStatus.legacy)+'</span>';
export const canReconcile=()=>session.user.role!=='viewer';
export const canConfirm=()=>session.user.role==='admin';
