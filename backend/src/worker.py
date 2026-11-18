import os,time,json,traceback
from pathlib import Path
from sqlalchemy import select
import redis
from .database import Session
from .models import ArrayBlock,SoilingSurvey
from .power_model import estimate
from .diagnosis_models import StringDiagnostic,StringFault
from .diagnosis_service import apply_result
from .diagnosis_algorithm import evaluate,evaluate_recheck
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
ARTIFACTS=Path(os.getenv('ARTIFACT_DIR','/artifacts'))


def _write(name,payload):
    try:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS/name).write_text(json.dumps(payload,ensure_ascii=False))
    except Exception:traceback.print_exc()


def assess(key):
    key=int(key)
    try:
        with Session() as db:
            survey=db.scalar(select(SoilingSurvey).where(SoilingSurvey.id==key).with_for_update())
            if not survey or survey.state!='evaluating':return
            block=db.get(ArrayBlock,survey.block_id)
            clean,loss,evidence=estimate(survey)
            survey.clean_kw=clean;survey.predicted_loss=loss;survey.model_evidence=evidence
            survey.recommended_water=round(block.area_m2*.6*(1+.1*survey.dust),1)
            survey.state='assessed';db.commit()
            _write(f'survey-{key}.json',{'survey_id':key,'clean_kw':clean,'loss_kw':loss,'model':evidence})
    except Exception as error:
        traceback.print_exc()
        with Session() as db:
            survey=db.get(SoilingSurvey,key)
            if survey:survey.state='blocked';survey.model_evidence={'error':str(error)};db.commit()


def diagnose_one(key):
    """消费一条组串诊断。先用独立 Session 读快照做计算，再在短事务内锁行落库，
    保证人工重试与旧诊断晚回都不会覆盖新结果。"""
    samples=None;kind=None;fault_id=None;baseline=None
    with Session() as db:
        diagnostic=db.get(StringDiagnostic,key)
        if not diagnostic or diagnostic.state not in ['pending','evaluating']:return
        kind=diagnostic.kind;fault_id=diagnostic.fault_id
        samples=[{'string_code':s.string_code,'current_a':s.current_a,'healthy':s.healthy} for s in diagnostic.samples]
        if kind=='recheck' and fault_id:
            fault=db.get(StringFault,fault_id)
            if fault and fault.state=='rechecking' and fault.latest_diagnostic_id==diagnostic.id:
                baseline=dict(fault.evidence or {})
                baseline['diagnostic_id']=fault.diagnostic_id
    if not samples:return
    try:
        if kind=='recheck':
            result=evaluate_recheck(samples,baseline) if baseline is not None else None
            if result is None:
                # 初诊依据缺失时，退回复诊样本自身的完整诊断，并要求其中含确认健康样本。
                result=evaluate(samples)
                result['evidence']['fallback']='初诊正常分布缺失，改以本次确认健康样本重新学习'
        else:
            result=evaluate(samples)
    except Exception as error:
        traceback.print_exc()
        result={'status':'blocked','anomalies':[],'scores':[],'threshold':None,'evidence':{'status':'blocked','reason':f'诊断计算失败：{error}','sample_count':len(samples)}}
    with Session() as db:
        diagnostic=db.scalar(select(StringDiagnostic).where(StringDiagnostic.id==key).with_for_update().execution_options(populate_existing=True))
        if not diagnostic or diagnostic.state not in ['pending','evaluating']:return
        apply_result(db,key,result)
        db.commit()
    _write(f'string-diagnosis-{key}.json',{'diagnostic_id':key,'status':result['status'],'anomalies':result['anomalies'],'evidence':result['evidence']})


def main():
    while True:
        Path('/tmp/solar-heartbeat').touch()
        # 同时阻塞在两条队列上，避免估损任务持续到达时饿死组串诊断。
        job=queue.blpop(['solar:assessment','solar:string-diagnosis'],timeout=3)
        if not job:continue
        name,value=job
        try:
            if name=='solar:assessment':assess(value)
            else:diagnose_one(int(value))
        except Exception:traceback.print_exc()


if __name__=='__main__':main()
