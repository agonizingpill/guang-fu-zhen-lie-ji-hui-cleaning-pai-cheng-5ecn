export const ROLES={admin:'运维主管',planner:'清洗计划员',viewer:'电站观察员'};
export const SURVEY_STATES={recorded:'待评估',evaluating:'评估中',assessed:'已评估',scheduled:'清洗中',retested:'已复测',closed:'已闭环',blocked:'需重试',stale:'清洗前旧读数'};
export const MISSION_STATES={draft:'待派工',dispatched:'执行中',stopped:'现场停工待修',retested:'待验收',accepted:'已验收',superseded:'旧计划已失效'};
export const DIAGNOSTIC_STATES={pending:'待诊断',evaluating:'诊断中',completed:'诊断完成',manual:'样本不足转人工',blocked:'诊断中断'};
export const FAULT_STATES={awaiting_repair:'故障待修',repaired:'待复诊',rechecking:'复诊判定中',recovered:'已恢复'};
