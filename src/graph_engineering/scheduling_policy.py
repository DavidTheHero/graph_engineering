"""Deterministic queue policies; reviewed Qwen proposal, corrected by the supervisor."""
import math


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
