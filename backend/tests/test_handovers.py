"""交接对账单元与流程检查。

运行：在 backend/ 下执行 `python -m pytest tests/test_handovers.py -q`
（SQLite + TestClient，不接触 Docker 容器。）
"""
import os
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + tempfile.mktemp(suffix='.db'))
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:1/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', tempfile.mkdtemp())
os.environ['SEED_DEMO'] = 'false'

import threading

import pytest
from fastapi.testclient import TestClient

from migrations.initial import migrate
from src.config.database import Session
from src.main import app
from sqlalchemy import func, select

from src.models import BatchReconciliation, Claim, User
from src.services.business import seed_records
from src.utils.security import hash_password


@pytest.fixture(scope='module')
def client():
    migrate()
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@test.local', name=role, role=role,
                        password=hash_password('Demo123!')))
        db.commit()
    return TestClient(app)


def auth(client, role):
    response = client.post('/auth/login',
                           json={'email': role + '@test.local', 'password': 'Demo123!'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


ROWS = seed_records()


def list_items(rows, factor=1):
    return [{'day': r['day'], 'team': r['team'], 'zone': r['zone'],
             'passed': factor * int(r['passed']), 'observed': factor * int(r['observed'])}
            for r in rows]


def key(row):
    return row['day'], row['team'], row['zone']


def create_handover(client, headers, name, items):
    response = client.post('/handovers', headers=headers,
                           json={'name': name, 'items': items})
    assert response.status_code == 201, response.text
    return response.json()


def create_dataset(client, headers, name, records, handover_id=None):
    payload = {'name': name, 'records': records}
    if handover_id:
        payload['handover_id'] = handover_id
    response = client.post('/datasets', headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def claim_lines(rows, by_key, indexes, passed=None, observed=None):
    lines = []
    for index in indexes:
        line = {'row_index': index, 'item_id': by_key[key(rows[index])]['id']}
        if passed is not None:
            line['passed'] = passed
        if observed is not None:
            line['observed'] = observed
        lines.append(line)
    return lines


def test_duplicate_and_invalid_list_items_rejected(client):
    headers = auth(client, 'analyst')
    duplicate = client.post('/handovers', headers=headers, json={
        'name': '重复清单', 'items': list_items(ROWS[:2]) + list_items(ROWS[:1])})
    assert duplicate.status_code == 422
    invalid = client.post('/handovers', headers=headers, json={
        'name': '非法清单',
        'items': [{'day': '2026-10-01', 'team': 'A队', 'zone': '中心区',
                   'passed': 5, 'observed': 3}]})
    assert invalid.status_code == 422


def test_rbac_viewer_cannot_claim_and_analyst_cannot_confirm(client):
    analyst, viewer = auth(client, 'analyst'), auth(client, 'viewer')
    handover = create_handover(client, analyst, '权限清单', list_items(ROWS[:20]))
    dataset = create_dataset(client, analyst, '权限日表', ROWS[:20], handover['id'])
    blocked = client.post(f"/datasets/{dataset['id']}/claims", headers=viewer,
                          json={'lines': [{'row_index': 0, 'item_id': handover['items'][0]['id']}]})
    assert blocked.status_code == 403
    confirm = client.post(f"/datasets/{dataset['id']}/confirm", headers=analyst, json={})
    assert confirm.status_code == 403


def test_full_reconciliation_flow_and_analysis_gate(client):
    analyst, admin = auth(client, 'analyst'), auth(client, 'admin')
    handover = create_handover(client, analyst, '主流程清单', list_items(ROWS))
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '主流程日表', ROWS, handover['id'])
    did = dataset['id']

    assert dataset['reconciliation']['status'] == 'pending_reconcile'
    early = client.post('/jobs', headers=analyst,
                        json={'name': '提前分析', 'dataset_id': did, 'params': {}})
    assert early.status_code == 409

    first = client.post(f'/datasets/{did}/claims', headers=analyst,
                        json={'lines': claim_lines(ROWS, by_key, range(60))})
    assert first.status_code == 201
    mid = client.get(f'/datasets/{did}/reconciliation', headers=analyst).json()
    assert mid['status'] == 'pending_complete'
    assert mid['claimed_rows'] == 60
    assert mid['issue_count'] == 60

    again = client.post(f'/datasets/{did}/claims', headers=analyst,
                        json={'lines': claim_lines(ROWS, by_key, [0])})
    assert again.status_code == 409

    rest = client.post(f'/datasets/{did}/claims', headers=analyst,
                       json={'lines': claim_lines(ROWS, by_key, range(60, 120))})
    assert rest.status_code == 201
    ready = client.get(f'/datasets/{did}/reconciliation', headers=analyst).json()
    assert ready['status'] == 'ready'
    assert ready['issue_count'] == 0

    confirmed = client.post(f'/datasets/{did}/confirm', headers=admin,
                            json={'idempotency_key': 'flow-key'})
    assert confirmed.status_code == 200
    assert confirmed.json()['reconciliation']['status'] == 'confirmed'
    replay = client.post(f'/datasets/{did}/confirm', headers=admin,
                         json={'idempotency_key': 'flow-key'})
    assert replay.status_code == 200 and replay.json()['already_confirmed'] is True

    job = client.post('/jobs', headers=analyst,
                      json={'name': '确认后分析', 'dataset_id': did, 'params': {}})
    assert job.status_code == 201, job.text

    detail = client.get(f'/datasets/{did}/reconciliation/rows', headers=analyst).json()
    states = {row['claim']['state'] for row in detail['rows'] if row['claim']}
    assert states == {'frozen'}
    frozen_claim = next(row['claim']['id'] for row in detail['rows'] if row['claim'])
    revoke = client.post(f'/claims/{frozen_claim}/revoke', headers=analyst)
    assert revoke.status_code == 409


def test_legacy_batch_still_analyzable(client):
    analyst = auth(client, 'analyst')
    dataset = create_dataset(client, analyst, '旧批次日表', ROWS)
    assert dataset['reconciliation']['status'] == 'legacy'
    job = client.post('/jobs', headers=analyst,
                      json={'name': '旧批次分析', 'dataset_id': dataset['id'], 'params': {}})
    assert job.status_code == 201, job.text


def test_one_list_can_split_across_batches(client):
    analyst, admin = auth(client, 'analyst'), auth(client, 'admin')
    handover = create_handover(client, analyst, '多批清单', list_items(ROWS, factor=2))
    by_key = {key(i): i for i in handover['items']}
    first = create_dataset(client, analyst, '多批日表甲', ROWS, handover['id'])
    second = create_dataset(client, analyst, '多批日表乙', ROWS, handover['id'])
    for dataset in (first, second):
        claimed = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                              json={'lines': claim_lines(ROWS, by_key, range(120))})
        assert claimed.status_code == 201, claimed.text
    summary = client.get(f"/handovers/{handover['id']}", headers=analyst).json()
    assert summary['status'] == 'ready'
    assert summary['totals']['remaining_passed'] == 0
    assert summary['totals']['remaining_observed'] == 0
    for dataset in (first, second):
        assert client.post(f"/datasets/{dataset['id']}/confirm", headers=admin,
                           json={}).status_code == 200
    assert client.get(f"/handovers/{handover['id']}", headers=analyst).json()['status'] == 'confirmed'


def test_remainder_cannot_leave_passed_above_observed(client):
    analyst = auth(client, 'analyst')
    rows = [{'day': f'2026-11-0{i + 1}', 'team': 'A队', 'zone': '中心区',
             'passed': 3, 'observed': 9, 'frame_size': 6200} for i in range(6)]
    items = [{'day': r['day'], 'team': r['team'], 'zone': r['zone'],
              'passed': 5, 'observed': 6} for r in rows]
    handover = create_handover(client, analyst, '余额清单', items)
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '余额日表', rows, handover['id'])
    item = by_key[key(rows[0])]
    too_many = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                           json={'lines': [{'row_index': 0, 'item_id': item['id'],
                                            'passed': 0, 'observed': 6}]})
    assert too_many.status_code == 409
    assert '剩余' in too_many.text
    over_list = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                            json={'lines': [{'row_index': 0, 'item_id': item['id']}]})
    assert over_list.status_code == 409


