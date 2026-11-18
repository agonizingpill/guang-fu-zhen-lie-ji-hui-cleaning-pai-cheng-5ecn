from datetime import datetime,timedelta
from sqlalchemy import select,func
from src.database import Base,engine,Session
from src.models import User,ArrayBlock,SoilingSurvey,WashMission
from src.revisit_models import WaterDelivery,FollowupSurvey
from src.auth import hash_password
from src.operations_models import WashCrew
from src.diagnosis_models import StringDiagnostic,StringCurrentSample,StringFault,WashSuspension

def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ['admin','planner','viewer']:
            email=role+'@qingling.test'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,password=hash_password('Solar2026!'),role=role))
        for code,minutes in [('CREW-SOUTH',480),('CREW-RIDGE',360)]:
            if not db.scalar(select(WashCrew).where(WashCrew.code==code)):db.add(WashCrew(code=code,max_daily_minutes=minutes))
        blocks=[('SL-A01','南坡一号','南坡',150,23,780,1500),('SL-A02','南坡二号','南坡',120,25,630,1200),('SL-B01','脊线一号','脊线',100,18,520,900),('SL-C01','西坡一号','西坡',80,30,420,600)]
        for code,name,sector,cap,slope,area,water in blocks:
            if not db.scalar(select(ArrayBlock).where(ArrayBlock.code==code)):db.add(ArrayBlock(code=code,name=name,sector=sector,capacity_kw=cap,slope=slope,area_m2=area,water_liters=water))
        db.flush();rows=db.scalars(select(ArrayBlock).order_by(ArrayBlock.id)).all()
        measurements=[('SV-1117-A01',rows[0],800,35,18,3.2,88),('SV-1117-A02',rows[1],820,33,12,2.1,76),('SV-CLOUD-01',rows[2],240,23,8,1.2,18)]
        for code,block,irr,temp,rain,dust,power in measurements:
            if not db.scalar(select(SoilingSurvey).where(SoilingSurvey.code==code)):db.add(SoilingSurvey(code=code,block_id=block.id,capacity_kw=block.capacity_kw,slope=block.slope,irradiance=irr,module_temp=temp,rain_days=rain,dust=dust,observed_kw=power,state='recorded'))
        db.flush()
        block=db.scalar(select(ArrayBlock).where(ArrayBlock.code=='SL-C01'))
        if not db.scalar(select(WaterDelivery).where(WaterDelivery.delivery_code=='WD-DEMO-01')):
            db.add(WaterDelivery(block_id=block.id,delivery_code='WD-DEMO-01',liters=500,source='西坡蓄水池 / 水车02'))
        historical=db.scalar(select(SoilingSurvey).where(SoilingSurvey.code=='SV-DEMO-CLEAN'))
        if not historical:
            historical=SoilingSurvey(code='SV-DEMO-CLEAN',block_id=block.id,capacity_kw=block.capacity_kw,slope=block.slope,irradiance=800,module_temp=35,rain_days=15,dust=2,observed_kw=50,clean_kw=62,predicted_loss=12,recommended_water=100,state='closed')
            db.add(historical);db.flush()
        mission=db.scalar(select(WashMission).where(WashMission.survey_id==historical.id))
        if not mission:
            mission=WashMission(survey_id=historical.id,water_liters=100,available_liters=700,status='accepted',post_kw=62,post_irradiance=800,normalized_kw=62,verified_gain=12,dispatched_at=datetime.utcnow()-timedelta(days=7),accepted_at=datetime.utcnow()-timedelta(days=7))
            db.add(mission);db.flush()
        if not db.scalar(select(FollowupSurvey).where(FollowupSurvey.mission_id==mission.id,FollowupSurvey.days_after==2)):
            db.add(FollowupSurvey(mission_id=mission.id,days_after=2,irradiance=800,observed_kw=61,normalized_kw=61,normalized_loss=100/62,state='stable',baseline_snapshot={'post_kw':62,'post_irradiance':800,'accepted_at':mission.accepted_at.isoformat(),'block_id':block.id,'block_code':block.code,'survey_id':historical.id,'survey_code':historical.code,'threshold_pct':5}))
        # 组串诊断演示：A02 一条已完成且建故障单，A01 一条样本不足转人工。均只补缺项，不改人工结果。
        a02=db.scalar(select(ArrayBlock).where(ArrayBlock.code=='SL-A02'))
        demo=db.scalar(select(StringDiagnostic).where(StringDiagnostic.code=='DG-DEMO-A02'))
        if not demo:
            demo=StringDiagnostic(code='DG-DEMO-A02',block_id=a02.id,kind='initial',state='completed',anomaly_strings=['S4'],created_by='admin@qingling.test',evidence={'status':'completed','method':'robust-z: 健康样本 median/MAD；独立留出校准阈值','median_a':10.0,'mad_a':0.1,'sigma_a':0.148,'threshold':3.15,'train_count':4,'holdout_count':2,'healthy_count':6,'sample_count':8,'samples_sha256':'demo-seed','train_samples':[{'string_code':f'S{i}','current_a':c} for i,c in[(1,10.0),(2,10.1),(3,9.9),(5,10.2)]],'holdout_samples':[{'string_code':'S6','current_a':10.0},{'string_code':'S7','current_a':9.8}],'candidate_scores':[{'string_code':'S4','current_a':4.3,'score':38.5,'anomalous':True}]})
            for code,current,healthy in [('S1',10.0,True),('S2',10.1,True),('S3',9.9,True),('S5',10.2,True),('S6',10.0,True),('S7',9.8,True),('S4',4.3,False),('S8',10.0,False)]:
                demo.samples.append(StringCurrentSample(string_code=code,current_a=current,healthy=healthy))
            db.add(demo);db.flush()
        if not db.scalar(select(StringFault).where(StringFault.diagnostic_id==demo.id)):
            fault=StringFault(block_id=a02.id,diagnostic_id=demo.id,diagnostic_code=demo.code,latest_diagnostic_id=demo.id,state='awaiting_repair',strings=['S4'],evidence={**demo.evidence,'diagnostic_id':demo.id,'diagnostic_code':demo.code,'block_code':a02.code},reported_by='system',note='演示数据：组串S4电流异常')
            db.add(fault);db.flush()
            suspension=db.scalar(select(WashSuspension).where(WashSuspension.block_id==a02.id))
            if suspension:suspension.active=True;suspension.fault_id=fault.id;suspension.reason='组串异常：S4';suspension.suspended_by='system';suspension.suspended_at=datetime.utcnow()
            else:db.add(WashSuspension(block_id=a02.id,active=True,fault_id=fault.id,reason='组串异常：S4',suspended_by='system',suspended_at=datetime.utcnow()))
        if not db.scalar(select(StringDiagnostic).where(StringDiagnostic.code=='DG-DEMO-A01')):
            short=StringDiagnostic(code='DG-DEMO-A01',block_id=rows[0].id,kind='initial',state='manual',created_by='planner@qingling.test',error='确认健康样本仅3个，少于6个，转人工补充健康样本',evidence={'status':'manual','reason':'确认健康样本仅3个，少于6个，转人工补充健康样本','healthy_count':3,'sample_count':5,'samples_sha256':'demo-seed-short','method':'robust-z: 健康样本 median/MAD；独立留出校准阈值'})
            for code,current,healthy in [('S1',10.0,True),('S2',10.1,True),('S3',9.9,True),('S4',4.3,False),('S5',10.0,False)]:
                short.samples.append(StringCurrentSample(string_code=code,current_a=current,healthy=healthy))
            db.add(short)
        db.commit();print({key:db.scalar(select(func.count()).select_from(model)) for key,model in [('users',User),('blocks',ArrayBlock),('surveys',SoilingSurvey),('diagnostics',StringDiagnostic),('faults',StringFault)]})
if __name__=='__main__':seed()
