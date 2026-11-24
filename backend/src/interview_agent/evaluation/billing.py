"""New-stage policy; old policy files/authorizations remain immutable."""
from typing import Literal
from interview_agent.career.billing import CareerPolicy
from interview_agent.billing.ledger import BillingUnavailable
from psycopg import AsyncConnection

AUTHORIZATION_ID='ja-09-02-20261030-cny2'
CALIBRATION_AUTHORIZATION_ID='ja-09-05-20261030-cny2'

class EvaluationPolicy(CareerPolicy):
    profile:Literal['ja09-deepseek-flash']
    max_http_attempts:Literal[24]
    max_operation_attempts:Literal[3]

class CalibrationPolicy(CareerPolicy):
    profile:Literal['ja09-05-deepseek-flash']
    max_http_attempts:Literal[18]
    max_operation_attempts:Literal[3]

def policy_for_stage(path,identity):
    if identity==AUTHORIZATION_ID:return EvaluationPolicy.load(path),'ja-09-02'
    if identity==CALIBRATION_AUTHORIZATION_ID:return CalibrationPolicy.load(path),'ja-09-05'
    raise ValueError('evaluation_stage_authorization_required')

def calibration_scope(task_ids,adapters,repeats,split):
    if (set(task_ids)!={'G04','G09','G10'} or len(task_ids)!=3 or set(adapters)!={'system_live','fixed_prefetch_live'}
        or len(adapters)!=2 or repeats!=1 or split!='development'):
        raise ValueError('calibration_frozen_six_attempt_matrix_required')

async def check_authorization(owner,policy,identity):
    async with await AsyncConnection.connect(owner.conninfo()) as conn:
        row=await (await conn.execute('SELECT policy_hash,max_nano,state FROM app.model_authorizations WHERE id=%s',(identity,))).fetchone()
    if not row or row[0]!=policy.digest or not 0<row[1]<=2000000000:raise BillingUnavailable('evaluation_authorization_unavailable')
    if row[2]!='ACTIVE':raise BillingUnavailable('model_usage_unknown')
    return {'authorization_id':identity,'max_nano':row[1],'state':row[2]}
