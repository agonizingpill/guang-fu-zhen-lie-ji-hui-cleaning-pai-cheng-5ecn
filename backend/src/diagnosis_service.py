from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import select,func
from .models import ArrayBlock,SoilingSurvey,WashMission,Audit
from .diagnosis_models import StringDiagnostic,StringCurrentSample,StringFault,WashSuspension
from .diagnosis_algorithm import evaluate,evaluate_recheck,MIN_HEALTHY
from .field_service import require,audit


def _audit(db,actor,message):
    db.add(Audit(actor=actor,action=message))


def _samples(rows):
    return [{"string_code":s.string_code,"current_a":s.current_a,"healthy":s.healthy} for s in rows]


def _add_samples(diagnostic,rows):
    for s in rows:
        diagnostic.samples.append(StringCurrentSample(string_code=s.string_code,current_a=s.current_a,healthy=s.healthy))


def register(db,body,user,queue):
    """登记初诊。写库与入队同一流程；重复编号返回已有诊断，便于中断后重试而不新建。"""
    existing=db.scalar(select(StringDiagnostic).where(StringDiagnostic.code==body.code))
    if existing:
        raise HTTPException(409,'诊断编号已存在，请在列表中使用“重试”恢复中断的诊断')
    block=require(db,ArrayBlock,body.block_id)
    diagnostic=StringDiagnostic(code=body.code,block_id=block.id,kind='initial',state='pending',created_by=user.email)
    _add_samples(diagnostic,body.samples)
    db.add(diagnostic);_audit(db,user.email,f'登记组串诊断 {body.code} 阵列{block.code} 样本{len(body.samples)}个')
    db.commit()
    queue.rpush('solar:string-diagnosis',str(diagnostic.id))
    return diagnostic


def retry(db,key,user,queue):
    """中断恢复：重排未完成的诊断。已完成的诊断不重复执行。"""
    diagnostic=require(db,StringDiagnostic,key,True)
    if diagnostic.state=='completed':
        raise HTTPException(409,'诊断已完成，无需重试；异常处理请查看故障登记')
    if diagnostic.state=='manual':
        raise HTTPException(409,'样本不足已转人工，请补充确认健康样本后以新编号登记，不能重试同一批样本')
    if diagnostic.kind=='recheck':
        fault=_lock_fault(db,diagnostic.fault_id)
        if fault.latest_diagnostic_id!=diagnostic.id:
            raise HTTPException(409,'该复诊已被新读数替代，不能重试')
        if fault.state!='rechecking':
            raise HTTPException(409,'故障当前不在复诊阶段，请刷新后再试')
    diagnostic.state='pending';diagnostic.error='';diagnostic.last_enqueued_at=datetime.utcnow()
    _audit(db,user.email,f'重试组串诊断 {diagnostic.code}（原状态已中断或待补样本）')
    db.commit()
    queue.rpush('solar:string-diagnosis',str(diagnostic.id))
    return diagnostic


def repair(db,key,body,user):
    """主管确认维修完成，进入待复诊。复诊正常前资格保持挂起。

    复诊判定中仍可再次确认维修（现场又处理了一次）：这会开启新一轮复诊，
    在途的旧复诊从此不是最新，晚回结果只留档、不能解禁。
    """
    fault=_lock_fault(db,key)
    block=require(db,ArrayBlock,fault.block_id)
    if fault.state=='recovered':raise HTTPException(409,'故障已恢复，不可重复登记维修')
    if fault.state not in ['awaiting_repair','rechecking']:raise HTTPException(409,'待复诊故障已登记复诊，请提交复诊读数')
    superseded=fault.state=='rechecking'
    fault.state='repaired';fault.repaired_by=user.email;fault.repaired_at=datetime.utcnow();fault.repair_note=body.repair_note
    fault.recheck_result=''
    # 开启新一轮复诊资格；上一轮复诊诊断从此不再是最新，晚回也不会解禁。
    fault.recheck_diagnostic_id=None
    note='（原在途复诊作废，晚回只留档）' if superseded else ''
    _audit(db,user.email,f'确认维修组串故障 {key} 阵列{block.code}：{body.repair_note}{note}')
    db.commit();return fault


def recheck(db,key,body,user,queue):
    """主管维修后登记新读数复诊；结果由 worker 判定，正常才解禁。"""
    fault=_lock_fault(db,key)
    block=require(db,ArrayBlock,fault.block_id)
    if fault.state=='recovered':raise HTTPException(409,'故障已恢复，无需复诊')
    if fault.state=='awaiting_repair':raise HTTPException(409,'须先由主管确认维修，才能登记复诊读数')
    if fault.state!='repaired':raise HTTPException(409,'复诊判定中，请等待当前结果')
    if fault.recheck_diagnostic_id:
        raise HTTPException(409,'本次维修已有复诊记录，请等待判定或重新确认维修')
    base=(fault.diagnostic_code or f'F{fault.id}').strip()
    seq=db.scalar(select(func.count()).select_from(StringDiagnostic).where(StringDiagnostic.fault_id==fault.id))+1
    diagnostic=StringDiagnostic(code=f'{base}-R{seq}',block_id=block.id,kind='recheck',fault_id=fault.id,state='pending',created_by=user.email)
    _add_samples(diagnostic,body.samples)
    db.add(diagnostic);db.flush()
    fault.latest_diagnostic_id=diagnostic.id
    fault.recheck_diagnostic_id=diagnostic.id
    fault.state='rechecking'
    _audit(db,user.email,f'登记故障 {key} 维修后复诊，样本{len(body.samples)}个')
    db.commit()
    queue.rpush('solar:string-diagnosis',str(diagnostic.id))
    return diagnostic


