import asyncio
from types import SimpleNamespace

from backend.tracing import trace_start, trace_end, trace_span
from scripts.verify_trace import inspect_span


def test_basic_child_trace_uses_server_generated_hierarchy(monkeypatch):
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def create_run(self, **kwargs):
            # The real /runs API rejects trace_id without dotted_order.
            if kwargs.get('trace_id') and not kwargs.get('dotted_order'):
                raise ValueError('dotted_order is required when trace_id is provided')
            calls.append(('create', kwargs))
        def update_run(self, id, **kwargs):
            calls.append(('end', {'id': id, **kwargs}))
    monkeypatch.setenv('LANGSMITH_API_KEY', 'local-test-key')
    monkeypatch.setattr('langsmith.Client', Client)
    async def run():
        root = await trace_start('job', {'operation': 'generation'})
        stats = {'trace_id': root}
        await trace_span(stats, 'review', {'role': 'reviewer', 'status': 'completed'})
        assert not stats.get('trace_warnings')
        assert await trace_end(root, {'status': 'completed'})
    asyncio.run(run())
    creates = [data for kind, data in calls if kind == 'create']
    assert len(creates) == 2
    assert creates[1]['parent_run_id'] == creates[0]['id']
    assert all(data['inputs'] == {} for data in creates)


def test_readback_rejects_unexpected_content_and_unclosed_spans():
    span = SimpleNamespace(id='one', name='LoreForge/job', parent_run_id=None,
                           inputs={'prompt': 'private'}, extra={'metadata': {'request': 'private'}},
                           outputs={'status': 'completed'}, end_time=None)
    inspected = inspect_span(span)
    assert len(inspected['errors']) == 3
    assert 'private' not in str(inspected)
