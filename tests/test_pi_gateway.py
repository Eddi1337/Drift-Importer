"""The LXC UI stays responsive and commands affect only the configured Pi."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPBasic
from fastapi.testclient import TestClient

from app import pi_gateway


@pytest.fixture
def proxy(monkeypatch):
    settings=SimpleNamespace(processing_role='frontend',processing_source_url='http://192.168.3.188:8080')
    gateway=pi_gateway.PiGateway()
    monkeypatch.setattr(pi_gateway,'get_settings',lambda:settings)
    monkeypatch.setattr(pi_gateway,'gateway',gateway)
    app=FastAPI();security=HTTPBasic(auto_error=False)
    @app.middleware('http')
    async def route(request,call_next):
        result=await pi_gateway.route_request(request,lambda credentials:True,security)
        return result if result is not None else await call_next(request)
    @app.get('/trips')
    def local_page():return {'local':'fast map interface'}
    return settings,gateway,app


def transport(monkeypatch,respond):
    real=httpx.AsyncClient
    monkeypatch.setattr(pi_gateway.httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(respond),**kw))
    return real


def test_ui_is_local_reads_cache_and_commands_are_forwarded_once(proxy,monkeypatch):
    settings,gateway,app=proxy;seen=[];version=1
    async def respond(request):
        nonlocal version
        seen.append(request)
        if request.method=='POST':version+=1;return httpx.Response(200,json={'queued':42})
        return httpx.Response(200,json={'progress':version})
    transport(monkeypatch,respond)
    with TestClient(app) as client:
        assert client.get('/trips').json()=={'local':'fast map interface'} and not seen
        assert client.get('/api/jobs/overview').json()=={'progress':1}
        assert client.get('/api/jobs/overview').json()=={'progress':1} and len(seen)==1
        result=client.post('/api/import-device?url=http://other.invalid',json={'paths':['one.MP4']})
        assert result.json()=={'queued':42} and len(seen)==2
        assert seen[-1].url.host=='192.168.3.188'
        assert seen[-1].content==b'{"paths": ["one.MP4"]}' or seen[-1].content==b'{"paths":["one.MP4"]}'
        assert client.get('/api/jobs/overview').json()=={'progress':2} and len(seen)==3


def test_slow_refresh_is_background_and_offline_data_is_labelled(proxy,monkeypatch):
    _,gateway,app=proxy;calls=0;release=None
    async def respond(request):
        nonlocal calls
        calls+=1
        if calls>1:
            await release.wait()
            raise httpx.ConnectError('offline')
        return httpx.Response(200,json={'saved_rides':[1,2]})
    real=transport(monkeypatch,respond)
    async def scenario():
        nonlocal release
        release=asyncio.Event()
        async with real(transport=httpx.ASGITransport(app=app),base_url='http://lxc') as client:
            assert (await client.get('/api/roadtrips/auto')).json()=={'saved_rides':[1,2]}
            next(iter(gateway.cache.values())).saved-=100
            gateway.online=False
            result=await asyncio.wait_for(client.get('/api/roadtrips/auto'),.5)
            assert result.status_code==200 and result.headers['x-drift-stale']=='true'
            assert result.json()=={'saved_rides':[1,2]}
            release.set()
            await asyncio.gather(*list(gateway.refreshing.values()))
            assert gateway.online is False
    asyncio.run(scenario())


def test_concurrent_tabs_share_one_pi_request(proxy,monkeypatch):
    _,gateway,app=proxy;calls=0
    async def respond(request):
        nonlocal calls
        calls+=1;await asyncio.sleep(.03)
        return httpx.Response(200,json={'running':1})
    real=transport(monkeypatch,respond)
    async def scenario():
        async with real(transport=httpx.ASGITransport(app=app),base_url='http://lxc') as client:
            results=await asyncio.gather(*(client.get('/api/jobs/overview') for _ in range(8)))
            assert all(r.json()=={'running':1} for r in results)
        assert calls==1 and not gateway.locks and not gateway.lock_users
    asyncio.run(scenario())


def test_failed_command_is_not_retried_or_faked_as_queued(proxy,monkeypatch):
    _,gateway,app=proxy;seen=[]
    async def respond(request):seen.append(request);raise httpx.ConnectError('offline')
    transport(monkeypatch,respond)
    with TestClient(app) as client:
        result=client.post('/api/upload',json={'media_ids':[7],'destination_ids':[1]})
        assert result.status_code==503 and 'did not confirm' in result.json()['detail']
        assert len(seen)==1 and not gateway.cache


def test_movie_ranges_stream_without_buffering_and_close_upstream(proxy,monkeypatch):
    _,gateway,app=proxy;closed=[];seen=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):yield b'ab';yield b'cde'
        async def aclose(self):closed.append(True)
    async def respond(request):
        seen.append(request)
        return httpx.Response(206,headers={'Content-Type':'video/mp4','Content-Length':'5','Content-Range':'bytes 100-104/999','Accept-Ranges':'bytes'},stream=Stream())
    transport(monkeypatch,respond)
    with TestClient(app) as client:
        result=client.get('/api/media/4/stream',headers={'Range':'bytes=100-104'})
        assert result.status_code==206 and result.content==b'abcde'
        assert result.headers['content-range']=='bytes 100-104/999'
        assert seen[0].headers['range']=='bytes=100-104' and closed and not gateway.cache


def test_processor_keeps_maps_local_and_forwards_camera_controls(proxy,monkeypatch):
    settings,gateway,app=proxy;settings.processing_role='processor';seen=[]
    async def respond(request):seen.append(request);return httpx.Response(200,json={'Pi':True})
    transport(monkeypatch,respond)
    with TestClient(app) as client:
        assert client.post('/api/roadtrips/auto/discover').status_code==404 and not seen
        assert client.post('/api/import-device',json={}).json()=={'Pi':True}
        result=client.get('/api/connection').json()
        assert result['trip_processing']=='lxc' and result['pi_online'] is True


@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:secret@host','http://host/path','http://host?url=evil'])
def test_invalid_config_cannot_become_an_open_proxy(proxy,monkeypatch,url):
    settings,gateway,app=proxy;settings.processing_source_url=url
    transport(monkeypatch,lambda request:pytest.fail('Invalid target must not be contacted'))
    with TestClient(app) as client:assert client.get('/api/jobs').status_code==503


def test_routing_loop_is_stopped(proxy,monkeypatch):
    _,gateway,app=proxy
    transport(monkeypatch,lambda request:pytest.fail('Loop must not be forwarded'))
    with TestClient(app) as client:assert client.get('/api/jobs',headers={'X-Drift-Proxy-Hop':'1'}).status_code==508


def test_trip_activity_controls_use_local_queue_not_pi(proxy,monkeypatch):
    settings,gateway,app=proxy;settings.processing_role='processor'
    @app.post('/api/jobs/7/cancel')
    def local_cancel():return {'cancelled_on':'lxc'}
    transport(monkeypatch,lambda request:pytest.fail('Local trip controls must never reach the Pi'))
    with TestClient(app) as client:
        assert client.post('/api/processor/jobs/7/cancel').json()=={'cancelled_on':'lxc'}


def test_old_background_refresh_cannot_replace_state_after_a_command(proxy,monkeypatch):
    _,gateway,app=proxy;version=1;release=None;reads=0
    async def respond(request):
        nonlocal version,reads
        if request.method=='POST':version=2;return httpx.Response(200,json={'changed':True})
        reads+=1;captured=version
        if reads==2:await release.wait()
        return httpx.Response(200,json={'version':captured})
    real=transport(monkeypatch,respond)
    async def scenario():
        nonlocal release
        release=asyncio.Event()
        async with real(transport=httpx.ASGITransport(app=app),base_url='http://lxc') as client:
            await client.get('/api/status');next(iter(gateway.cache.values())).saved-=10
            await client.get('/api/status');await asyncio.sleep(.01)
            await client.post('/api/control',json={})
            assert (await client.get('/api/status')).json()=={'version':2}
            release.set();await asyncio.gather(*list(gateway.refreshing.values()))
            assert (await client.get('/api/status')).json()=={'version':2}
            assert reads==3
    asyncio.run(scenario())


@pytest.mark.parametrize('role',['processor','frontend'])
def test_fast_ui_opens_on_maps_with_clear_navigation_and_separate_activity(monkeypatch,role):
    from app.main import app
    from app.routers import pages
    monkeypatch.setattr(pages,'get_settings',lambda:SimpleNamespace(processing_role=role))
    with TestClient(app,raise_server_exceptions=True) as client:
        home=client.get('/',follow_redirects=False)
        assert home.status_code==307 and home.headers['location']=='/trips'
        page=client.get('/trips')
        assert page.status_code==200
        for text in ('Rides &amp; map','Camera &amp; uploads','piConnection','rtOpenLatestMap'):
            assert text in page.text
        jobs=client.get('/jobs?source=trips')
        assert ('data-job-source="trips"' in jobs.text)==(role=='processor')
