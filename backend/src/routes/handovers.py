from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import Dataset, HandoverList
from src.schemas.handover import AttachInput, ClaimInput, ConfirmInput, HandoverInput
from src.services import handovers
from src.services.audit import record
router = APIRouter()


@router.get('/handovers')
def handover_listing(db=Depends(get_db), u=Depends(current_user)):
    return [handovers.list_summary(db, h)
            for h in db.scalars(select(HandoverList).order_by(HandoverList.id.desc()))]


@router.post('/handovers', status_code=201)
def create_handover(data: HandoverInput, db=Depends(get_db), u=Depends(can_write)):
    handover = handovers.create_list(db, data, u)
    record(db, u.email, 'handover.create', {'id': handover.id, 'items': len(data.items)})
    db.commit()
    return handovers.list_detail(db, handover.id)


@router.get('/handovers/{id}')
def handover_detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    return handovers.list_detail(db, id)


@router.get('/handovers/{id}/reconciliation')
def handover_reconciliation(id: int, db=Depends(get_db), u=Depends(current_user)):
    return handovers.reconciliation_report(db, id)


@router.post('/datasets/{id}/handover', status_code=201)
def attach(id: int, data: AttachInput, db=Depends(get_db), u=Depends(can_write)):
    dataset = db.get(Dataset, id)
    if dataset is None:
        raise HTTPException(404, '数据批次不存在')
    handovers.attach_dataset(db, dataset, data.handover_id, u)
    record(db, u.email, 'dataset.attach_handover',
           {'dataset': id, 'handover': data.handover_id})
    db.commit()
    return handovers.dataset_reconciliation(db, dataset)


@router.get('/datasets/{id}/reconciliation')
def dataset_reconciliation(id: int, db=Depends(get_db), u=Depends(current_user)):
    dataset = db.get(Dataset, id)
    if dataset is None:
        raise HTTPException(404, '数据批次不存在')
    return handovers.dataset_reconciliation(db, dataset)


@router.get('/datasets/{id}/reconciliation/rows')
def dataset_reconciliation_rows(id: int, db=Depends(get_db), u=Depends(current_user)):
    dataset = db.get(Dataset, id)
    if dataset is None:
        raise HTTPException(404, '数据批次不存在')
    return handovers.dataset_rows_detail(db, dataset)


@router.post('/datasets/{id}/claims', status_code=201)
def claim(id: int, data: ClaimInput, db=Depends(get_db), u=Depends(can_write)):
    try:
        claims, replayed = handovers.claim_rows(db, id, data.lines, u, data.idempotency_key)
        if not replayed:
            record(db, u.email, 'claim.create', {'dataset': id, 'claims': len(claims),
                                                 'idempotency_key': data.idempotency_key})
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    dataset = db.get(Dataset, id)
    return {'replayed': replayed,
            'claims': [handovers.claim_dict(c) for c in claims],
            'reconciliation': handovers.dataset_reconciliation(db, dataset)}


@router.post('/claims/{id}/revoke')
def revoke(id: int, db=Depends(get_db), u=Depends(can_write)):
    try:
        claim_record = handovers.revoke_claim(db, id, u)
        record(db, u.email, 'claim.revoke', {'claim': id, 'dataset': claim_record.dataset_id})
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return handovers.claim_dict(claim_record)


@router.post('/datasets/{id}/confirm')
def confirm(id: int, data: ConfirmInput | None = None, db=Depends(get_db), u=Depends(admin_only)):
    payload = data or ConfirmInput()
    try:
        outcome = handovers.confirm_batch(db, id, payload.idempotency_key, u)
        if outcome['created']:
            record(db, u.email, 'batch.confirm',
                   {'dataset': id, 'handover': outcome['reconciliation'].handover_id})
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    dataset = db.get(Dataset, id)
    return {'already_confirmed': not outcome['created'],
            'reconciliation': handovers.dataset_reconciliation(db, dataset)}


@router.get('/batches/status')
def batch_statuses(db=Depends(get_db), u=Depends(current_user)):
    return {'datasets': [{'id': d.id, 'name': d.name, 'rows': len(d.records or []),
                          **handovers.dataset_reconciliation(db, d)}
                         for d in db.scalars(select(Dataset).order_by(Dataset.id.desc()))],
            'counts': handovers.overview_counts(db)}
