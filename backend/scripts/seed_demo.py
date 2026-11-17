import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, Claim, HandoverItem, HandoverList
from src.services.business import metadata, seed_records, analyze
from src.services import handovers
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        _seed_handover(db, d)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
def _seed_handover(db, baseline):
    """演示用对账数据：基线批次保持旧批次；另建一份 10 月日表并挂上纸质清单。

    清单含 120 项（与日表同键）外加 1 项日表没有的 09-30 记录，演示缺项与余量。
    日表先认领前两天 8 行，接入页因此能看到“待补齐”和逐行差异。
    """
    name = '10 月纸质交接清单'
    if db.scalar(select(HandoverList).where(HandoverList.name == name)):
        return
    admin = db.scalar(select(User).where(User.role == 'admin'))
    rows = baseline.records
    daily = db.scalar(select(Dataset).where(Dataset.name == '10 月两队日表 · 补录'))
    if not daily:
        daily = Dataset(name='10 月两队日表 · 补录', source='两队日表-202610.csv',
                        records=rows, checksum=checksum(rows), created_at=time.time())
        db.add(daily); db.flush()
    handover = HandoverList(name=name, source='纸质交接清单 · 10 月', note='两队日汇总誊抄',
                            created_by=admin.id, created_at=time.time())
    db.add(handover); db.flush()
    for row in rows:
        db.add(HandoverItem(handover_id=handover.id, day=row['day'], team=row['team'],
                            zone=row['zone'], passed=int(row['passed']),
                            observed=int(row['observed']), claimed_passed=0,
                            claimed_observed=0, created_at=time.time()))
    db.add(HandoverItem(handover_id=handover.id, day='2026-09-30', team='A队', zone='中心区',
                        passed=8, observed=9, claimed_passed=0, claimed_observed=0,
                        created_at=time.time()))
    db.flush()
    reconciliation = handovers.attach_dataset(db, daily, handover.id, admin)
    db.flush()
    items = {item.day + item.team + item.zone: item
             for item in db.scalars(select(HandoverItem).where(HandoverItem.handover_id == handover.id))}
    for index in range(8):
        row = rows[index]
        item = items[row['day'] + row['team'] + row['zone']]
        claim = Claim(handover_id=handover.id, item_id=item.id, dataset_id=daily.id,
                      row_index=index, passed=int(row['passed']), observed=int(row['observed']),
                      row_passed=int(row['passed']), row_observed=int(row['observed']),
                      state='active', created_by=admin.id, created_at=time.time())
        item.claimed_passed += claim.passed
        item.claimed_observed += claim.observed
        db.add(claim)
    db.flush()
    db.add(Audit(actor='seed', action='handover.seed',
                 detail={'handover': handover.id, 'dataset': daily.id}, at=time.time()))

if __name__=='__main__':seed()