def dispatch_guard(db,block):
    """派工前检查隐患：故障或挂起状态一律拒绝。调用方必须已锁定阵列行。"""
    suspension=db.scalar(select(WashSuspension).where(WashSuspension.block_id==block.id).with_for_update())
    if suspension and suspension.active:
        raise HTTPException(409,'该阵列因组串故障已挂起清洗资格，维修复诊正常前不得派工')
    active=db.scalar(select(StringFault).where(StringFault.block_id==block.id,StringFault.state.in_(['awaiting_repair','repaired','rechecking'])))
    if active:
        raise HTTPException(409,'该阵列存在未恢复的组串故障，维修复诊正常前不得派工')


def get_suspension(db,block_id,lock=False):
    query=select(WashSuspension).where(WashSuspension.block_id==block_id)
    if lock:query=query.with_for_update()
    return db.scalar(query)


def _lock_block_missions(db,block_id):
    """派工链路按 任务→巡测→阵列 加锁；诊断落库也先取涉及的任务行，保持同一顺序。"""
    missions=db.scalars(select(WashMission).join(SoilingSurvey,SoilingSurvey.id==WashMission.survey_id).where(SoilingSurvey.block_id==block_id).order_by(WashMission.id).with_for_update()).all()
    return missions


def _lock_fault(db,key):
    """全局加锁顺序：任务→阵列→挂起台账→故障单。先读 block_id 再加锁，避免与派工反向。"""
    ref=db.get(StringFault,key)
    if not ref:raise HTTPException(404,'故障记录不存在')
    _lock_block_missions(db,ref.block_id)
    require(db,ArrayBlock,ref.block_id,True)
    get_suspension(db,ref.block_id,True)
    return db.scalar(select(StringFault).where(StringFault.id==key).with_for_update().execution_options(populate_existing=True))


def apply_result(db,key,result):
    """worker 判定落库。diagnostic 行已锁；返回 True 表示处理成功。"""
    diagnostic=db.get(StringDiagnostic,key)
    if not diagnostic or diagnostic.state not in ['pending','evaluating']:return False
    # 按 任务→阵列→挂起台账 顺序取锁，与派工链路一致：
    # 要么派工先提交则随后停工，要么诊断先挂起则派工被拒。
    _lock_block_missions(db,diagnostic.block_id)
    block=require(db,ArrayBlock,diagnostic.block_id,True)
    suspension=get_suspension(db,block.id,True)
    diagnostic.state=result['status']
    diagnostic.evidence=result['evidence']
    diagnostic.anomaly_strings=result['anomalies']
    diagnostic.error=result['evidence'].get('reason','') if result['status']!='completed' else ''
    diagnostic.finished_at=datetime.utcnow()

    if diagnostic.kind=='recheck':
        _apply_recheck(db,diagnostic,block,result)
        return True

    if result['status']!='completed':
        _audit(db,'system',f'组串诊断 {diagnostic.code} 转人工：{diagnostic.error}')
        return True
    if not result['anomalies']:
        _audit(db,'system',f'组串诊断 {diagnostic.code} 未发现异常')
        return True
    _apply_anomaly(db,diagnostic,block,suspension,result)
    return True


def _apply_anomaly(db,diagnostic,block,suspension,result):
    existing=db.scalar(select(StringFault).where(StringFault.block_id==block.id,StringFault.state.in_(['awaiting_repair','repaired','rechecking'])).order_by(StringFault.id).with_for_update())
    if existing:
        # 同一阵列已有未恢复故障：只追加依据，不重复建单、不改动维修/复诊进度。
        _audit(db,'system',f'组串诊断 {diagnostic.code} 发现异常{",".join(result["anomalies"])}，附属于既有故障 {existing.id}')
        return
    create_fault(db,block,suspension,result['anomalies'],{**result['evidence'],'diagnostic_id':diagnostic.id,'diagnostic_code':diagnostic.code,'block_code':block.code},diagnostic.id,'system',f'组串异常：{",".join(result["anomalies"])}')


