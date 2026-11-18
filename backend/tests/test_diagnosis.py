"""组串诊断端到端测试：真实 FastAPI 路由 + SQLAlchemy（SQLite 内存库）+ 假 Redis 队列。

生产用 MySQL，锁语义由 with_for_update 提供；这里只验证业务状态机与判定逻辑，
并发顺序通过显式先调用对应服务来模拟。
"""
import os, sys, json
from pathlib import Path

os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path('/tmp/diag-test.db'))
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ['SEED_DEMO'] = 'false'
os.environ['ARTIFACT_DIR'] = '/tmp/diag-artifacts'
Path('/tmp/diag-test.db').unlink(missing_ok=True)
Path('/tmp/diag-artifacts').mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.app as app_module
from src.database import Base, engine, Session
from src.models import ArrayBlock, SoilingSurvey, WashMission, User, Audit
from src.diagnosis_models import StringDiagnostic, StringFault, WashSuspension
from src.auth import hash_password


class FakeQueue:
    def __init__(self): self.jobs = []
    def rpush(self, name, value): self.jobs.append((name, value))
    def blpop(self, name, timeout=0):
        for i, (n, v) in enumerate(self.jobs):
            if n == name:
                return [n, self.jobs.pop(i)[1]]
        return None
    def ping(self): return True


queue = FakeQueue()
app_module.queue = queue
import src.diagnosis_routes as dr
import src.survey_routes as sr
dr.queue = queue
sr.queue = queue


@pytest.fixture(scope='module')
def client():
    Base.metadata.create_all(engine)
    with Session() as db:
        if not db.scalar(select(User).where(User.email == 'admin@t')):
            db.add_all([
                User(email='admin@t', password=hash_password('x'), role='admin'),
                User(email='planner@t', password=hash_password('x'), role='planner'),
                User(email='viewer@t', password=hash_password('x'), role='viewer'),
                ArrayBlock(code='B1', name='一号', sector='南坡', capacity_kw=100, slope=23, area_m2=500, water_liters=1000),
                ArrayBlock(code='B2', name='二号', sector='西坡', capacity_kw=100, slope=23, area_m2=500, water_liters=1000),
            ])
            db.commit()
    with TestClient(app_module.app) as c:
        yield c