def test_revoke_refunds_exact_amount_and_is_not_repeatable(client):
    analyst = auth(client, 'analyst')
    handover = create_handover(client, analyst, '撤销清单', list_items(ROWS[:10]))
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '撤销日表', ROWS[:10], handover['id'])
    claimed = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                          json={'lines': claim_lines(ROWS[:10], by_key, [0])})
    claim = claimed.json()['claims'][0]
    before = {i['id']: i for i in client.get(f"/handovers/{handover['id']}", headers=analyst).json()['items']}
    revoked = client.post(f"/claims/{claim['id']}/revoke", headers=analyst)
    assert revoked.status_code == 200
    after = {i['id']: i for i in client.get(f"/handovers/{handover['id']}", headers=analyst).json()['items']}
    item_id = claim['item_id']
    assert (after[item_id]['claimed_passed'], after[item_id]['claimed_observed']) == (0, 0)
    assert before[item_id]['claimed_passed'] == claim['passed']
    repeat = client.post(f"/claims/{claim['id']}/revoke", headers=analyst)
    assert repeat.status_code == 409
    still = {i['id']: i for i in client.get(f"/handovers/{handover['id']}", headers=analyst).json()['items']}
    assert (still[item_id]['claimed_passed'], still[item_id]['claimed_observed']) == (0, 0)


