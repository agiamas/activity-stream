import asyncio
import functools
import json
import logging
import os
import signal
import sys

import raven
from raven_aiohttp import QueuedAioHttpTransport


def flatten(list_to_flatten):
    return [
        item
        for sublist in list_to_flatten
        for item in sublist
    ]


def async_repeat_until_cancelled(coroutine):

    async def _async_repeat_until_cancelled(*args, **kwargs):
        app_logger = logging.getLogger('activity-stream')

        kwargs_to_pass, (raven_client, exception_interval, logging_title) = \
            extract_keys(kwargs, [
                '_async_repeat_until_cancelled_raven_client',
                '_async_repeat_until_cancelled_exception_interval',
                '_async_repeat_until_cancelled_logging_title',
            ])

        while True:
            try:
                await coroutine(*args, **kwargs_to_pass)
            except asyncio.CancelledError:
                break
            except BaseException as exception:
                app_logger.exception('%s raised exception: %s', logging_title, exception)
                raven_client.captureException()
                app_logger.warning('Waiting %s seconds until restarting', exception_interval)

                try:
                    await asyncio.sleep(exception_interval)
                except asyncio.CancelledError:
                    break

    return _async_repeat_until_cancelled


def sub_dict_lower(super_dict, keys):
    return {
        key.lower(): super_dict[key]
        for key in keys
    }


def extract_keys(dictionary, keys):
    extracted = [
        dictionary[key]
        for key in keys
    ]
    without_keys = {
        key: value
        for key, value in dictionary.items()
        if key not in keys
    }
    return without_keys, extracted


async def cancel_non_current_tasks():
    current_task = asyncio.Task.current_task()
    all_tasks = asyncio.Task.all_tasks()
    non_current_tasks = [task for task in all_tasks if task != current_task]
    for task in non_current_tasks:
        task.cancel()
    # Allow CancelledException to be thrown at the location of all awaits
    await asyncio.sleep(0)


def get_common_config(env):
    es_endpoint = {
        'host': env['ELASTICSEARCH']['HOST'],
        'access_key_id': env['ELASTICSEARCH']['AWS_ACCESS_KEY_ID'],
        'secret_key': env['ELASTICSEARCH']['AWS_SECRET_ACCESS_KEY'],
        'region': env['ELASTICSEARCH']['REGION'],
        'protocol': env['ELASTICSEARCH']['PROTOCOL'],
        'base_url': (
            env['ELASTICSEARCH']['PROTOCOL'] + '://' +
            env['ELASTICSEARCH']['HOST'] + ':' + env['ELASTICSEARCH']['PORT']
        ),
        'port': env['ELASTICSEARCH']['PORT'],
    }
    redis_uri = json.loads(os.environ['VCAP_SERVICES'])['redis'][0]['credentials']['uri']
    sentry = {
        'dsn': env['SENTRY_DSN'],
        'environment': env['SENTRY_ENVIRONMENT'],
    }
    return es_endpoint, redis_uri, sentry


def get_raven_client(sentry):
    return raven.Client(
        sentry['dsn'],
        environment=sentry['environment'],
        transport=functools.partial(QueuedAioHttpTransport, workers=1, qsize=1000))


def main(run_application_coroutine):
    stdout_handler = logging.StreamHandler(sys.stdout)
    aiohttp_log = logging.getLogger('aiohttp.access')
    aiohttp_log.setLevel(logging.DEBUG)
    aiohttp_log.addHandler(stdout_handler)

    app_logger = logging.getLogger('activity-stream')
    app_logger.setLevel(logging.DEBUG)
    app_logger.addHandler(stdout_handler)

    loop = asyncio.get_event_loop()
    cleanup = loop.run_until_complete(run_application_coroutine())

    async def cleanup_then_stop_loop():
        await cleanup()
        asyncio.get_event_loop().stop()
        return 'anything-to-avoid-pylint-assignment-from-none-error'

    cleanup_then_stop = cleanup_then_stop_loop()
    loop.add_signal_handler(signal.SIGINT, loop.create_task, cleanup_then_stop)
    loop.add_signal_handler(signal.SIGTERM, loop.create_task, cleanup_then_stop)
    loop.run_forever()
    app_logger.info('Reached end of main. Exiting now.')
