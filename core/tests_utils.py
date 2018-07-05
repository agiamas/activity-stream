import asyncio
import json

import aiohttp
from aiohttp import web
import mohawk

from core.app import run_application


def run_app_until_accepts_http():
    asyncio.ensure_future(run_application())
    is_http_accepted_eventually()


def is_http_accepted_eventually():
    loop = asyncio.get_event_loop()
    connected_future = asyncio.ensure_future(_is_http_accepted_eventually())
    return loop.run_until_complete(connected_future)


async def _is_http_accepted_eventually():
    def is_connection_error(exception):
        return 'Cannot connect to host' in str(exception)

    attempts = 0
    while attempts < 20:
        try:
            async with aiohttp.ClientSession() as session:
                url = 'http://127.0.0.1:8080/'
                auth = hawk_auth_header(
                    'incoming-some-id-3', 'incoming-some-secret-3', url, 'GET', '', '',
                )
                await session.get(url, headers={
                    'Authorization': auth,
                    'X-Forwarded-For': '1.2.3.4',
                    'X-Forwarded-Proto': 'http',
                    'Content-Type': '',
                }, timeout=1)
            return True
        except aiohttp.client_exceptions.ClientConnectorError as exception:
            attempts += 1
            await asyncio.sleep(0.2)
            if not is_connection_error(exception):
                return True

    return False


def read_file(path):
    with open('core/' + path, 'rb') as file:
        return file.read().decode('utf-8')


async def delete_all_es_data():
    async with aiohttp.ClientSession() as session:
        await session.delete('http://127.0.0.1:9200/*')


async def fetch_all_es_data_until(condition, sleep):

    async def fetch_all_es_data():
        async with aiohttp.ClientSession() as session:
            results = await session.get('http://127.0.0.1:9200/_search')
            return json.loads(await results.text())

    while True:
        all_es_data = await fetch_all_es_data()
        if condition(all_es_data):
            break
        await sleep(0.05)

    return all_es_data


def append_until(condition):
    future = asyncio.Future()

    all_data = []

    def append(data):
        if not future.done():
            all_data.append(data)
        if condition(all_data):
            future.set_result(all_data)

    return (future, append)


def hawk_auth_header(key_id, secret_key, url, method, content, content_type):
    return mohawk.Sender({
        'id': key_id,
        'key': secret_key,
        'algorithm': 'sha256',
    }, url, method, content=content, content_type=content_type).request_header


async def get(url, auth, x_forwarded_for, body):
    async with aiohttp.ClientSession() as session:
        result = await session.get(url, headers={
            'Authorization': auth,
            'Content-Type': 'application/json',
            'X-Forwarded-For': x_forwarded_for,
            'X-Forwarded-Proto': 'http',
        }, data=body, timeout=1)
    return (await result.text(), result.status, result.headers)


async def get_until(url, x_forwarded_for, condition, sleep):
    while True:
        auth = hawk_auth_header(
            'incoming-some-id-3', 'incoming-some-secret-3', url, 'GET', '', 'application/json',
        )
        all_data, status, headers = await get(url, auth, x_forwarded_for, b'')
        dict_data = json.loads(all_data)
        if condition(dict_data):
            break
        await sleep(0.05)

    return dict_data, status, headers


async def post(url, auth, x_forwarded_for):
    return await post_with_headers(url, {
        'Authorization': auth,
        'Content-Type': '',
        'X-Forwarded-For': x_forwarded_for,
        'X-Forwarded-Proto': 'http',
    })


async def post_with_headers(url, headers):
    async with aiohttp.ClientSession(skip_auto_headers=['Content-Type']) as session:
        result = await session.post(url, headers=headers, timeout=1)
    return (await result.text(), result.status)


def respond_http(text, status):
    async def response(_):
        return web.Response(text=text, status=status, content_type='application/json')

    return response


async def run_es_application(port, override_routes):
    default_routes = [
        web.put('/activities/_mapping/_doc', respond_http('{}', 200)),
        web.put('/activities', respond_http('{}', 200)),
        web.get('/_search', respond_http('{}', 200)),
        web.post('/_bulk', respond_http('{}', 200)),
    ]

    routes_no_duplicates = {
        (route.method, route.path): route
        for route in (default_routes+override_routes)
    }.values()

    return await _web_application(port=port, routes=routes_no_duplicates)


async def run_feed_application(feed, feed_requested_callback, port):
    async def handle(request):
        path = request.match_info['feed']
        asyncio.get_event_loop().call_soon(feed_requested_callback, request)
        return web.Response(text=feed(path))

    routes = [web.get('/{feed}', handle)]
    return await _web_application(port=port, routes=routes)


async def _web_application(port, routes):
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()
    return runner


def has_at_least(num_results):
    return lambda results: (
        'hits' in results and 'hits' in results['hits'] and
        len(results['hits']['hits']) >= num_results
    )


def has_at_least_ordered_items(num_results):
    return lambda results: len(results['orderedItems']) >= num_results


def mock_env():
    return {
        'PORT': '8080',
        'ELASTICSEARCH__AWS_ACCESS_KEY_ID': 'some-id',
        'ELASTICSEARCH__AWS_SECRET_ACCESS_KEY': 'aws-secret',
        'ELASTICSEARCH__HOST': '127.0.0.1',
        'ELASTICSEARCH__PORT': '9200',
        'ELASTICSEARCH__PROTOCOL': 'http',
        'ELASTICSEARCH__REGION': 'us-east-2',
        'FEEDS__1__SEED': 'http://localhost:8081/tests_fixture_activity_stream_1.json',
        'FEEDS__1__ACCESS_KEY_ID': 'feed-some-id',
        'FEEDS__1__SECRET_ACCESS_KEY': '?[!@$%^%',
        'FEEDS__1__TYPE': 'activity_stream',
        'INCOMING_ACCESS_KEY_PAIRS__1__KEY_ID': 'incoming-some-id-1',
        'INCOMING_ACCESS_KEY_PAIRS__1__SECRET_KEY': 'incoming-some-secret-1',
        'INCOMING_ACCESS_KEY_PAIRS__1__PERMISSIONS__1': 'POST',
        'INCOMING_ACCESS_KEY_PAIRS__2__KEY_ID': 'incoming-some-id-2',
        'INCOMING_ACCESS_KEY_PAIRS__2__SECRET_KEY': 'incoming-some-secret-2',
        'INCOMING_ACCESS_KEY_PAIRS__2__PERMISSIONS__1': 'POST',
        'INCOMING_ACCESS_KEY_PAIRS__3__KEY_ID': 'incoming-some-id-3',
        'INCOMING_ACCESS_KEY_PAIRS__3__SECRET_KEY': 'incoming-some-secret-3',
        'INCOMING_ACCESS_KEY_PAIRS__3__PERMISSIONS__1': 'GET',
        'INCOMING_IP_WHITELIST__1': '1.2.3.4',
        'INCOMING_IP_WHITELIST__2': '2.3.4.5',
    }