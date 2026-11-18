"""组串电流异常诊断。

只用操作员确认健康的组串电流学习正常分布；健康样本再独立留出一部分，
仅用于校准异常阈值，不参与中位数/离散度估计。确认健康样本不足或没有
待判定组串时返回 manual，由人工复核，不自动挂起阵列。
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics

MIN_TRAIN = 4
MIN_HOLDOUT = 2
MIN_HEALTHY = MIN_TRAIN + MIN_HOLDOUT
HOLDOUT_RATIO = 0.30
SPLIT_SEED = 42
Z_FLOOR = 3.0
CALIBRATION_MARGIN = 0.20
SIGMA_FLOOR_RATIO = 0.005
METHOD = "robust-z: 健康样本 median/MAD；独立留出校准阈值"


def _sha(samples):
    payload = [{"string_code": s["string_code"], "current_a": round(float(s["current_a"]), 4), "healthy": bool(s["healthy"])} for s in samples]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _manual(reason, samples, extra=None):
    evidence = {
        "status": "manual",
        "reason": reason,
        "method": METHOD,
        "params": {"min_healthy": MIN_HEALTHY, "min_train": MIN_TRAIN, "min_holdout": MIN_HOLDOUT, "holdout_ratio": HOLDOUT_RATIO, "split_seed": SPLIT_SEED},
        "sample_count": len(samples),
        "healthy_count": sum(1 for s in samples if s["healthy"]),
        "candidate_count": sum(1 for s in samples if not s["healthy"]),
        "samples_sha256": _sha(samples),
    }
    if extra:
        evidence.update(extra)
    return {"status": "manual", "anomalies": [], "scores": [], "threshold": None, "evidence": evidence}


def evaluate(samples):
    """initial 诊断：学习正常分布并判定待判定组串。"""
    samples = [{"string_code": str(s["string_code"]), "current_a": float(s["current_a"]), "healthy": bool(s["healthy"])} for s in samples]
    healthy = sorted((s for s in samples if s["healthy"]), key=lambda s: s["string_code"])
    candidates = sorted((s for s in samples if not s["healthy"]), key=lambda s: s["string_code"])
    if len(healthy) < MIN_HEALTHY:
        return _manual(f"确认健康样本仅{len(healthy)}个，少于{MIN_HEALTHY}个，转人工补充健康样本", samples)
    if not candidates:
        return _manual("全部样本均被确认为健康，没有待判定组串，转人工确认", samples)

    # 独立留出：固定种子打乱健康样本，留出样本不参与正常分布估计。
    order = list(range(len(healthy)))
    random.Random(SPLIT_SEED).shuffle(order)
    holdout_n = max(MIN_HOLDOUT, round(len(healthy) * HOLDOUT_RATIO))
    holdout_n = min(holdout_n, len(healthy) - MIN_TRAIN)
    holdout_idx = set(order[:holdout_n])
    train = [healthy[i] for i in range(len(healthy)) if i not in holdout_idx]
    holdout = [healthy[i] for i in range(len(healthy)) if i in holdout_idx]

    currents = [s["current_a"] for s in train]
    median = statistics.median(currents)
    mad = statistics.median([abs(x - median) for x in currents])
    sigma = 1.4826 * mad
    if sigma <= 1e-9:
        sigma = statistics.pstdev(currents) if len(currents) > 1 else 0.0
    sigma = max(sigma, abs(median) * SIGMA_FLOOR_RATIO, 0.01)

    def score(value):
        return abs(value - median) / sigma

    holdout_scores = [{"string_code": s["string_code"], "current_a": s["current_a"], "score": round(score(s["current_a"]), 4)} for s in holdout]
    threshold = max(Z_FLOOR, max(h["score"] for h in holdout_scores) * (1 + CALIBRATION_MARGIN))
    threshold = round(threshold, 4)

    candidate_scores = []
    anomalies = []
    for s in candidates:
        raw = score(s["current_a"])
        anomalous = raw > threshold
        candidate_scores.append({"string_code": s["string_code"], "current_a": s["current_a"], "score": round(raw, 4), "anomalous": anomalous})
        if anomalous:
            anomalies.append(s["string_code"])

    evidence = {
        "status": "completed",
        "method": METHOD,
        "params": {"min_healthy": MIN_HEALTHY, "min_train": MIN_TRAIN, "min_holdout": MIN_HOLDOUT, "holdout_ratio": HOLDOUT_RATIO, "split_seed": SPLIT_SEED, "z_floor": Z_FLOOR, "calibration_margin": CALIBRATION_MARGIN, "sigma_floor_ratio": SIGMA_FLOOR_RATIO},
        "sample_count": len(samples),
        "healthy_count": len(healthy),
        "train_count": len(train),
        "holdout_count": len(holdout),
        "median_a": round(median, 4),
        "mad_a": round(mad, 4),
        "sigma_a": round(sigma, 4),
        "threshold": threshold,
        "threshold_basis": "独立留出健康样本得分上界 × (1+margin)，且不低于z下限",
        "train_samples": [{"string_code": s["string_code"], "current_a": s["current_a"]} for s in train],
        "holdout_samples": [{"string_code": s["string_code"], "current_a": s["current_a"]} for s in holdout],
        "holdout_scores": holdout_scores,
        "candidate_scores": candidate_scores,
        "anomaly_strings": anomalies,
        "samples_sha256": _sha(samples),
    }
    return {"status": "completed", "anomalies": anomalies, "scores": candidate_scores, "threshold": threshold, "evidence": evidence}


def evaluate_recheck(samples, baseline):
    """复诊：沿用初诊保存的正常分布与阈值判定维修后新读数。

    baseline 为初诊 evidence（含 median_a/sigma_a/threshold）。缺失时返回 None，
    调用方退回完整 evaluate（要求重新提供确认健康样本）。
    """
    try:
        median = float(baseline["median_a"])
        sigma = float(baseline["sigma_a"])
        threshold = float(baseline["threshold"])
        if sigma <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    samples = [{"string_code": str(s["string_code"]), "current_a": float(s["current_a"]), "healthy": bool(s.get("healthy", False))} for s in samples]
    scores = []
    anomalies = []
    for s in samples:
        raw = abs(s["current_a"] - median) / sigma
        anomalous = raw > threshold
        scores.append({"string_code": s["string_code"], "current_a": s["current_a"], "score": round(raw, 4), "anomalous": anomalous})
        if anomalous:
            anomalies.append(s["string_code"])
    evidence = {
        "status": "completed",
        "method": "recheck: 沿用初诊正常分布/阈值",
        "baseline": {"median_a": median, "sigma_a": sigma, "threshold": threshold, "diagnostic_id": baseline.get("diagnostic_id")},
        "sample_count": len(samples),
        "recheck_scores": scores,
        "anomaly_strings": anomalies,
        "samples_sha256": _sha(samples),
    }
    return {"status": "completed", "anomalies": anomalies, "scores": scores, "threshold": threshold, "evidence": evidence}
