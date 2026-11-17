from sqlalchemy import String, Double, Integer, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class HandoverList(Base):
    """纸质交接清单：各日、各队、各区域的合格数与观察数总额。"""

    __tablename__ = 'handover_lists'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    source: Mapped[str] = mapped_column(String(200), default='纸质交接清单')
    note: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class HandoverItem(Base):
    """清单中的一个日/队/区域行，同时保存可并发扣减的两项余额。"""

    __tablename__ = 'handover_items'
    __table_args__ = (UniqueConstraint('handover_id', 'day', 'team', 'zone', name='uq_handover_item_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey('handover_lists.id'))
    day: Mapped[str] = mapped_column(String(10))
    team: Mapped[str] = mapped_column(String(20))
    zone: Mapped[str] = mapped_column(String(60))
    passed: Mapped[int] = mapped_column(Integer)
    observed: Mapped[int] = mapped_column(Integer)
    claimed_passed: Mapped[int] = mapped_column(Integer, default=0)
    claimed_observed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Double)


class Claim(Base):
    """日表批次的一行对清单一项的认领。state: active / frozen / revoked。"""

    __tablename__ = 'claims'
    __table_args__ = (
        Index('ix_claim_handover', 'handover_id'),
        Index('ix_claim_dataset', 'dataset_id'),
        Index(
            'uq_claim_active_row',
            'dataset_id',
            'row_index',
            unique=True,
            sqlite_where=text("state != 'revoked'"),
            postgresql_where=text("state != 'revoked'"),
        ),
        Index(
            'uq_claim_idempotency',
            'dataset_id',
            'idempotency_key',
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey('handover_lists.id'))
    item_id: Mapped[int] = mapped_column(ForeignKey('handover_items.id'))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    row_index: Mapped[int] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer)
    observed: Mapped[int] = mapped_column(Integer)
    row_passed: Mapped[int] = mapped_column(Integer)
    row_observed: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default='active')
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_checksum: Mapped[str] = mapped_column(String(64), default='')
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    revoked_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class BatchReconciliation(Base):
    """批次与交接清单的关联及确认状态。

    dataset_id 唯一：一个日表批次只对一份清单，确认不会重复建立关联；
    idempotency_key 唯一：确认中断后重试命中同一记录，不会重复扣减或另建批次。
    """

    __tablename__ = 'batch_reconciliations'
    id: Mapped[int] = mapped_column(primary_key=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey('handover_lists.id'))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='draft')
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    confirmed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), default='')