def test_claim_idempotency_key_prevents_double_debit(client):
    analyst = auth(client, 'analyst')
    handover = create_handover(client, analyst, '幂等清单', list_items(ROWS[:20]))
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '幂等日表', ROWS[:20], handover['id'])
    body = {'lines': claim_lines(ROWS[:20], by_key, [0], passed=2, observed=2),
            'idempotency_key': 'retry-once'}
    first = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst, json=body)
    second = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst, json=body)
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()['replayed'] is True
    item_id = by_key[key(ROWS[0])]['id']
    items = {i['id']: i for i in client.get(f"/handovers/{handover['id']}", headers=analyst).json()['items']}
    assert items[item_id]['claimed_passed'] == 2
    assert items[item_id]['claimed_observed'] == 2


def test_concurrent_same_row_claim_only_one_wins(client):
    analyst = auth(client, 'analyst')
    synth = [{'day': f'2026-12-{i + 1:02d}', 'team': 'A队', 'zone': '中心区',
              'passed': 3, 'observed': 9, 'frame_size': 6200} for i in range(12)]
    handover = create_handover(client, analyst, '并发行清单', [
        {'day': r['day'], 'team': r['team'], 'zone': r['zone'], 'passed': 5, 'observed': 9}
        for r in synth])
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '并发行日表', synth, handover['id'])
    line = {'row_index': 0, 'item_id': by_key[key(synth[0])]['id'],
            'passed': 3, 'observed': 5}
    statuses = []

    def worker():
        local = TestClient(app)
        statuses.append(local.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                                   json={'lines': [line]}).status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert statuses.count(201) == 1
    assert statuses.count(409) == 3
    items = client.get(f"/handovers/{handover['id']}", headers=analyst).json()['items']
    chosen = [i for i in items if i['id'] == line['item_id']][0]
    assert chosen['claimed_passed'] == 3 and chosen['claimed_observed'] == 5


def test_concurrent_distinct_rows_keep_balances_exact(client):
    analyst = auth(client, 'analyst')
    handover = create_handover(client, analyst, '并发余额清单', list_items(ROWS))
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '并发余额日表', ROWS, handover['id'])
    indexes = list(range(40))
    chunks = [indexes[i::4] for i in range(4)]
    statuses = []

    def worker(chunk):
        local = TestClient(app)
        statuses.append(local.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                                   json={'lines': claim_lines(ROWS, by_key, chunk)}).status_code)

    threads = [threading.Thread(target=worker, args=(chunk,)) for chunk in chunks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert statuses.count(201) == 4
    summary = client.get(f"/handovers/{handover['id']}", headers=analyst).json()
    assert summary['totals']['claimed_passed'] == sum(int(ROWS[i]['passed']) for i in indexes)
    assert summary['totals']['claimed_observed'] == sum(int(ROWS[i]['observed']) for i in indexes)
    assert all(i['remaining_passed'] <= i['remaining_observed'] for i in summary['items'])


def test_confirmation_rejects_incomplete_batch_with_issues(client):
    analyst, admin = auth(client, 'analyst'), auth(client, 'admin')
    handover = create_handover(client, analyst, '缺项清单', list_items(ROWS))
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '缺项日表', ROWS[:5], handover['id'])
    partial = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                          json={'lines': claim_lines(ROWS[:5], by_key, [0], passed=4, observed=4)})
    assert partial.status_code == 201
    blocked = client.post(f"/datasets/{dataset['id']}/confirm", headers=admin, json={})
    assert blocked.status_code == 409
    issues = blocked.json()['detail']['issues']
    codes = {issue['code'] for issue in issues}
    assert 'missing_row' in codes and 'count_difference' in codes


