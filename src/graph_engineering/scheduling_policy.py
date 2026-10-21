"""Deterministic queue policies; reviewed Qwen proposal, corrected by the supervisor."""
import math


def execution_limits(ticket):
    from .workflow import REGISTRY
    state=ticket['state'];config=state['config'];node=ticket['node'];spec=REGISTRY.get(node)
    key=f"{node}:{state.get('repairs',0)}"
    if config.get('run_mode')=='intake':
        key+=f":g{state['intake'].get('generation',0)}:s{state.get('search_round',0)}:v{state.get('validation_round',0)}:r{state.get('revision',0)}"
    result={'step':node,'step_used':state.get('attempts',{}).get(key,0),
        'step_limit':config.get('max_node_attempts',2) if config.get('run_mode')=='accepted_development' else min(config.get('max_node_attempts',2),spec.max_attempts if spec else 2),
        'retries_used':ticket['retries'],'retry_limit':ticket['max_retries'],
        'repairs_used':state.get('repairs',0),'repair_limit':config.get('max_repairs',2),
        'exhausted':state.get('error')=='Attempt budget exhausted'}
    label='Step attempt limit reached at' if result['exhausted'] else 'Step attempts at'
    explanation=f"{label} {node.replace('_',' ')} ({result['step_used']}/{result['step_limit']}). Ticket retries used: {result['retries_used']}/{result['retry_limit']}."
    review=state.get('regression_review',{})
    reason=state.get('regression_feedback') or next((f['message'] for f in review.get('findings',[]) if f.get('severity') in ('critical','required') and not review.get('approved')),'')
    if not reason:
        previous=state.get('last_error',{});reason=previous.get('message','') if isinstance(previous,dict) else str(previous or '')
    result['reason']=reason[:1000]
    result['explanation']=explanation+(' Last feedback: '+reason[:500] if reason else '')
    return result


def effective_rank(priority, enqueued_at, current, aging_seconds=300):
    if not math.isfinite(aging_seconds) or aging_seconds <= 0:
        raise ValueError('aging_seconds must be positive and finite')
    base = {'P0':0, 'P1':1, 'P2':2, 'P3':3}.get(priority, 2)
    return max(0, base - int(max(0, current - enqueued_at) // aging_seconds))


def retry_delay(attempt, base=2, cap=300):
    if type(attempt) is not int or attempt < 1 or not math.isfinite(base) or not math.isfinite(cap) or base <= 0 or cap < base:
        raise ValueError('Invalid bounded retry parameters')
    # Compare logarithms before exponentiation so a huge attempt never overflows.
    if attempt - 1 >= math.log2(cap / base): return float(cap)
    return min(float(cap), base * 2 ** (attempt - 1))


def board_status(node, status, paused=False):
    if status=='cancelled':return 'Cancelled'
    if status=='ready_for_delivery':return 'Ready for Merge'
    if status in ('completed','no_findings','duplicate'): return 'Done'
    if status in ('escalated','failed'): return 'Failed'
    if paused or status in ('blocked','cancelled'): return 'Blocked'
    if status in ('needs_information','duplicate_review','enhancement_rejected'):return 'Blocked'
    if status=='ready_for_review':return 'Code Review'
    if status in ('ready_for_development','development_queued'):return 'To Do'
    if status=='ready' and node=='manual_intake':return 'To Do'
    if status in ('waiting_approval','scanned'): return 'Ready for Merge' if status == 'waiting_approval' else 'Backlog'
    if status == 'ready' and node == 'repository_discovery': return 'To Do'
    if node in ('test_execution','post_merge_verification'): return 'Testing'
    if node == 'independent_review': return 'Code Review'
    if node in ('pull_request_creation','ci_policy_verification','approval_gate','merge'): return 'Ready for Merge'
    return 'In Progress'
