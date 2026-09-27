import importlib,copy
import pytest
from fastapi.testclient import TestClient
from jev_helpers import make_bot
from config import BotConfig,config_to_dict
from bot import BinanceTrBot

@pytest.fixture
def webenv(tmp_path,monkeypatch):
    module=importlib.import_module('web.app')
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'})
    monkeypatch.setattr(module,'bot_instance',b)
    monkeypatch.setattr(module,'save_config',lambda cfg:str(tmp_path/'unused.yaml'))
    client=TestClient(module.app)
    yield b,c,f,p,client
    c.close()

@pytest.mark.parametrize('route,payload',[
 ('/api/config',{'budget_per_trade':1}),('/api/config/full',{'decision':{'model':'changed'}}),
 ('/api/config/switch_mode',{'mode':'live'}),('/api/config/reset_default',{'mode':'simulation'}),('/api/start',{'duration_minutes':1})])
def test_running_settings_cannot_mutate_recorded_policy(webenv,route,payload):
    b,c,f,p,client=webenv;before=config_to_dict(b.config)
    result=client.post(route,json=payload)
    assert result.status_code==409 and config_to_dict(b.config)==before

def test_jev_status_auth_and_safe_manual_actions(webenv):
    b,c,f,p,client=webenv;b.step()
    assert client.get('/api/decisions/status').json()['engine']=='jev'
    rows=client.get('/api/decisions/recent').json()['decisions'];assert len(rows)==2
    assert client.post('/api/force_buy',json={}).status_code==400
    assert client.post('/api/force_buy',json={'symbol':'SOL_TRY','budget':'bad'}).status_code==400
    b.config.auth.enabled=True
    assert client.get('/api/decisions/status').status_code==401
    assert client.post('/api/config/full',json={}).status_code==401

def test_invalid_jev_settings_are_rejected_before_apply(webenv):
    b,c,f,p,client=webenv;b.is_running=False
    result=client.post('/api/config/full',json={'decision':{'min_action_confidence':2}})
    assert result.status_code==400 and b.config.decision.min_action_confidence==.65
    assert b._decision_controller is c

def test_start_missing_key_explains_error_without_thread(webenv,monkeypatch):
    b,c,f,p,client=webenv;b.is_running=False;b._decision_provider=None
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    result=client.post('/api/start',json={})
    assert result.status_code==400 and 'missing_openrouter_api_key' in result.json()['message']
    assert b.thread is None and not b.is_running

def test_jev_dashboard_does_not_invoke_network_scan(webenv,monkeypatch):
    b,c,f,p,client=webenv;b.scanner.cached_top_pairs=[]
    monkeypatch.setattr(b.scanner,'scan_top_active_pairs',lambda **k:pytest.fail('dashboard must be in-memory'))
    result=client.get('/api/state')
    assert result.status_code==200 and result.json()['decision_engine']=='jev'


def test_decision_feed_distinguishes_model_choice_from_applied_fallback(webenv):
    b,c,f,p,client=webenv
    p.choices={'action':'BUY'};p.confidence=.2
    b.step()
    rows=client.get('/api/decisions/recent').json()['decisions']
    candidate=next(r for r in rows if r['stage']=='candidate')
    assert candidate['model_choice']=='BUY'
    assert candidate['confidence']==.2 and candidate['valid'] is True
    assert candidate['applied_action']=='WAIT'
    assert candidate['reasons']==['low_confidence:action']
    assert candidate['execution'] is None
    assert 'request_json' not in candidate and 'raw' not in candidate

def test_jev_constructor_never_syncs_real_wallet(tmp_path,monkeypatch):
    from core.live_trader import LiveTraderEngine
    monkeypatch.setattr(LiveTraderEngine,'sync_with_wallet',lambda *a:pytest.fail('private wallet access is not part of Jev paper setup'))
    cfg=BotConfig();cfg.decision.engine='jev';cfg.decision.database_path=str(tmp_path/'no-wallet.sqlite3')
    cfg.api.api_key='fake';cfg.api.secret_key='fake'
    b=BinanceTrBot(cfg);assert not b.live_trader.positions
    cfg.trading.mode='live'
    with pytest.raises(ValueError,match='live execution disabled'):BinanceTrBot(cfg)


def test_decision_feed_shows_allocation_fallback_and_retry(webenv):
    b,c,f,p,client=webenv
    p.choices={'action':'BUY'}
    calls=[]
    def hook(state,questions,response):
        if 'allocation' not in questions:return
        calls.append(1)
        response['answers']['allocation']['confidence']=.2
        if len(calls)==1:response['answers']['action']['choice']='INVALID'
    p.hook=hook;b.step()
    rows=client.get('/api/decisions/recent').json()['decisions']
    candidate=next(r for r in rows if r['stage']=='candidate')
    assert candidate['model_choice']=='BUY' and candidate['valid'] is True
    assert candidate['retry_count']==1
    assert candidate['allocation_confidence']==.2
    assert candidate['allocation_fallback']['budget_try']==500
    assert candidate['applied_action']=='BUY_PROPOSED'
    assert candidate['execution']=='filled'
