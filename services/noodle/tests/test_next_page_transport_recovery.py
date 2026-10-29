"""Transient page failures retry locally without replaying the whole book."""

from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError

from noodle_agent.next import page_evidence
from noodle_agent.next.page_evidence import PageEvidence
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.resilience import transient_provider_failure


@pytest.mark.parametrize('failures',[1,3])
def test_page_retry_is_bounded_and_preserves_single_successful_receipt(monkeypatch,failures):
    calls,events,delays=[],[],[]
    def prepare(*args,**kwargs):
        calls.append(kwargs['selected_pages'])
        if len(calls)<=failures:
            raise TimeoutError('Temporary page service failure')
        return [{'page':1,'evidence_id':'same-successful-proof'}]
    monkeypatch.setattr(page_evidence,'page_contexts',prepare)
    monkeypatch.setattr(page_evidence.time,'sleep',delays.append)
    g=SimpleNamespace(store=None,journal=None,parsing=SimpleNamespace(vision=None),
        lease=SimpleNamespace(input={'revision_id':'source'}),progress=lambda stage,value:events.append((stage,value)))
    with PageEvidence(g,[]) as work:
        work.units[1]=[]
        if failures==3:
            with pytest.raises(TimeoutError):
                work._prepare(1)
        else:
            assert work._prepare(1)==[{'page':1,'evidence_id':'same-successful-proof'}]
    assert len(calls)==min(failures+1,3)
    assert delays==([2] if failures==1 else [2,4])
    assert sum(stage=='visual-page-ready' for stage,_ in events)==int(failures==1)


def test_invalid_page_evidence_is_not_retried_or_replaced(monkeypatch):
    calls=[]
    def prepare(*args,**kwargs):
        calls.append(1)
        raise QualityFailure('Source image hash differs')
    monkeypatch.setattr(page_evidence,'page_contexts',prepare)
    g=SimpleNamespace(store=None,journal=None,parsing=SimpleNamespace(vision=None),lease=SimpleNamespace(input={'revision_id':'source'}),progress=lambda *args:None)
    with PageEvidence(g,[]) as work:
        work.units[1]=[]
        with pytest.raises(QualityFailure,match='hash'):
            work._prepare(1)
    assert len(calls)==1


def test_actual_rate_limit_type_is_transient_but_authorization_failure_is_not():
    request=httpx.Request('POST','https://example.invalid')
    limited=RateLimitError('ServerOverloaded',response=httpx.Response(429,request=request),body=None)
    assert transient_provider_failure(limited)
    forbidden=httpx.HTTPStatusError('Forbidden',request=request,response=httpx.Response(403,request=request))
    assert not transient_provider_failure(forbidden)
