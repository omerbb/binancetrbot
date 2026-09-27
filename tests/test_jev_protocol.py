import copy,json
import pytest,requests
from config import BotConfig
from decision.contracts import validate_config,validate_questions,validate_response,clean,dumps
from decision.questions import symbol_questions,portfolio_questions,prebuy_questions
from decision.fixtures import FixtureDecisionProvider
from decision.openrouter import OpenRouterJevProvider,ProviderError

class Reply:
    def __init__(self,status=200,payload=None,headers=None):
        self.status_code=status;self.payload=payload;self.headers=headers or {};self.content=json.dumps(payload).encode()
    def json(self):
        if isinstance(self.payload,str): raise ValueError('malformed json')
        return self.payload

class Transport:
    def __init__(self,replies): self.replies=list(replies);self.calls=[]
    def post(self,url,**kwargs):
        self.calls.append((url,kwargs));r=self.replies.pop(0)
        if isinstance(r,Exception): raise r
        return r
    def close(self): pass

def sample():
    qs=symbol_questions(False,{'SMALL':.25,'FULL':1})
    ans=FixtureDecisionProvider().evaluate({'market':{'features_ready':True}},qs)
    return qs,ans

def test_documented_primitives_complete_transport(monkeypatch):
    cfg=BotConfig().decision;qs,res=sample()
    qs.update(prebuy_questions());qs['binary']={'type':'noul','instructions':'Is state.ready true?'}
    res=FixtureDecisionProvider().evaluate({'market':{'features_ready':True}},qs)
    validate_questions(qs);validate_response(res,qs)
    key='sk-or-v1-protocol-test-not-a-real-key';monkeypatch.setenv('OPENROUTER_API_KEY',key)
    transport=Transport([Reply(payload=res)]);attempts=[]
    provider=OpenRouterJevProvider(cfg,session=transport)
    got=provider.evaluate({'ready':True,'api_key':'SHOULD_NOT_BE_SENT'},qs,on_attempt=attempts.append)
    validate_response(got,qs)
    url,kw=transport.calls[0]
    assert url=='https://openrouter.ai/api/v1/systemone'
    assert kw['headers']['Authorization']=='Bearer '+key
    assert kw['allow_redirects'] is False and kw['timeout']==(4.0,4.0)
    payload=json.loads(kw['data']);assert set(payload)=={'model','state','questions'}
    assert payload['state']['api_key']=='[REDACTED]'
    assert isinstance(payload['questions']['prebuy_authorization'],dict)
    assert key not in dumps(attempts) and 'SHOULD_NOT_BE_SENT' not in kw['data'].decode()

@pytest.mark.parametrize('mutate',[
 lambda r:r.pop('model'), lambda r:r.pop('usage'),lambda r:r['answers'].pop('action'),
 lambda r:r['answers'].update(extra={}),lambda r:r['answers']['action'].update(choice='STEAL'),
 lambda r:r['answers']['action'].update(confidence=float('nan')),
 lambda r:r['answers']['action'].update(confidence=True),
 lambda r:r['answers']['action']['probabilities'].update(BUY=.2),
 lambda r:r['answers']['action']['probabilities'].update(WAIT=2),
 lambda r:r['answers']['action'].update(choice='WAIT'),
 lambda r:r['answers']['setup_quality'].update(score=1.05),
 lambda r:r['answers']['setup_quality'].pop('legend'),
 lambda r:r['usage'].update(cost=-1),lambda r:r['usage'].update(input_tokens='1'),
])
def test_reject_invalid_structured_responses(mutate):
    qs,r=sample();mutate(r)
    with pytest.raises((ValueError,TypeError)):validate_response(r,qs)


def test_live_provider_rounded_distribution_is_valid_without_rewriting_evidence():
    qs, response = sample()
    # Captured provider values: the unrounded probabilities sum to one, but
    # their serialized hundredths sum to 0.99 and the score rounds to 0.36.
    response['answers']['setup_quality'].update(
        probabilities={'0': .68, '1': .28, '2': .03, '3': 0}, score=.36)
    original = copy.deepcopy(response)
    assert validate_response(response, qs) == original
    assert response == original


@pytest.mark.parametrize('probabilities,score', [
    ({'0': .60, '1': .28, '2': .03, '3': 0}, .36),
    ({'0': .68, '1': .28, '2': .03, '3': 0}, .50),
    ({'0': .99, '1': .02, '2': .02, '3': 0}, .03),
])
def test_rounding_does_not_admit_inconsistent_mass_or_score(probabilities, score):
    qs, response = sample()
    response['answers']['setup_quality'].update(probabilities=probabilities, score=score)
    with pytest.raises(ValueError):
        validate_response(response, qs)