def test_handover_detail_exposes_missing_remainder_and_differences(client):
    analyst = auth(client, 'analyst')
    handover = create_handover(client, analyst, '差异清单',
                                list_items(ROWS[:6]) +
                                [{'day': '2026-09-30', 'team': 'A队', 'zone': '中心区',
                                  'passed': 8, 'observed': 9}])
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '差异日表', ROWS[:6], handover['id'])
    client.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                json={'lines': claim_lines(ROWS[:6], by_key, [0], passed=4, observed=4)})
    detail = client.get(f"/handovers/{handover['id']}", headers=analyst).json()
    assert detail['missing_count'] == 6
    assert detail['remainder_count'] == 7
    assert detail['difference_count'] == 6
    assert detail['count_differences'][0]['passed_difference'] == int(ROWS[0]['passed']) - 4
    assert detail['count_differences'][0]['observed_difference'] == int(ROWS[0]['observed']) - 4
    report = client.get(f"/handovers/{handover['id']}/reconciliation", headers=analyst).json()
    assert report['batches_detail'][0]['rows'][0]['claim']['passed'] == 4
    assert report['batches_detail'][0]['rows'][0]['claim']['observed'] == 4
    assert report['row_differences']


def test_interrupted_confirmation_recovers_without_ledger_change(client):
    """确认中断留下 confirming 时，恢复原账目且重试不重复扣减或新建批次。"""
    analyst, admin = auth(client, 'analyst'), auth(client, 'admin')
    handover = create_handover(client, analyst, '中断清单', list_items(ROWS[:8]))
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '中断日表', ROWS[:8], handover['id'])
    client.post(f"/datasets/{dataset['id']}/claims", headers=analyst,
                json={'lines': claim_lines(ROWS[:8], by_key, range(8))})
    before = client.get(f"/handovers/{handover['id']}", headers=analyst).json()
    with Session() as db:
        reconciliation = db.scalar(select(BatchReconciliation).where(
            BatchReconciliation.dataset_id == dataset['id']))
        reconciliation.status = 'confirming'
        db.commit()
    # 未完成确认的批次不开放分析
    blocked = client.post('/jobs', headers=analyst,
                          json={'name': '中断期分析', 'dataset_id': dataset['id'], 'params': {}})
    assert blocked.status_code == 409
    from src.services import handovers as service
    with Session() as db:
        recovered = service.recover_interrupted(db)
        db.commit()
    assert dataset['id'] in recovered
    after = client.get(f"/handovers/{handover['id']}", headers=analyst).json()
    assert after['totals'] == before['totals']
    assert client.get(f"/datasets/{dataset['id']}/reconciliation",
                      headers=analyst).json()['status'] == 'ready'
    assert client.post(f"/datasets/{dataset['id']}/confirm", headers=admin,
                       json={'idempotency_key': 'resume-key'}).status_code == 200
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(BatchReconciliation)
                         .where(BatchReconciliation.dataset_id == dataset['id'])) == 1


def test_rows_detail_exposes_item_for_unclaimed_rows(client):
    analyst = auth(client, 'analyst')
    handover = create_handover(client, analyst, '行匹配清单', list_items(ROWS[:6]))
    dataset = create_dataset(client, analyst, '行匹配日表', ROWS[:6], handover['id'])
    detail = client.get(f"/datasets/{dataset['id']}/reconciliation/rows",
                        headers=analyst).json()
    assert detail['rows']
    assert all(row['item'] and row['item_match'] for row in detail['rows'])
    assert detail['rows'][0]['item']['remaining_observed'] == int(ROWS[0]['observed'])


def test_same_key_with_different_lines_rejected(client):
    analyst = auth(client, 'analyst')
    handover = create_handover(client, analyst, '键冲突清单', list_items(ROWS[:8]))
    by_key = {key(i): i for i in handover['items']}
    dataset = create_dataset(client, analyst, '键冲突日表', ROWS[:8], handover['id'])
    first = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst, json={
        'lines': claim_lines(ROWS[:8], by_key, [0]), 'idempotency_key': 'same-key'})
    assert first.status_code == 201
    second = client.post(f"/datasets/{dataset['id']}/claims", headers=analyst, json={
        'lines': claim_lines(ROWS[:8], by_key, [1]), 'idempotency_key': 'same-key'})
    assert second.status_code == 409
