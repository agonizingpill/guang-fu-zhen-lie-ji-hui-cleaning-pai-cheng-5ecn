from datetime import datetime
from sqlalchemy import String,Float,ForeignKey,JSON,CheckConstraint,DateTime,Boolean,func
from sqlalchemy.orm import Mapped,mapped_column,relationship
from .database import Base


class StringDiagnostic(Base):
    """一次组串电流诊断。healthy=True 的样本用于学习正常分布，其余为待判定组串。"""
    __tablename__='string_diagnostics'
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(50),unique=True)
    block_id:Mapped[int]=mapped_column(ForeignKey('array_blocks.id'))
    kind:Mapped[str]=mapped_column(String(16),default='initial')
    # 复诊诊断直接记录故障主键；不建外键，避免与 fault.diagnostic_id 形成建表环。
    fault_id:Mapped[int|None]=mapped_column(nullable=True)
    state:Mapped[str]=mapped_column(String(24),default='pending')
    anomaly_strings:Mapped[list|None]=mapped_column(JSON,nullable=True)
    evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error:Mapped[str]=mapped_column(String(500),default='')
    created_by:Mapped[str]=mapped_column(String(120),default='')
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    last_enqueued_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    finished_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    samples:Mapped[list['StringCurrentSample']]=relationship(lazy='selectin',order_by='StringCurrentSample.id',cascade='all, delete-orphan')


class StringCurrentSample(Base):
    __tablename__='string_current_samples'
    __table_args__=(CheckConstraint('current_a>0'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    diagnostic_id:Mapped[int]=mapped_column(ForeignKey('string_diagnostics.id'))
    string_code:Mapped[str]=mapped_column(String(50))
    current_a:Mapped[float]=mapped_column(Float)
    healthy:Mapped[bool]=mapped_column(Boolean,default=False)


class StringFault(Base):
    __tablename__='string_faults'
    id:Mapped[int]=mapped_column(primary_key=True)
    block_id:Mapped[int]=mapped_column(ForeignKey('array_blocks.id'))
    # 同一诊断只对应一张故障单：重试复用，不重复新建。
    diagnostic_id:Mapped[int|None]=mapped_column(ForeignKey('string_diagnostics.id'),unique=True,nullable=True)
    diagnostic_code:Mapped[str]=mapped_column(String(50),default='')
    latest_diagnostic_id:Mapped[int|None]=mapped_column(nullable=True)
    state:Mapped[str]=mapped_column(String(24),default='awaiting_repair')
    strings:Mapped[list|None]=mapped_column(JSON,nullable=True)
    evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    recheck_strings:Mapped[list|None]=mapped_column(JSON,nullable=True)
    recheck_evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    recheck_result:Mapped[str]=mapped_column(String(24),default='')
    stop_work:Mapped[bool]=mapped_column(Boolean,default=False)
    mission_id:Mapped[int|None]=mapped_column(ForeignKey('wash_missions.id'),nullable=True)
    stop_water_liters:Mapped[float|None]=mapped_column(Float,nullable=True)
    stop_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    note:Mapped[str]=mapped_column(String(500),default='')
    reported_by:Mapped[str]=mapped_column(String(120),default='')
    reported_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    repaired_by:Mapped[str]=mapped_column(String(120),default='')
    repaired_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    repair_note:Mapped[str]=mapped_column(String(500),default='')
    recheck_diagnostic_id:Mapped[int|None]=mapped_column(ForeignKey('string_diagnostics.id'),nullable=True)
    recovered_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    recovered_by:Mapped[str]=mapped_column(String(120),default='')


class WashSuspension(Base):
    """阵列清洗资格挂起台账；一阵列一行，仅当前故障可解禁。"""
    __tablename__='wash_suspensions'
    id:Mapped[int]=mapped_column(primary_key=True)
    block_id:Mapped[int]=mapped_column(ForeignKey('array_blocks.id'),unique=True)
    active:Mapped[bool]=mapped_column(Boolean,default=False)
    fault_id:Mapped[int|None]=mapped_column(ForeignKey('string_faults.id'),nullable=True)
    reason:Mapped[str]=mapped_column(String(300),default='')
    suspended_by:Mapped[str]=mapped_column(String(120),default='')
    suspended_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    cleared_by:Mapped[str]=mapped_column(String(120),default='')
    cleared_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