@pytest.mark.parametrize('status',[408,429,500,502,503,504,529])
def test_retry_only_inference_transient_errors(monkeypatch,status):
    monkeypatch.setenv('OPENROUTER_API_KEY','test-not-secret');qs,r=sample();attempts=[];sleeps=[]
    session=Transport([Reply(status),Reply(payload=r)])
    p=OpenRouterJevProvider(BotConfig().decision,session=session,sleep=sleeps.append)
    assert p.evaluate({},qs,on_attempt=attempts.append)==r
    assert len(session.calls)==2 and sleeps==[.25] and attempts[0]['retryable'] is True

@pytest.mark.parametrize('status',[301,302,401,403,404,422])
def test_no_retry_or_redirect_nontransient(monkeypatch,status):
    monkeypatch.setenv('OPENROUTER_API_KEY','test-not-secret');qs,_=sample();session=Transport([Reply(status)])
    with pytest.raises(ProviderError,match=f'http_{status}'):
        OpenRouterJevProvider(BotConfig().decision,session=session).evaluate({},qs)
    assert len(session.calls)==1

def test_long_retry_after_is_not_ignored(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY','test-not-secret');qs,_=sample()
    p=OpenRouterJevProvider(BotConfig().decision,session=Transport([Reply(429,headers={'Retry-After':'60'})]),sleep=lambda _:pytest.fail('must not wait60s'))
    with pytest.raises(ProviderError,match='retry_after_exceeds'):p.evaluate({},qs)

def test_timeout_and_missing_key_fail_safely(monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False);qs,_=sample();transport=Transport([])
    p=OpenRouterJevProvider(BotConfig().decision,session=transport,sleep=lambda _:None)
    with pytest.raises(ProviderError,match='missing_openrouter'):p.evaluate({},qs)
    assert not transport.calls
    monkeypatch.setenv('OPENROUTER_API_KEY','test-not-secret');transport.replies=[requests.Timeout(),requests.ConnectionError()]
    with pytest.raises(ProviderError,match='network_or_timeout'):p.evaluate({},qs)

def test_local_yaml_key_can_be_used_without_environment_variable(monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    cfg=BotConfig().decision;cfg.api_key='sk-or-v1-local-test-not-a-real-key';qs,response=sample()
    transport=Transport([Reply(payload=response)])
    OpenRouterJevProvider(cfg,session=transport).evaluate({},qs)
    assert transport.calls[0][1]['headers']['Authorization']=='Bearer '+cfg.api_key

def test_request_limit_no_silent_feature_truncation(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY','test-not-secret');cfg=BotConfig().decision;cfg.max_request_bytes=1000
    p=OpenRouterJevProvider(cfg,session=Transport([]))
    with pytest.raises(ProviderError,match='request_too_large'):p.evaluate({'huge':'x'*5000},portfolio_questions())

@pytest.mark.parametrize('field,value', [('min_action_confidence',-1),('slippage_bps',10000),('max_attempts',0),('max_candidates_per_step',True),('endpoint','https://attacker.invalid'),('outcome_horizons_seconds',[60,60]),('allocation_fractions',{'FULL':1.5}),('api_key_env','sk-or-my-secret')])
def test_invalid_configuration_is_rejected(field,value):
    cfg=BotConfig();setattr(cfg.decision,field,value)
    with pytest.raises(ValueError):validate_config(cfg)

def test_reserved_secret_key_cannot_silently_replace_a_question():
    with pytest.raises(ValueError):validate_questions({'authorization':{'type':'choice','instructions':'authorize','criteria':{'Y':'yes'}}})

def test_selected_action_survives_json_sanitization_for_every_stage():
    for qs in (portfolio_questions(),prebuy_questions(),symbol_questions(True,{'FULL':1}),symbol_questions(False,{'FULL':1})):
        validate_questions(qs)
        response=FixtureDecisionProvider().evaluate({'market':{'features_ready':True},'position':None},qs)
        # The held-stage fixture must receive a held position; validate a proper HOLD choice instead.
        if 'action' in qs and 'HOLD' in qs['action']['criteria']:
            response=FixtureDecisionProvider().evaluate({'market':{'features_ready':True},'position':{'unrealized_pnl_pct':0}},qs)
        assert validate_response(json.loads(dumps(response)),json.loads(dumps(qs)))['answers']==response['answers']