def create_fault(db,block,suspension,strings,evidence,diagnostic_id,actor,reason,note=''):
    """建立故障单并挂起资格；若已有执行中任务则现场停工，已扣水量不返还。
    调用方须已按全局顺序锁定 任务/阵列/挂起台账。"""
    mission=db.scalar(select(WashMission).join(SoilingSurvey,SoilingSurvey.id==WashMission.survey_id).where(SoilingSurvey.block_id==block.id,WashMission.status=='dispatched').order_by(WashMission.id).with_for_update())
    fault=StringFault(
        block_id=block.id,
        diagnostic_id=diagnostic_id,
        diagnostic_code=(evidence or {}).get('diagnostic_code',''),
        latest_diagnostic_id=diagnostic_id,
        state='awaiting_repair',
        strings=list(strings),
        evidence=evidence,
        reported_by=actor,
        note=note,
    )
    if mission:
        # 派工先到：现场任务立即停工，已扣水量保留不自动返还。
        mission.status='stopped'
        fault.stop_work=True
        fault.mission_id=mission.id
        fault.stop_water_liters=mission.water_liters
        fault.stop_at=datetime.utcnow()
    db.add(fault);db.flush()
    if suspension:
        suspension.active=True;suspension.fault_id=fault.id;suspension.reason=reason;suspension.suspended_by=actor;suspension.suspended_at=datetime.utcnow();suspension.cleared_by='';suspension.cleared_at=None
    else:
        db.add(WashSuspension(block_id=block.id,active=True,fault_id=fault.id,reason=reason,suspended_by=actor,suspended_at=datetime.utcnow()))
    stop_note='，现场清洗转停工（用水不返还）' if mission else ''
    _audit(db,actor,f'登记组串故障 {fault.id} 阵列{block.code} 异常{",".join(strings)}，挂起清洗资格{stop_note}')
    return fault


def report(db,body,user):
    """现场人工登记故障；与派工同序加锁，保证先登记则拒派。"""
    block=require(db,ArrayBlock,body.block_id)
    _lock_block_missions(db,block.id)
    block=require(db,ArrayBlock,block.id,True)
    suspension=get_suspension(db,block.id,True)
    existing=db.scalar(select(StringFault).where(StringFault.block_id==block.id,StringFault.state.in_(['awaiting_repair','repaired','rechecking'])).order_by(StringFault.id).with_for_update())
    if existing:raise HTTPException(409,'该阵列已有未恢复的组串故障，请先完成维修与复诊')
    fault=create_fault(db,block,suspension,body.strings,{'source':'manual','note':body.note,'block_code':block.code},None,user.email,f'现场登记：{body.note}')
    db.commit();return fault


def _apply_recheck(db,diagnostic,block,result):
    fault=db.scalar(select(StringFault).where(StringFault.id==diagnostic.fault_id).with_for_update())
    if not fault:
        _audit(db,'system',f'复诊 {diagnostic.code} 找不到故障单，结果仅留档')
        return
    # 只有当前故障的最新复诊可以改变状态；旧诊断晚回一律只留档。
    if fault.latest_diagnostic_id!=diagnostic.id or fault.state!='rechecking':
        _audit(db,'system',f'复诊 {diagnostic.code} 非最新复诊，结果留档但不改变解禁状态')
        return
    fault.recheck_strings=result.get('anomalies') or result['evidence'].get('anomaly_strings',[])
    fault.recheck_evidence={**result['evidence'],'diagnostic_id':diagnostic.id,'diagnostic_code':diagnostic.code}
    if result['status']!='completed':
        fault.state='awaiting_repair';fault.recheck_result='manual'
        _audit(db,'system',f'复诊 {diagnostic.code} 转人工，故障{fault.id}继续挂起')
        return
    if result['anomalies']:
        fault.state='awaiting_repair';fault.recheck_result='anomalous'
        _audit(db,'system',f'复诊 {diagnostic.code} 仍有异常{",".join(result["anomalies"])}，故障{fault.id}继续挂起')
        return
    fault.state='recovered';fault.recheck_result='normal';fault.recovered_at=datetime.utcnow();fault.recovered_by='system'
    suspension=get_suspension(db,block.id)
    if suspension and suspension.active and suspension.fault_id==fault.id:
        suspension.active=False;suspension.cleared_by='system';suspension.cleared_at=datetime.utcnow();suspension.reason=f'故障{fault.id}复诊正常已解禁'
    _release_stopped_work(db,block,fault)
    _audit(db,'system',f'故障{fault.id}复诊正常，恢复阵列{block.code}清洗资格')


def _release_stopped_work(db,block,fault):
    """停工任务为终态：保留已扣水量与记录，但不继续占用阵列。

    恢复资格后阵列回到可巡检，需重新巡测、重新计划并重新扣水派工；
    原停工任务的巡测标记 stale，避免用旧读数再次派工。
    """
    if not fault.stop_work or not fault.mission_id:return
    mission=db.get(WashMission,fault.mission_id)
    if not mission or mission.status!='stopped':return
    survey=db.get(SoilingSurvey,mission.survey_id)
    if survey and survey.state=='scheduled':survey.state='stale'
    other=db.scalar(select(WashMission).join(SoilingSurvey,SoilingSurvey.id==WashMission.survey_id).where(SoilingSurvey.block_id==block.id,WashMission.status=='dispatched',WashMission.id!=mission.id))
    if not other and block.cleaning_state=='washing':block.cleaning_state='ready'
    _audit(db,'system',f'停工任务{mission.id}保留已扣{mission.water_liters}L，阵列{block.code}解除清洗占用')
