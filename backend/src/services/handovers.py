"""纸质交接清单与日表批次的对账服务。

关键账目不变式（均在数据库条件更新中守住）：
- 清单项累计认领的合格数、观察数都不超过清单原值；
- 任一时刻清单项的剩余合格数不超过剩余观察数；
- 同一日表批次的一行最多一条有效认领（数据库部分唯一索引兜底）；
- 撤销只退还原认领量；已用于分析的认领冻结。
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from src.models import (
    BatchReconciliation,
    Claim,
    Dataset,
    HandoverItem,
    HandoverList,
    Job,
)

MAX_COUNT = 1_000_000_000
ACTIVE_STATES = ('active', 'frozen')
STATUS_LABELS = {
    'legacy': '旧批次',
    'pending_reconcile': '待对账',
    'pending_complete': '待补齐',
    'ready': '可确认',
    'confirmed': '已确认',
}


def _integer(value, name, minimum=0, maximum=MAX_COUNT):
    try:
        answer = float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f'{name}必须是整数') from None
    if not math.isfinite(answer) or not answer.is_integer() or not minimum <= answer <= maximum:
        raise HTTPException(422, f'{name}必须是{minimum}至10亿之间的整数')
    return int(answer)


def _text(value, name, maximum):
    text = str(value if value is not None else '').strip()
    if not text or len(text) > maximum:
        raise HTTPException(422, f'{name}不能为空且不能超过{maximum}个字符')
    return text


def _item_key(day, team, zone):
    return (str(day).strip(), str(team).strip(), str(zone).strip())


# ---------------------------------------------------------------- 清单

def create_list(db, data, user=None):
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(422, '清单名称去除空格后至少需要两个字符')
    if db.scalar(select(HandoverList).where(HandoverList.name == name)):
        raise HTTPException(409, '该交接清单名称已存在，请修改名称')
    if not data.items:
        raise HTTPException(422, '交接清单至少需要一项日/队/区域记录')

    seen = set()
    items = []
    for index, raw in enumerate(data.items):
        label = f'第 {index + 1} 项'
        try:
            day = date.fromisoformat(str(raw.day).strip()).isoformat()
        except (TypeError, ValueError):
            raise HTTPException(422, f'{label}的观察日必须是 YYYY-MM-DD 格式') from None
        team = _text(raw.team, f'{label}采样队', 20)
        zone = _text(raw.zone, f'{label}区域', 60)
        passed = _integer(raw.passed, f'{label}合格数')
        observed = _integer(raw.observed, f'{label}观察数')
        if passed > observed:
            raise HTTPException(422, f'{label}合格数不能超过观察数')
        key = (day, team, zone)
        if key in seen:
            raise HTTPException(422, f'第 {index + 1} 项与前面的日/队/区域重复，请先去重')
        seen.add(key)
        items.append(HandoverItem(day=day, team=team, zone=zone, passed=passed,
                                  observed=observed, claimed_passed=0, claimed_observed=0,
                                  created_at=time.time()))

    handover = HandoverList(name=name, source=(data.source or '纸质交接清单').strip()[:200],
                            note=(data.note or '').strip()[:500],
                            created_by=user.id if user else None, created_at=time.time())
    db.add(handover)
    db.flush()
    for item in items:
        item.handover_id = handover.id
        db.add(item)
    db.flush()
    return handover


def item_dict(item):
    remaining_passed = item.passed - item.claimed_passed
    remaining_observed = item.observed - item.claimed_observed
    return {
        'id': item.id,
        'day': item.day,
        'team': item.team,
        'zone': item.zone,
        'passed': item.passed,
        'observed': item.observed,
        'claimed_passed': item.claimed_passed,
        'claimed_observed': item.claimed_observed,
        'remaining_passed': remaining_passed,
        'remaining_observed': remaining_observed,
        'missing': item.claimed_passed == 0 and item.claimed_observed == 0,
        'fully_claimed': remaining_passed == 0 and remaining_observed == 0,
        'rate': round(item.passed / item.observed * 100, 3) if item.observed else None,
    }


def _items_for_list(db, handover_id):
    return list(db.scalars(
        select(HandoverItem).where(HandoverItem.handover_id == handover_id)
        .order_by(HandoverItem.day, HandoverItem.team, HandoverItem.zone)
    ))


def _claims_for_list(db, handover_id):
    return list(db.scalars(
        select(Claim).where(Claim.handover_id == handover_id).order_by(Claim.id)
    ))


def _reconciliations_for_list(db, handover_id):
    return list(db.scalars(
        select(BatchReconciliation).where(BatchReconciliation.handover_id == handover_id)
        .order_by(BatchReconciliation.id)
    ))


def _dataset_rows(dataset):
    return dataset.records if isinstance(dataset.records, list) else []


def _row_counts(row):
    return _integer(row.get('passed'), '日表合格数'), _integer(row.get('observed'), '日表观察数')


def _non_revoked_claims(db, dataset_id):
    return list(db.scalars(
        select(Claim).where(Claim.dataset_id == dataset_id, Claim.state.in_(ACTIVE_STATES))
        .order_by(Claim.row_index, Claim.id)
    ))


def _row_claim_map(claims):
    result = {}
    for claim in claims:
        result.setdefault(claim.row_index, []).append(claim)
    return result


def _confirm_issues(db, dataset, claims=None):
    """返回 [(code, message)]，空列表表示整批吻合。"""
    rows = _dataset_rows(dataset)
    claims = claims if claims is not None else _non_revoked_claims(db, dataset.id)
    by_row = _row_claim_map(claims)
    issues = []
    touched = set()
    for index, row in enumerate(rows):
        found = by_row.get(index, [])
        if not found:
            issues.append(('missing_row', f'第 {index + 1} 行尚未认领'))
            continue
        if len(found) > 1:
            issues.append(('duplicate_claim', f'第 {index + 1} 行存在多条有效认领'))
            continue
        claim = found[0]
        row_passed, row_observed = _row_counts(row)
        day, team, zone = _item_key(row.get('day'), row.get('team'), row.get('zone'))
        item = db.get(HandoverItem, claim.item_id)
        if item is None or (item.day, item.team, item.zone) != (day, team, zone):
            issues.append(('item_mismatch', f'第 {index + 1} 行认领的清单项与日/队/区域不一致'))
            continue
        touched.add(item.id)
        if claim.passed != row_passed or claim.observed != row_observed:
            issues.append((
                'count_difference',
                f'第 {index + 1} 行计数差异：合格差 {row_passed - claim.passed}，'
                f'观察差 {row_observed - claim.observed}',
            ))
    for row_index in sorted(set(by_row) - set(range(len(rows)))):
        issues.append(('orphan_claim', f'认领指向不存在的日表第 {row_index + 1} 行'))
    return issues


def batch_status(db, dataset, reconciliation=None, claims=None):
    if reconciliation is None:
        reconciliation = db.scalar(
            select(BatchReconciliation).where(BatchReconciliation.dataset_id == dataset.id)
        )
    if reconciliation is None:
        return {'status': 'legacy', 'status_label': STATUS_LABELS['legacy'],
                'claimed_rows': 0, 'total_rows': len(_dataset_rows(dataset)),
                'issue_count': 0}
    claims = claims if claims is not None else _non_revoked_claims(db, dataset.id)
    by_row = _row_claim_map(claims)
    rows = _dataset_rows(dataset)
    claimed_rows = sum(1 for index in range(len(rows)) if by_row.get(index))
    issues = _confirm_issues(db, dataset, claims)
    if not issues:
        status = 'confirmed' if reconciliation.status == 'confirmed' else 'ready'
    elif claimed_rows == 0:
        status = 'pending_reconcile'
    else:
        status = 'pending_complete'
    return {
        'status': status,
        'status_label': STATUS_LABELS[status],
        'claimed_rows': claimed_rows,
        'total_rows': len(rows),
        'issue_count': len(issues),
    }


def _row_differences(db, dataset, claims):
    by_row = _row_claim_map(claims)
    rows = _dataset_rows(dataset)
    differences = []
    for index, row in enumerate(rows):
        row_passed, row_observed = _row_counts(row)
        found = by_row.get(index, [])
        claimed_passed = sum(claim.passed for claim in found)
        claimed_observed = sum(claim.observed for claim in found)
        if len(found) != 1 or claimed_passed != row_passed or claimed_observed != row_observed:
            differences.append({
                'dataset_id': dataset.id,
                'dataset_name': dataset.name,
                'row_index': index,
                'row_number': index + 1,
                'day': str(row.get('day', '')).strip(),
                'team': str(row.get('team', '')).strip(),
                'zone': str(row.get('zone', '')).strip(),
                'passed': row_passed,
                'observed': row_observed,
                'claimed_passed': claimed_passed,
                'claimed_observed': claimed_observed,
                'passed_difference': row_passed - claimed_passed,
                'observed_difference': row_observed - claimed_observed,
                'claim_count': len(found),
            })
    for row_index in sorted(set(by_row) - set(range(len(rows)))):
        claim = by_row[row_index][0]
        differences.append({
            'dataset_id': dataset.id,
            'dataset_name': dataset.name,
            'row_index': row_index,
            'row_number': row_index + 1,
            'day': '', 'team': '', 'zone': '',
            'passed': 0, 'observed': 0,
            'claimed_passed': sum(c.passed for c in by_row[row_index]),
            'claimed_observed': sum(c.observed for c in by_row[row_index]),
            'passed_difference': -sum(c.passed for c in by_row[row_index]),
            'observed_difference': -sum(c.observed for c in by_row[row_index]),
            'claim_count': len(by_row[row_index]),
        })
    return differences


def list_summary(db, handover, include_items=False):
    items = _items_for_list(db, handover.id)
    reconciliations = _reconciliations_for_list(db, handover.id)
    claims = _claims_for_list(db, handover.id)
    totals = {
        'passed': sum(item.passed for item in items),
        'observed': sum(item.observed for item in items),
        'claimed_passed': sum(item.claimed_passed for item in items),
        'claimed_observed': sum(item.claimed_observed for item in items),
    }
    totals['remaining_passed'] = totals['passed'] - totals['claimed_passed']
    totals['remaining_observed'] = totals['observed'] - totals['claimed_observed']
    missing = [item_dict(item) for item in items
               if item.claimed_passed == 0 and item.claimed_observed == 0]
    remainder = [item_dict(item) for item in items
                 if item.passed != item.claimed_passed or item.observed != item.claimed_observed]

    batches = []
    differences = []
    destinations = {}
    all_confirmed = True
    for reconciliation in reconciliations:
        dataset = db.get(Dataset, reconciliation.dataset_id)
        if dataset is None:
            continue
        batch_claims = _non_revoked_claims(db, dataset.id)
        state = batch_status(db, dataset, reconciliation, batch_claims)
        if state['status'] != 'confirmed':
            all_confirmed = False
        row_differences = _row_differences(db, dataset, batch_claims)
        differences.extend(row_differences)
        for claim in batch_claims:
            destinations.setdefault(claim.item_id, []).append({
                'claim_id': claim.id, 'dataset_id': dataset.id,
                'dataset_name': dataset.name, 'row_number': claim.row_index + 1,
                'passed': claim.passed, 'observed': claim.observed, 'state': claim.state,
            })
        batches.append({
            'dataset_id': dataset.id,
            'dataset_name': dataset.name,
            'rows': len(_dataset_rows(dataset)),
            **state,
        })
    if totals['remaining_passed'] == 0 and totals['remaining_observed'] == 0 and not differences:
        # 清单已全部认领且各批次确认后，才算清单已确认。
        status = 'confirmed' if batches and all_confirmed else 'ready'
    elif totals['claimed_passed'] == 0 and totals['claimed_observed'] == 0:
        status = 'pending_reconcile'
    else:
        status = 'pending_complete'

    result = {
        'id': handover.id,
        'name': handover.name,
        'source': handover.source,
        'note': handover.note,
        'created_at': handover.created_at,
        'status': status,
        'status_label': STATUS_LABELS[status],
        'item_count': len(items),
        'missing_count': len(missing),
        'remainder_count': len(remainder),
        'difference_count': len(differences),
        'totals': totals,
        'batches': batches,
        'claim_count': len(claims),
    }
    if include_items:
        result['items'] = [item_dict(item) for item in items]
        result['missing'] = missing
        result['remainder'] = remainder
        result['count_differences'] = differences
        result['claims'] = [claim_dict(claim) for claim in claims]
        result['destinations'] = [
            {'item_id': item_id, 'claims': entries}
            for item_id, entries in sorted(destinations.items())
        ]
    return result


def list_detail(db, handover_id):
    handover = db.get(HandoverList, handover_id)
    if handover is None:
        raise HTTPException(404, '交接清单不存在')
    return list_summary(db, handover, include_items=True)


# ---------------------------------------------------------------- 批次关联与状态

def attach_dataset(db, dataset, handover_id, user=None):
    handover = db.get(HandoverList, handover_id)
    if handover is None:
        raise HTTPException(404, '交接清单不存在')
    existing = db.scalar(
        select(BatchReconciliation).where(BatchReconciliation.dataset_id == dataset.id)
    )
    if existing:
        if existing.handover_id == handover_id:
            return existing
        raise HTTPException(409, '该数据批次已关联其他交接清单')
    if db.scalar(select(func.count()).select_from(Claim).where(Claim.dataset_id == dataset.id)):
        raise HTTPException(409, '该数据批次已有认领，不能改挂其他清单')
    reconciliation = BatchReconciliation(
        handover_id=handover_id, dataset_id=dataset.id, status='draft',
        created_by=user.id if user else None, created_at=time.time(),
    )
    db.add(reconciliation)
    db.flush()
    return reconciliation


def dataset_reconciliation(db, dataset):
    reconciliation = db.scalar(
        select(BatchReconciliation).where(BatchReconciliation.dataset_id == dataset.id)
    )
    if reconciliation is None:
        return {**batch_status(db, dataset, None, []), 'handover_id': None,
                'handover_name': None, 'confirmed_at': None, 'claim_count': 0,
                'issues': []}
    handover = db.get(HandoverList, reconciliation.handover_id)
    claims = _non_revoked_claims(db, dataset.id)
    state = batch_status(db, dataset, reconciliation, claims)
    return {
        **state,
        'handover_id': reconciliation.handover_id,
        'handover_name': handover.name if handover else None,
        'confirmed_at': reconciliation.confirmed_at,
        'checksum': reconciliation.checksum,
        'claim_count': len(claims),
        'issues': [{'code': code, 'message': message}
                   for code, message in _confirm_issues(db, dataset, claims)],
    }


def claim_dict(claim):
    return {
        'id': claim.id,
        'handover_id': claim.handover_id,
        'item_id': claim.item_id,
        'dataset_id': claim.dataset_id,
        'row_index': claim.row_index,
        'row_number': claim.row_index + 1,
        'passed': claim.passed,
        'observed': claim.observed,
        'row_passed': claim.row_passed,
        'row_observed': claim.row_observed,
        'state': claim.state,
        'idempotency_key': claim.idempotency_key,
        'created_at': claim.created_at,
        'revoked_at': claim.revoked_at,
    }


# ---------------------------------------------------------------- 认领

def _claim_failure_reason(db, item_id, passed, observed):
    item = db.get(HandoverItem, item_id)
    if item is None:
        return '清单项不存在'
    if item.claimed_passed + passed > item.passed or item.claimed_observed + observed > item.observed:
        return (
            f'累计认领超过清单：剩余合格 {item.passed - item.claimed_passed}、'
            f'观察 {item.observed - item.claimed_observed}'
        )
    new_remaining_passed = item.passed - item.claimed_passed - passed
    new_remaining_observed = item.observed - item.claimed_observed - observed
    if new_remaining_passed > new_remaining_observed:
        return '认领后剩余合格数将超过剩余观察数，不能认领'
    return '同一批次行的并发认领发生冲突，请刷新后重试'


def _lock_items(db, item_ids):
    """PostgreSQL 下按主键顺序加行锁，串行化同一清单项的并发认领/撤销。"""
    if db.get_bind().dialect.name != 'postgresql':
        return
    for item_id in sorted(set(item_ids)):
        db.execute(select(HandoverItem.id).where(HandoverItem.id == item_id).with_for_update())


def _request_key(value):
    key = (value or '').strip() or None
    if key and len(key) > 100:
        raise HTTPException(422, '认领幂等键不能超过100个字符')
    return key


def claim_rows(db, dataset_id, lines, user=None, idempotency_key=None):
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(404, '数据批次不存在')
    reconciliation = db.scalar(
        select(BatchReconciliation).where(BatchReconciliation.dataset_id == dataset.id)
    )
    if reconciliation is None:
        raise HTTPException(409, '该批次未关联交接清单，旧批次无需对账')
    if reconciliation.status == 'confirmed' and db.scalar(
            select(func.count()).select_from(Job).where(Job.dataset_id == dataset.id)):
        raise HTTPException(409, '该批次已确认并用于分析，认领已冻结')
    handover_id = reconciliation.handover_id
    rows = _dataset_rows(dataset)
    if not lines:
        raise HTTPException(422, '请至少提供一行认领')
    if len(lines) > 5000:
        raise HTTPException(422, '单次最多认领 5000 行')

    request_key = _request_key(idempotency_key)
    if request_key:
        replay = list(db.scalars(
            select(Claim).where(
                Claim.dataset_id == dataset.id,
                Claim.idempotency_key.like(request_key + ':%'),
            ).order_by(Claim.row_index, Claim.id)
        ))
        if replay:
            expected = hashlib.sha256(json.dumps(
                [[line.row_index, line.item_id, line.passed, line.observed] for line in lines]
            ).encode()).hexdigest()
            if any(claim.request_checksum != expected for claim in replay):
                raise HTTPException(409, '幂等键已用于另一组认领明细，请更换幂等键')
            return replay, True

    prepared = []
    seen_rows = set()
    for index, line in enumerate(lines):
        label = f'第 {index + 1} 条认领'
        row_index = _integer(line.row_index, f'{label}行号', 0, 10_000_000)
        if row_index >= len(rows):
            raise HTTPException(422, f'{label}行号超出日表范围')
        if row_index in seen_rows:
            raise HTTPException(422, f'同一请求中第 {row_index + 1} 行重复认领')
        seen_rows.add(row_index)
        item_id = _integer(line.item_id, f'{label}清单项', 1, 10_000_000)
        item = db.get(HandoverItem, item_id)
        if item is None or item.handover_id != handover_id:
            raise HTTPException(422, f'{label}不属于当前交接清单')
        row = rows[row_index]
        day, team, zone = _item_key(row.get('day'), row.get('team'), row.get('zone'))
        if (item.day, item.team, item.zone) != (day, team, zone):
            raise HTTPException(422, f'{label}的日/队/区域与清单项不一致')
        row_passed, row_observed = _row_counts(row)
        passed = row_passed if line.passed is None else _integer(line.passed, f'{label}合格数')
        observed = row_observed if line.observed is None else _integer(line.observed, f'{label}观察数')
        if passed > row_passed or observed > row_observed:
            raise HTTPException(422, f'{label}认领数不能超过日表该行的计数')
        if passed > observed:
            raise HTTPException(422, f'{label}合格数不能超过观察数')
        prepared.append({
            'row_index': row_index, 'item_id': item_id, 'item': item,
            'passed': passed, 'observed': observed,
            'row_passed': row_passed, 'row_observed': row_observed,
        })

    request_checksum = hashlib.sha256(json.dumps(
        [[line.row_index, line.item_id, line.passed, line.observed] for line in lines]
    ).encode()).hexdigest()
    _lock_items(db, (entry['item_id'] for entry in prepared))
    created = []
    try:
        for entry in prepared:
            statement = (
                update(HandoverItem)
                .where(HandoverItem.id == entry['item_id'])
                .where(HandoverItem.claimed_passed + entry['passed'] <= HandoverItem.passed)
                .where(HandoverItem.claimed_observed + entry['observed'] <= HandoverItem.observed)
                .where(
                    HandoverItem.passed - (HandoverItem.claimed_passed + entry['passed'])
                    <= HandoverItem.observed - (HandoverItem.claimed_observed + entry['observed'])
                )
                .values(
                    claimed_passed=HandoverItem.claimed_passed + entry['passed'],
                    claimed_observed=HandoverItem.claimed_observed + entry['observed'],
                )
                .execution_options(synchronize_session=False)
            )
            result = db.execute(statement)
            if result.rowcount != 1:
                reason = _claim_failure_reason(db, entry['item_id'], entry['passed'], entry['observed'])
                raise HTTPException(409, reason)
            claim = Claim(
                handover_id=handover_id, item_id=entry['item_id'], dataset_id=dataset.id,
                row_index=entry['row_index'], passed=entry['passed'], observed=entry['observed'],
                row_passed=entry['row_passed'], row_observed=entry['row_observed'],
                state='active',
                idempotency_key=f"{request_key}:{entry['row_index']}" if request_key else None,
                request_checksum=request_checksum,
                created_by=user.id if user else None, created_at=time.time(),
            )
            db.add(claim)
            db.flush()
            created.append(claim)
            db.refresh(entry['item'])
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该批次行已被认领，请刷新后重试') from None
    except HTTPException:
        db.rollback()
        raise
    return created, False


# ---------------------------------------------------------------- 撤销

def revoke_claim(db, claim_id, user=None):
    claim = db.get(Claim, claim_id)
    if claim is None:
        raise HTTPException(404, '认领记录不存在')
    if claim.state == 'frozen':
        raise HTTPException(409, '该认领已用于分析，已冻结，不能撤销')
    if claim.state == 'revoked':
        raise HTTPException(409, '该认领已经撤销，不能重复退款')
    if db.scalar(select(func.count()).select_from(Job).where(Job.dataset_id == claim.dataset_id)):
        raise HTTPException(409, '该批次已用于分析，认领已冻结，不能撤销')

    _lock_items(db, [claim.item_id])
    changed = db.execute(
        update(Claim)
        .where(Claim.id == claim.id, Claim.state == 'active')
        .values(state='revoked', revoked_at=time.time())
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '认领状态已变化，请刷新后重试')

    refunded = db.execute(
        update(HandoverItem)
        .where(HandoverItem.id == claim.item_id)
        .where(HandoverItem.claimed_passed >= claim.passed)
        .where(HandoverItem.claimed_observed >= claim.observed)
        .values(
            claimed_passed=HandoverItem.claimed_passed - claim.passed,
            claimed_observed=HandoverItem.claimed_observed - claim.observed,
        )
        .execution_options(synchronize_session=False)
    )
    if refunded.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '清单账目异常，撤销未执行')

    reconciliation = db.scalar(
        select(BatchReconciliation).where(BatchReconciliation.dataset_id == claim.dataset_id)
    )
    if reconciliation and reconciliation.status == 'confirmed':
        reconciliation.status = 'draft'
        reconciliation.confirmed_at = None
        reconciliation.checksum = ''
        if reconciliation.idempotency_key and reconciliation.idempotency_key.startswith('auto-'):
            reconciliation.idempotency_key = None
    db.flush()
    db.refresh(claim)
    return claim


# ---------------------------------------------------------------- 确认

def _confirmation_checksum(db, dataset, handover_id):
    claims = _non_revoked_claims(db, dataset.id)
    payload = {
        'dataset': dataset.id,
        'name': dataset.name,
        'checksum': dataset.checksum,
        'handover': handover_id,
        'claims': [[c.row_index, c.item_id, c.passed, c.observed] for c in claims],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def recover_interrupted(db):
    """启动时修复中断的确认：处于 confirming 的批次退回草案，账目不变。"""
    changed = []
    for reconciliation in db.scalars(select(BatchReconciliation).where(
            BatchReconciliation.status == 'confirming')):
        reconciliation.status = 'draft'
        reconciliation.confirmed_at = None
        changed.append(reconciliation.dataset_id)
    db.flush()
    return changed


def _lock_reconciliation(db, reconciliation_id):
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(select(BatchReconciliation.id)
                   .where(BatchReconciliation.id == reconciliation_id)
                   .with_for_update())


def _issue_payload(issues):
    return {'message': '整批尚未吻合，暂不开放分析',
            'issues': [{'code': code, 'message': message} for code, message in issues]}


def _finalize_confirmation(db, dataset, reconciliation):
    """第二阶段：重新核对后把批次置为可用；失败则退回草案，账目不变。"""
    _lock_reconciliation(db, reconciliation.id)
    db.refresh(reconciliation)
    if reconciliation.status == 'confirmed':
        return False
    issues = _confirm_issues(db, dataset)
    if issues:
        reconciliation.status = 'draft'
        reconciliation.confirmed_at = None
        db.commit()
        raise HTTPException(409, _issue_payload(issues))
    changed = db.execute(
        update(BatchReconciliation)
        .where(BatchReconciliation.id == reconciliation.id,
               BatchReconciliation.status == 'confirming')
        .values(status='confirmed', confirmed_at=time.time(),
                checksum=_confirmation_checksum(db, dataset, reconciliation.handover_id),
                idempotency_key=reconciliation.idempotency_key)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(reconciliation)
    return changed.rowcount == 1


def confirm_batch(db, dataset_id, idempotency_key=None, user=None):
    """两阶段确认：先落“确认中”意向，再核对并置为可用。

    中断只会留下 confirming，启动恢复把它退回草案；清单余额与认领明细不受影响。
    重试命中同一 idempotency_key 时返回既有结果，不新增批次、不重复扣减。
    """
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(404, '数据批次不存在')
    reconciliation = db.scalar(
        select(BatchReconciliation).where(BatchReconciliation.dataset_id == dataset.id)
    )
    if reconciliation is None:
        raise HTTPException(409, '该批次未关联交接清单，旧批次无需确认')
    key = _request_key(idempotency_key)
    if key:
        clash = db.scalar(
            select(BatchReconciliation).where(
                BatchReconciliation.idempotency_key == key,
                BatchReconciliation.dataset_id != dataset.id,
            )
        )
        if clash:
            raise HTTPException(409, '该幂等键已用于其他批次确认')

    if reconciliation.status == 'confirmed':
        state = batch_status(db, dataset, reconciliation)
        if state['status'] == 'confirmed':
            return {'created': False, 'reconciliation': reconciliation, 'state': state}
        # 已确认但账目被改动（例如撤销回退），重新走确认。
        reconciliation.status = 'draft'
        db.flush()

    issues = _confirm_issues(db, dataset)
    if issues:
        raise HTTPException(409, _issue_payload(issues))

    if reconciliation.status != 'confirming':
        if key:
            reconciliation.idempotency_key = key
        elif not reconciliation.idempotency_key:
            reconciliation.idempotency_key = 'auto-' + secrets.token_hex(12)
        reconciliation.status = 'confirming'
        db.commit()
        db.refresh(reconciliation)

    created = _finalize_confirmation(db, dataset, reconciliation)
    reconciliation = db.get(BatchReconciliation, reconciliation.id)
    return {'created': created, 'reconciliation': reconciliation,
            'state': batch_status(db, dataset, reconciliation)}


def freeze_claims(db, dataset_id):
    db.execute(
        update(Claim)
        .where(Claim.dataset_id == dataset_id, Claim.state == 'active')
        .values(state='frozen')
        .execution_options(synchronize_session=False)
    )


# ---------------------------------------------------------------- 对账报告

def reconciliation_report(db, handover_id):
    handover = db.get(HandoverList, handover_id)
    if handover is None:
        raise HTTPException(404, '交接清单不存在')
    summary = list_summary(db, handover, include_items=True)
    item_by_id = {item['id']: item for item in summary['items']}
    batches = []
    row_differences = []
    for reconciliation in _reconciliations_for_list(db, handover.id):
        dataset = db.get(Dataset, reconciliation.dataset_id)
        if dataset is None:
            continue
        status = batch_status(db, dataset, reconciliation)
        claims = _non_revoked_claims(db, dataset.id)
        by_row = _row_claim_map(claims)
        rows = []
        for index, row in enumerate(_dataset_rows(dataset)):
            row_passed, row_observed = _row_counts(row)
            found = by_row.get(index, [])
            entry = {
                'row_index': index,
                'row_number': index + 1,
                'day': str(row.get('day', '')).strip(),
                'team': str(row.get('team', '')).strip(),
                'zone': str(row.get('zone', '')).strip(),
                'passed': row_passed,
                'observed': row_observed,
                'claim': claim_dict(found[0]) if len(found) == 1 else None,
                'claim_count': len(found),
                'passed_difference': row_passed - sum(c.passed for c in found),
                'observed_difference': row_observed - sum(c.observed for c in found),
                'item': item_by_id.get(found[0].item_id) if len(found) == 1 else None,
            }
            if entry['claim'] is None or entry['passed_difference'] or entry['observed_difference']:
                row_differences.append({**entry, 'dataset_id': dataset.id,
                                        'dataset_name': dataset.name})
            rows.append(entry)
        batches.append({
            'dataset_id': dataset.id,
            'dataset_name': dataset.name,
            'source': dataset.source,
            'status': status['status'],
            'status_label': status['status_label'],
            'rows': rows,
        })
    summary['batches_detail'] = batches
    summary['row_differences'] = row_differences
    return summary


def overview_counts(db):
    datasets = list(db.scalars(select(Dataset)))
    counts = {'legacy': 0, 'pending_reconcile': 0, 'pending_complete': 0,
              'ready': 0, 'confirmed': 0}
    reconciliations = {r.dataset_id: r for r in db.scalars(select(BatchReconciliation))}
    for dataset in datasets:
        state = batch_status(db, dataset, reconciliations.get(dataset.id))
        counts[state['status']] = counts.get(state['status'], 0) + 1
    return counts


def dataset_rows_detail(db, dataset):
    """日表逐行对账视图：每行认领与对应清单项、差量、可否确认。"""
    reconciliation = db.scalar(
        select(BatchReconciliation).where(BatchReconciliation.dataset_id == dataset.id)
    )
    claims = _non_revoked_claims(db, dataset.id)
    by_row = _row_claim_map(claims)
    handover = db.get(HandoverList, reconciliation.handover_id) if reconciliation else None
    item_by_key = {}
    if handover:
        for item in _items_for_list(db, handover.id):
            item_by_key[(item.day, item.team, item.zone)] = item
    rows = []
    for index, row in enumerate(_dataset_rows(dataset)):
        row_passed, row_observed = _row_counts(row)
        found = by_row.get(index, [])
        row_key = _item_key(row.get('day'), row.get('team'), row.get('zone'))
        item = db.get(HandoverItem, found[0].item_id) if len(found) == 1 else item_by_key.get(row_key)
        rows.append({
            'row_index': index,
            'row_number': index + 1,
            'day': str(row.get('day', '')).strip(),
            'team': str(row.get('team', '')).strip(),
            'zone': str(row.get('zone', '')).strip(),
            'passed': row_passed,
            'observed': row_observed,
            'claim': claim_dict(found[0]) if len(found) == 1 else None,
            'claim_count': len(found),
            'item': item_dict(item) if item else None,
            'item_match': bool(item and (item.day, item.team, item.zone) == row_key),
            'passed_difference': row_passed - sum(c.passed for c in found),
            'observed_difference': row_observed - sum(c.observed for c in found),
            'claimed': bool(found),
        })
    return {
        'dataset': {'id': dataset.id, 'name': dataset.name, 'source': dataset.source,
                    'checksum': dataset.checksum},
        'reconciliation': dataset_reconciliation(db, dataset),
        'handover': {'id': handover.id, 'name': handover.name} if handover else None,
        'rows': rows,
    }