def token(client, email):
    r = client.post('/auth/login', json={'email': email, 'password': 'x'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def block(client, code):
    return next(b for b in client.get('/arrays', headers=token(client, 'viewer@t')).json() if b['code'] == code)


def register_diag(client, code, block_id, healthy, candidates, who='planner@t'):
    samples = [{'string_code': c, 'current_a': a, 'healthy': True} for c, a in healthy] + [{'string_code': c, 'current_a': a, 'healthy': False} for c, a in candidates]
    return client.post('/string-diagnostics', json={'code': code, 'block_id': block_id, 'samples': samples}, headers=token(client, who))


def run_job():
    """模拟 worker 消费一条诊断队列。"""
    from src.worker import diagnose_one
    job = queue.blpop('solar:string-diagnosis')
    assert job, '队列为空'
    diagnose_one(int(job[1]))


def fleet(client):
    return {
        'diag': client.get('/string-diagnostics', headers=token(client, 'viewer@t')).json(),
        'faults': client.get('/string-diagnostics/faults', headers=token(client, 'viewer@t')).json(),
        'susp': client.get('/string-diagnostics/suspensions', headers=token(client, 'viewer@t')).json(),
    }


HEALTHY = [('S1', 10.0), ('S2', 10.2), ('S3', 9.8), ('S4', 10.1), ('S5', 9.9), ('S6', 10.3), ('S7', 10.0)]


def make_dispatched_mission(client, b, survey_code):
    """在指定阵列上造一条已派工任务，返回 mission_id。"""
    h = token(client, 'planner@t')
    r = client.post('/surveys', json={'code': survey_code, 'block_id': b['id'], 'irradiance': 800, 'module_temp': 30, 'rain_days': 10, 'dust': 3, 'observed_kw': 80}, headers=h)
    assert r.status_code == 200, r.text
    sid = r.json()['id']
    from src.field_service import require
    from src import wash_service
    with Session() as db:
        s = require(db, SoilingSurvey, sid, True)
        s.state = 'assessed'; s.clean_kw = 95; s.predicted_loss = 15; s.recommended_water = 300
        db.commit()
    r = client.post('/missions', json={'survey_id': sid, 'water_liters': 300}, headers=h)
    assert r.status_code == 200, r.text
    mid = r.json()['id']
    # 直接落派工（绕开许可页，仅测试诊断联动）
    with Session() as db:
        m = db.get(WashMission, mid); blk = db.get(ArrayBlock, b['id'])
        m.status = 'dispatched'; m.available_liters = blk.water_liters
        blk.water_liters -= 300; blk.cleaning_state = 'washing'
        s = db.get(SoilingSurvey, m.survey_id); s.state = 'scheduled'
        db.commit()
    return mid


def test_01_sample_shortage_goes_manual_no_suspend(client):
    b = block(client, 'B2')
    r = register_diag(client, 'DG-SHORT', b['id'], HEALTHY[:3], [('BAD', 4.0)])
    assert r.status_code == 200, r.text
    run_job()
    data = fleet(client)
    d = next(x for x in data['diag'] if x['code'] == 'DG-SHORT')
    assert d['state'] == 'manual' and d['anomaly_strings'] == []
    assert d['evidence']['status'] == 'manual'
    assert not any(f['block_id'] == b['id'] for f in data['faults'])
    assert not any(s['block_id'] == b['id'] and s['active'] for s in data['susp'])


def test_02_anomaly_suspends_and_logs_evidence(client):
    b = block(client, 'B1')
    r = register_diag(client, 'DG-ANOM', b['id'], HEALTHY, [('S8', 9.9), ('BAD', 3.1)])
    assert r.status_code == 200, r.text
    run_job()
    data = fleet(client)
    d = next(x for x in data['diag'] if x['code'] == 'DG-ANOM')
    assert d['state'] == 'completed' and d['anomaly_strings'] == ['BAD']
    ev = d['evidence']
    assert ev['train_count'] >= 4 and ev['holdout_count'] >= 2
    assert ev['threshold'] >= 3.0
    assert set(ev['holdout_samples'][0]) >= {'string_code', 'current_a'}
    fault = next(f for f in data['faults'] if f['block_id'] == b['id'])
    assert fault['state'] == 'awaiting_repair' and fault['strings'] == ['BAD']
    assert fault['evidence']['diagnostic_code'] == 'DG-ANOM'
    susp = next(s for s in data['susp'] if s['block_id'] == b['id'])
    assert susp['active'] and susp['fault_id'] == fault['id']


def test_03_dispatch_rejected_after_suspension(client):
    b = block(client, 'B1')
    h = token(client, 'planner@t')
    r = client.post('/surveys', json={'code': 'SV-B1-X', 'block_id': b['id'], 'irradiance': 800, 'module_temp': 30, 'rain_days': 10, 'dust': 3, 'observed_kw': 80}, headers=h)
    assert r.status_code == 200, r.text
    sid = r.json()['id']
    with Session() as db:
        s = db.get(SoilingSurvey, sid); s.state = 'assessed'; s.clean_kw = 95; s.predicted_loss = 15; s.recommended_water = 300; db.commit()
    r = client.post('/missions', json={'survey_id': sid, 'water_liters': 300}, headers=h)
    mid = r.json()['id']
    r = client.post(f'/missions/{mid}/dispatch', headers=token(client, 'admin@t'))
    assert r.status_code == 409 and '挂起' in r.json()['detail']


def test_04_dispatch_first_then_diagnosis_stops_work_and_keeps_water(client):
    b = block(client, 'B2')
    mid = make_dispatched_mission(client, b, 'SV-B2-WASH')
    before = block(client, 'B2')['water_liters']
    r = register_diag(client, 'DG-STOP', b['id'], HEALTHY, [('BAD2', 2.4)])
    assert r.status_code == 200, r.text
    run_job()
    data = fleet(client)
    fault = next(f for f in data['faults'] if f['block_id'] == b['id'])
    assert fault['stop_work'] and fault['mission_id'] == mid
    assert fault['stop_water_liters'] == 300
    with Session() as db:
        m = db.get(WashMission, mid)
        assert m.status == 'stopped'
    assert block(client, 'B2')['water_liters'] == before, '已扣水不自动返还'
    assert next(s for s in data['susp'] if s['block_id'] == b['id'])['active']


def test_05_repair_then_recheck_normal_recovers(client):
    data = fleet(client)
    fault = next(f for f in data['faults'] if f['block_id'] == block(client, 'B2')['id'])
    fid = fault['id']
    # 未确认维修不能直接复诊
    r = client.post(f'/string-diagnostics/faults/{fid}/recheck', json={'samples': [{'string_code': 'BAD2', 'current_a': 10.0}]}, headers=token(client, 'admin@t'))
    assert r.status_code == 409
    # 只有主管能确认维修
    r = client.post(f'/string-diagnostics/faults/{fid}/repair', json={'repair_note': '更换熔断器并复紧端子'}, headers=token(client, 'planner@t'))
    assert r.status_code == 403
    r = client.post(f'/string-diagnostics/faults/{fid}/repair', json={'repair_note': '更换熔断器并复紧端子'}, headers=token(client, 'admin@t'))
    assert r.status_code == 200, r.text
    # 复诊仍异常：不解禁
    r = client.post(f'/string-diagnostics/faults/{fid}/recheck', json={'samples': [{'string_code': 'BAD2', 'current_a': 3.0}]}, headers=token(client, 'admin@t'))
    assert r.status_code == 200, r.text
    run_job()
    data = fleet(client)
    f = next(x for x in data['faults'] if x['id'] == fid)
    assert f['state'] == 'awaiting_repair' and f['recheck_result'] == 'anomalous'
    assert next(s for s in data['susp'] if s['block_id'] == f['block_id'])['active']
    # 再次确认维修后正常读数解禁
    assert client.post(f'/string-diagnostics/faults/{fid}/repair', json={'repair_note': '重新接线后再试'}, headers=token(client, 'admin@t')).status_code == 200
    r = client.post(f'/string-diagnostics/faults/{fid}/recheck', json={'samples': [{'string_code': 'BAD2', 'current_a': 10.0}]}, headers=token(client, 'admin@t'))
    assert r.status_code == 200, r.text
    run_job()
    data = fleet(client)
    f = next(x for x in data['faults'] if x['id'] == fid)
    assert f['state'] == 'recovered' and f['recheck_result'] == 'normal'
    assert not next(s for s in data['susp'] if s['block_id'] == f['block_id'])['active']
    # 恢复后可再派工：停工任务保留水量但不再占用阵列
    with Session() as db:
        m = db.get(WashMission, f['mission_id'])
        assert m.status == 'stopped', '停工任务保持停工，不自动恢复'
        blk = db.get(ArrayBlock, f['block_id'])
        assert blk.cleaning_state == 'ready', '恢复后解除阵列清洗占用'
        old_s = db.get(SoilingSurvey, m.survey_id)
        assert old_s.state == 'stale', '停工任务旧巡测失效'
    h = token(client, 'planner@t')
    r = client.post('/surveys', json={'code': 'SV-B2-AFTER', 'block_id': f['block_id'], 'irradiance': 800, 'module_temp': 30, 'rain_days': 5, 'dust': 2, 'observed_kw': 90}, headers=h)
    assert r.status_code == 200, r.text
    sid = r.json()['id']
    with Session() as db:
        s = db.get(SoilingSurvey, sid); s.state = 'assessed'; s.clean_kw = 95; s.predicted_loss = 5; s.recommended_water = 100; db.commit()
    mid = client.post('/missions', json={'survey_id': sid, 'water_liters': 100}, headers=h).json()['id']
    # 需要许可：验证不是被诊断拦截（无许可会提示许可）
    r = client.post(f'/missions/{mid}/dispatch', headers=token(client, 'admin@t'))
    assert r.status_code == 409 and '许可' in r.json()['detail']


def test_06_retry_does_not_duplicate_fault(client):
    b = block(client, 'B2')
    register_diag(client, 'DG-REPLAY', b['id'], HEALTHY, [('BAD3', 1.5)])
    with Session() as db:
        # 故障仍在，先恢复掉以便新诊断建单
        f = db.scalar(select(StringFault).where(StringFault.block_id == b['id'], StringFault.state.in_(['awaiting_repair', 'repaired', 'rechecking'])))
        if f:
            f.state = 'recovered'
            s = db.scalar(select(WashSuspension).where(WashSuspension.block_id == b['id'])); s.active = False
        db.commit()
    n_before = len(fleet(client)['faults'])
    run_job()
    n_after = len(fleet(client)['faults'])
    assert n_after == n_before + 1
    # 重放同一任务（模拟中断后重试）不重复建单
    from src.worker import diagnose_one
    diag = next(x for x in fleet(client)['diag'] if x['code'] == 'DG-REPLAY')
    diagnose_one(diag['id'])
    assert len(fleet(client)['faults']) == n_after


def test_07_late_old_recheck_cannot_unlock(client):
    b = block(client, 'B1')
    data = fleet(client)
    fault = next(f for f in data['faults'] if f['block_id'] == b['id'] and f['state'] != 'recovered')
    fid = fault['id']
    # 第一轮复诊：先入队不消费（模拟中断/延迟）
    client.post(f'/string-diagnostics/faults/{fid}/repair', json={'repair_note': '第一轮更换旁路二极管'}, headers=token(client, 'admin@t'))
    r1 = client.post(f'/string-diagnostics/faults/{fid}/recheck', json={'samples': [{'string_code': 'BAD', 'current_a': 10.0}]}, headers=token(client, 'admin@t'))
    assert r1.status_code == 200, r1.text
    old_diag = r1.json()['id']
    # 尚未消费，重新确认维修并登记更新读数（复诊仍异常）
    assert client.post(f'/string-diagnostics/faults/{fid}/repair', json={'repair_note': '第二轮调整组串接线'}, headers=token(client, 'admin@t')).status_code == 200
    r2 = client.post(f'/string-diagnostics/faults/{fid}/recheck', json={'samples': [{'string_code': 'BAD', 'current_a': 4.0}]}, headers=token(client, 'admin@t'))
    assert r2.status_code == 200, r2.text
    new_diag = r2.json()['id']
    # 先处理旧诊断，再处理新诊断
    from src.worker import diagnose_one
    diagnose_one(old_diag)   # 旧诊断正常读数，但已不是最新，不得解禁
    data = fleet(client)
    f = next(x for x in data['faults'] if x['id'] == fid)
    assert f['state'] == 'rechecking', f['state']
    assert next(s for s in data['susp'] if s['block_id'] == b['id'])['active']
    diagnose_one(new_diag)   # 最新诊断仍异常 → 保持挂起
    data = fleet(client)
    f = next(x for x in data['faults'] if x['id'] == fid)
    assert f['state'] == 'awaiting_repair' and f['recheck_result'] == 'anomalous'
    assert next(s for s in data['susp'] if s['block_id'] == b['id'])['active']


def test_08_manual_report_rejects_dispatch_and_cannot_duplicate(client):
    b = block(client, 'B1')
    # B1 仍有未恢复故障
    r = client.post('/string-diagnostics/faults', json={'block_id': b['id'], 'strings': ['S9'], 'note': '现场巡检发现电流偏低'}, headers=token(client, 'planner@t'))
    assert r.status_code == 409
    # B2 恢复干净后人工登记
    b2 = block(client, 'B2')
    with Session() as db:
        for f in db.scalars(select(StringFault).where(StringFault.block_id == b2['id'])).all():
            f.state = 'recovered'
        s = db.scalar(select(WashSuspension).where(WashSuspension.block_id == b2['id']))
        if s: s.active = False
        db.commit()
    r = client.post('/string-diagnostics/faults', json={'block_id': b2['id'], 'strings': ['S9'], 'note': '现场巡检发现电流偏低'}, headers=token(client, 'planner@t'))
    assert r.status_code == 200, r.text
    assert r.json()['state'] == 'awaiting_repair' and r.json()['strings'] == ['S9']
    assert next(s for s in fleet(client)['susp'] if s['block_id'] == b2['id'])['active']


def test_09_retry_interrupted_diagnostic(client):
    b = block(client, 'B2')
    with Session() as db:
        for f in db.scalars(select(StringFault).where(StringFault.block_id == b['id'])).all():
            f.state = 'recovered'
        s = db.scalar(select(WashSuspension).where(WashSuspension.block_id == b['id']))
        if s: s.active = False
        db.commit()
    r = register_diag(client, 'DG-CRASH', b['id'], HEALTHY, [('BAD9', 1.0)])
    did = r.json()['id']
    # 模拟队列丢失：清空，不消费
    queue.jobs.clear()
    data = fleet(client)
    d = next(x for x in data['diag'] if x['id'] == did)
    assert d['state'] == 'pending'
    # 观察员不能重试
    assert client.post(f'/string-diagnostics/{did}/retry', headers=token(client, 'viewer@t')).status_code == 403
    r = client.post(f'/string-diagnostics/{did}/retry', headers=token(client, 'planner@t'))
    assert r.status_code == 200, r.text
    run_job()
    d = next(x for x in fleet(client)['diag'] if x['id'] == did)
    assert d['state'] == 'completed' and d['anomaly_strings'] == ['BAD9']
    # 完成后不能再次重试
    assert client.post(f'/string-diagnostics/{did}/retry', headers=token(client, 'planner@t')).status_code == 409


def test_10_duplicate_diagnostic_code_rejected(client):
    b = block(client, 'B2')
    r = register_diag(client, 'DG-DUP', b['id'], HEALTHY, [('X', 9.9)])
    assert r.status_code == 200
    r = register_diag(client, 'DG-DUP', b['id'], HEALTHY, [('X', 9.9)])
    assert r.status_code == 409
    queue.jobs.clear()


def test_11_validation(client):
    b = block(client, 'B2')
    h = token(client, 'planner@t')
    # 重复组串编号
    r = client.post('/string-diagnostics', json={'code': 'DG-V1', 'block_id': b['id'], 'samples': [{'string_code': 'A', 'current_a': 1, 'healthy': False}, {'string_code': 'A', 'current_a': 2, 'healthy': False}]}, headers=h)
    assert r.status_code == 422
    # 非正电流
    r = client.post('/string-diagnostics', json={'code': 'DG-V2', 'block_id': b['id'], 'samples': [{'string_code': 'A', 'current_a': 0, 'healthy': False}]}, headers=h)
    assert r.status_code == 422
    # 维修说明过短
    r = client.post('/string-diagnostics/faults', json={'block_id': b['id'], 'strings': ['A'], 'note': '短'}, headers=h)
    assert r.status_code == 422


def test_12_manual_diagnostic_cannot_retry_same_samples(client):
    b = block(client, 'B2')
    with Session() as db:
        for f in db.scalars(select(StringFault).where(StringFault.block_id == b['id'])).all():
            f.state = 'recovered'
        s = db.scalar(select(WashSuspension).where(WashSuspension.block_id == b['id']))
        if s: s.active = False
        db.commit()
    r = register_diag(client, 'DG-MANUAL2', b['id'], HEALTHY[:2], [('Y', 9.8)])
    did = r.json()['id']; queue.jobs.clear()
    # 直接落库 manual 结果，模拟 worker 已判定
    from src.worker import diagnose_one
    from src.diagnosis_service import apply_result
    from src.diagnosis_algorithm import evaluate
    with Session() as db:
        d = db.get(StringDiagnostic, did)
        res = evaluate([{'string_code': s.string_code, 'current_a': s.current_a, 'healthy': s.healthy} for s in d.samples])
        assert res['status'] == 'manual'
        apply_result(db, did, res); db.commit()
    assert client.post(f'/string-diagnostics/{did}/retry', headers=token(client, 'planner@t')).status_code == 409


def test_13_second_anomaly_on_same_array_does_not_duplicate_fault(client):
    b = block(client, 'B1')
    data = fleet(client)
    active = [f for f in data['faults'] if f['block_id'] == b['id'] and f['state'] != 'recovered']
    assert active, 'B1 应有未恢复故障'
    before = len(data['faults'])
    r = register_diag(client, 'DG-AGAIN', b['id'], HEALTHY, [('S11', 1.0)])
    assert r.status_code == 200, r.text
    run_job()
    data = fleet(client)
    assert len(data['faults']) == before, '同阵列不再新建故障单'
    d = next(x for x in data['diag'] if x['code'] == 'DG-AGAIN')
    assert d['state'] == 'completed' and d['anomaly_strings'] == ['S11']
