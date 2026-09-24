import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient
from work_mcp.server import build_app

BASE = 'https://stewytailscale.tail93bb57.ts.net:8443'
CALLBACK = 'https://chatgpt.com/connector/oauth/test-connection'

@pytest.fixture
def setup(tmp_path):
    calls = []
    def bridge(req):
        if req.url.path == '/':
            if req.headers.get('authorization') != 'Basic ' + base64.b64encode(b'andy:password').decode():
                return httpx.Response(401)
            return httpx.Response(200,text='dashboard')
        assert req.headers['x-api-key'] == 'secret-test-only'
        calls.append(req)
        if req.url.path == '/api/summary':
            return httpx.Response(200,json={'date':'2026-09-22','timezone':'America/Toronto','calories':2550})
        if req.method == 'POST':
            return httpx.Response(200,json={'id':1,**json.loads(req.content)})
        return httpx.Response(200,json=[])
    config = {'public_url':BASE,'api_key':'secret-test-only','state_path':str(tmp_path/'auth.db')}
    with TestClient(build_app(config,httpx.MockTransport(bridge)),base_url=BASE,follow_redirects=False) as client:
        yield client,calls,config,bridge


def register(client,redirect=CALLBACK):
    return client.post('/register',json={'redirect_uris':[redirect],'token_endpoint_auth_method':'none',
        'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'scope':'meals'})


def login(client):
    r=register(client)
    assert r.status_code == 201,r.text
    cid=r.json()['client_id']
    verifier='v'*64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    r=client.get('/authorize',params={'client_id':cid,'redirect_uri':CALLBACK,'response_type':'code',
        'code_challenge':challenge,'code_challenge_method':'S256','resource':BASE+'/mcp','scope':'meals','state':'test-state'})
    assert r.status_code == 302,r.text
    ticket=parse_qs(urlsplit(r.headers['location']).query)['ticket'][0]
    r=client.get(r.headers['location'])
    assert r.status_code == 200,r.text
    assert r.headers['referrer-policy'] == 'strict-origin'
    assert "form-action 'self' https://chatgpt.com;" in r.headers['content-security-policy']
    csrf=client.cookies.get('__Host-calorie-csrf')
    data={'ticket':ticket,'csrf':csrf,'username':'andy','password':'password'}
    assert client.post('/approve',data=data,headers={'Origin':'https://evil.example'}).status_code == 403
    bad={**data,'password':'wrong'}
    assert client.post('/approve',data=bad,headers={'Origin':BASE}).status_code == 401
    r=client.post('/approve',data=data,headers={'Origin':BASE})
    assert r.status_code == 303,r.text
    query=parse_qs(urlsplit(r.headers['location']).query)
    assert query['state'] == ['test-state']
    return {'client_id':cid,'code':query['code'][0],'code_verifier':verifier,
        'redirect_uri':CALLBACK,'resource':BASE+'/mcp','grant_type':'authorization_code'}


def token(client):
    form=login(client)
    bad=client.post('/token',data={**form,'code_verifier':'incorrect'})
    assert bad.status_code == 400,bad.text
    r=client.post('/token',data=form)
    assert r.status_code == 200,r.text
    assert client.post('/token',data=form).status_code == 400
    return r.json(),form['client_id']


def rpc(client,access,method,params=None):
    return client.post('/mcp',headers={'Authorization':'Bearer '+access,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':1,'method':method,**({'params':params} if params is not None else {})})


def test_discovery_and_auth(setup):
    c,calls,_,_=setup
    r=c.post('/mcp',json={})
    assert r.status_code == 401
    assert 'oauth-protected-resource/mcp' in r.headers['www-authenticate']
    assert c.get('/.well-known/oauth-protected-resource/mcp').json()['resource']==BASE+'/mcp'
    assert c.get('/.well-known/oauth-authorization-server').json()['token_endpoint_auth_methods_supported']==['none']
    assert register(c,'https://evil.example/callback').status_code == 400
    assert not calls


def test_tools_and_idempotency(setup):
    c,calls,_,_=setup
    tok,_=token(c)
    access=tok['access_token']
    r=rpc(c,access,'initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
    assert r.status_code == 200,r.text
    tools=rpc(c,access,'tools/list').json()['result']['tools']
    assert {t['name'] for t in tools}=={'logMeal','getMeals','getDailySummary','getMealSyncStatus','retryMealSync'}
    r=rpc(c,access,'tools/call',{'name':'getDailySummary','arguments':{}})
    assert r.status_code == 200,r.text
    assert '2550' in r.text
    assert str(calls[-1].url)=='http://127.0.0.1:8021/api/summary'
    args={'request_id':'meal-test-request-1','name':'Lunch','calories':500}
    first=rpc(c,access,'tools/call',{'name':'logMeal','arguments':args}).json()
    assert not first['result'].get('isError'),first
    second=rpc(c,access,'tools/call',{'name':'logMeal','arguments':args}).json()
    assert first['result']==second['result']
    assert len([r for r in calls if r.method=='POST'])==1
    bad=rpc(c,access,'tools/call',{'name':'logMeal','arguments':{**args,'calories':900}}).json()
    assert bad['result']['isError']
    invalid=rpc(c,access,'tools/call',{'name':'logMeal','arguments':{**args,'request_id':'different-request-2','calories':-1}}).json()
    assert invalid['result']['isError']


def test_refresh_revoke_and_persistence(setup):
    c,_,config,bridge=setup
    tok,cid=token(c)
    form={'grant_type':'refresh_token','client_id':cid,'refresh_token':tok['refresh_token'],'resource':BASE+'/mcp'}
    r=c.post('/token',data=form)
    assert r.status_code==200,r.text
    assert c.post('/token',data=form).status_code==400
    new=r.json()
    with TestClient(build_app(config,httpx.MockTransport(bridge)),base_url=BASE) as restarted:
        assert rpc(restarted,new['access_token'],'tools/list').status_code==200
    r=c.post('/revoke',data={'client_id':cid,'token':new['refresh_token'],'token_type_hint':'refresh_token'})
    assert r.status_code==200,r.text
    assert rpc(c,new['access_token'],'tools/list').status_code==401
    assert rpc(c,tok['access_token'],'tools/list').status_code==401


def test_resource_and_csrf_rejected(setup):
    c,_,_,_=setup
    form=login(c)
    assert c.post('/token',data={**form,'resource':'https://evil.example/mcp'}).status_code==400
    r=c.post('/token',data=form)
    assert r.status_code==200
    assert rpc(c,'not-a-token','tools/list').status_code==401
    assert c.post('/approve',data={'ticket':'fake','csrf':'fake'},headers={'Origin':BASE}).status_code==403


def test_uncertain_write_is_never_retried(tmp_path):
    posts=[]
    def failing_bridge(req):
        if req.url.path=='/': return httpx.Response(200,text='dashboard')
        posts.append(req)
        raise httpx.ReadTimeout('response lost')
    config={'public_url':BASE,'api_key':'test-key','state_path':str(tmp_path/'state.db')}
    def transport(req):
        if req.url.path=='/':
            good='Basic '+base64.b64encode(b'andy:password').decode()
            return httpx.Response(200 if req.headers.get('authorization')==good else 401)
        return failing_bridge(req)
    with TestClient(build_app(config,httpx.MockTransport(transport)),base_url=BASE,follow_redirects=False) as c:
        tok,_=token(c)
        args={'request_id':'uncertain-meal-request','name':'Lunch','calories':500}
        for _ in range(2):
            r=rpc(c,tok['access_token'],'tools/call',{'name':'logMeal','arguments':args})
            assert r.json()['result']['isError']
        assert len(posts)==1


def test_real_bridge_database_and_background_sync(tmp_path,monkeypatch):
    from app import main
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine=create_engine('sqlite:///'+str(tmp_path/'meals.db'),connect_args={'check_same_thread':False})
    main.Base.metadata.create_all(engine)
    monkeypatch.setattr(main,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
    monkeypatch.setattr(main,'DASHBOARD_USERNAME','andy')
    monkeypatch.setattr(main,'DASHBOARD_PASSWORD','password')
    monkeypatch.setattr(main,'APP_API_KEY','test-key')
    monkeypatch.setattr(main,'fatsecret_connected',lambda db:True)
    config={'public_url':BASE,'api_key':'test-key','state_path':str(tmp_path/'auth.db')}
    with TestClient(build_app(config,httpx.ASGITransport(app=main.app)),base_url=BASE,follow_redirects=False) as c:
        tok,_=token(c)
        args={'request_id':'real-bridge-meal-1','name':'Integration meal','calories':123,
            'eaten_at':'2026-09-22T23:30:00-04:00'}
        r=rpc(c,tok['access_token'],'tools/call',{'name':'logMeal','arguments':args}).json()
        assert not r['result'].get('isError'),r
        r=rpc(c,tok['access_token'],'tools/call',{'name':'getDailySummary','arguments':{'day':'2026-09-22'}}).json()
        assert not r['result'].get('isError'),r
        content=r['result'].get('structuredContent') or json.loads(r['result']['content'][0]['text'])
        assert content['calories']==123
        assert content['meal_count']==1
        with main.SessionLocal() as db:
            assert db.get(main.SyncJob, 1).state == "pending"
    engine.dispose()


def test_discovery_aliases_do_not_redirect(setup):
    c,_,_,_=setup
    for path in ('/.well-known/oauth-authorization-server','/.well-known/oauth-authorization-server/',
                 '/.well-known/oauth-authorization-server/mcp','/mcp/.well-known/oauth-authorization-server'):
        r=c.get(path)
        assert r.status_code==200,(path,r.text)
        assert r.json()['code_challenge_methods_supported']==['S256']
        assert r.json()['issuer']==BASE+'/'
    for path in ('/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/'):
        r=c.get(path)
        assert r.status_code==200
        assert r.json()['resource']==BASE+'/mcp'


def test_request_diagnostics_exclude_secrets_and_unknown_paths(setup,caplog):
    import logging
    c,_,_,_=setup
    with caplog.at_level(logging.INFO,logger='uvicorn.error'):
        c.get('/.well-known/oauth-authorization-server?token=never-log-this',headers={'Authorization':'Bearer secret-header'})
        c.get('/secret-path-value?code=secret-code')
    messages='\n'.join(r.getMessage() for r in caplog.records if r.name=='uvicorn.error')
    assert 'path=/.well-known/oauth-authorization-server status=200' in messages
    assert 'path=<other> status=404' in messages
    for secret in ('never-log-this','secret-header','secret-path-value','secret-code'):
        assert secret not in messages


def test_log_checks_both_destinations_and_cached_retry_refreshes_status(setup):
    _, calls, config, bridge = setup
    checks = []
    def remote(req):
        if req.url.path.endswith('/verification'):
            checks.append(req)
            state = 'verified' if len(checks) == 1 else 'verifying'
            return httpx.Response(200, json={'local_saved': True,
                                           'fatsecret': {'status': state}})
        return bridge(req)
    with TestClient(build_app(config,httpx.MockTransport(remote)),base_url=BASE,follow_redirects=False) as c:
        tok,_ = token(c)
        args = {'request_id': 'verified-meal-request', 'name': 'SunChips', 'calories': 300}
        def log():
            result = rpc(c,tok['access_token'],'tools/call',{'name':'logMeal','arguments':args}).json()['result']
            assert not result.get('isError')
            return result.get('structuredContent') or json.loads(result['content'][0]['text'])
        first, second = log(), log()
        assert first['verification']['fatsecret']['status'] == 'verified'
        assert second['verification']['fatsecret']['status'] == 'verifying'
        assert len(checks) == 2
        assert len([r for r in calls if r.method == 'POST']) == 1


def test_check_failure_does_not_hide_successful_pi_save(setup):
    _, calls, config, bridge = setup
    def remote(req):
        if req.url.path.endswith('/verification'):
            raise httpx.ReadTimeout('downstream unavailable')
        return bridge(req)
    with TestClient(build_app(config,httpx.MockTransport(remote)),base_url=BASE,follow_redirects=False) as c:
        tok,_ = token(c)
        result = rpc(c,tok['access_token'],'tools/call',{'name':'logMeal','arguments':{
            'request_id':'verification-failed-test','name':'SunChips','calories':300}}).json()['result']
        assert not result.get('isError')
        content = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert content['verification']['local_saved']
        assert content['verification']['fatsecret']['status'] == 'unverified'
        assert len([r for r in calls if r.method == 'POST']) == 1
