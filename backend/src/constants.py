from enum import StrEnum
class Role(StrEnum):
    ADMIN='admin'
    PLANNER='planner'
    VIEWER='viewer'
class SurveyState(StrEnum):
    RECORDED='recorded'
    EVALUATING='evaluating'
    ASSESSED='assessed'
    SCHEDULED='scheduled'
    RETESTED='retested'
    CLOSED='closed'
    BLOCKED='blocked'
    STALE='stale'
class MissionState(StrEnum):
    DRAFT='draft'
    DISPATCHED='dispatched'
    STOPPED='stopped'
    RETESTED='retested'
    ACCEPTED='accepted'
    SUPERSEDED='superseded'
class DiagnosticState(StrEnum):
    PENDING='pending'
    EVALUATING='evaluating'
    COMPLETED='completed'
    MANUAL='manual'
    BLOCKED='blocked'
class FaultState(StrEnum):
    AWAITING_REPAIR='awaiting_repair'
    REPAIRED='repaired'
    RECHECKING='rechecking'
    RECOVERED='recovered'
AWAITING_DIAGNOSIS_STATES=('pending','evaluating','manual','blocked')
