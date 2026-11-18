import os
import redis
from fastapi import APIRouter,Depends
from sqlalchemy import select
from .database import session
from .auth import current,editor,admin
from .diagnosis_models import StringDiagnostic,StringFault,WashSuspension
from .diagnosis_schemas import DiagnosticInput,RecheckInput,RepairInput,FaultReportInput
from . import diagnosis_service
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
router=APIRouter(prefix='/string-diagnostics')


def _diag_list(items):return [{'id':d.id,'code':d.code,'block_id':d.block_id,'kind':d.kind,'state':d.state,'fault_id':d.fault_id,'anomaly_strings':d.anomaly_strings,'evidence':d.evidence,'error':d.error,'created_by':d.created_by,'created_at':d.created_at,'finished_at':d.finished_at,'samples':[{'string_code':s.string_code,'current_a':s.current_a,'healthy':s.healthy} for s in d.samples]} for d in items]


@router.get('')
def diagnostics(user=Depends(current),db=Depends(session)):
    return _diag_list(db.scalars(select(StringDiagnostic).order_by(StringDiagnostic.id.desc())).all())


@router.get('/faults')
def faults(user=Depends(current),db=Depends(session)):
    return db.scalars(select(StringFault).order_by(StringFault.id.desc())).all()


@router.get('/suspensions')
def suspensions(user=Depends(current),db=Depends(session)):
    return db.scalars(select(WashSuspension).order_by(WashSuspension.block_id)).all()


@router.post('')
def register(body:DiagnosticInput,user=Depends(editor),db=Depends(session)):
    return diagnosis_service.register(db,body,user,queue)


@router.post('/{key}/retry')
def retry(key:int,user=Depends(editor),db=Depends(session)):
    return diagnosis_service.retry(db,key,user,queue)


@router.post('/faults')
def report(body:FaultReportInput,user=Depends(editor),db=Depends(session)):
    return diagnosis_service.report(db,body,user)


@router.post('/faults/{key}/repair')
def repair(key:int,body:RepairInput,user=Depends(admin),db=Depends(session)):
    return diagnosis_service.repair(db,key,body,user)


@router.post('/faults/{key}/recheck')
def recheck(key:int,body:RecheckInput,user=Depends(admin),db=Depends(session)):
    return diagnosis_service.recheck(db,key,body,user,queue)
