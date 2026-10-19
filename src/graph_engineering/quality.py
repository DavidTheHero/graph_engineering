"""Explicitly opted-in parallel independent quality commands with real fan-in."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import copy_context
from threading import Event
import uuid

from .repository import run_check


def parallel_checks(flow,state,path):
    commands=[state['config']['test_command'],*state['config']['build_commands']]
    if len(commands)>4: raise ValueError('Parallel quality supports at most four explicitly configured independent commands')
    cancelled=Event();results={};children={};pending={}
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        for index,argv in enumerate(commands):
            child=f'{flow.run_id}-quality-{uuid.uuid4().hex[:8]}'
            children[index]=child
            flow.store.event(flow.run_id,'test_execution','child_started',{
                'child_task_id':child,'source':'test_execution','destination':'child:'+child,
                'agent_id':'regression-runner' if index==0 else 'build-runner','argv':argv,'status':'running'})
            future=pool.submit(copy_context().run,run_check,path,argv,state['config']['trusted_local'],
                               state['config']['command_timeout'],cancelled.is_set)
            pending[future]=index
        while pending:
            if flow.cancelled(): cancelled.set()
            done,_=wait(pending,timeout=.1,return_when=FIRST_COMPLETED)
            for future in done:
                index=pending.pop(future);child=children[index]
                try: result=future.result()
                except BaseException:
                    cancelled.set()
                    flow.store.event(flow.run_id,'test_execution','child_failed',{
                        'child_task_id':child,'source':'child:'+child,'destination':'test_execution','status':'failed'})
                    raise
                results[index]=result
                artifact=flow.store.artifact(flow.run_id,child+'.json',result)
                flow.store.event(flow.run_id,'test_execution','child_completed',{
                    'child_task_id':child,'source':'child:'+child,'destination':'test_execution',
                    'status':'passed' if result['passed'] else 'failed','artifact':artifact})
    flow.store.event(flow.run_id,'test_execution','quality_join',{
        'source':'test_execution','destination':'test_execution','children':list(children.values()),
        'all_dependencies_completed':len(results)==len(commands),'passed':all(r['passed'] for r in results.values())})
    return results[0],[results[i] for i in range(1,len(commands))]
