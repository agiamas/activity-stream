import asyncio
import os

import aiohttp
from prometheus_client import (
    CollectorRegistry,
    generate_latest,
)
import ujson

from shared.logger import (
    get_root_logger,
    get_child_logger,
    logged,
)
from shared.utils import (
    get_common_config,
    normalise_environment,
)

from .app_elasticsearch import (
    ESMetricsUnavailable,
    es_bulk,
    es_feed_activities_total,
    es_searchable_total,
    es_nonsearchable_total,
    es_min_verification_age,
    create_index,
    get_new_index_name,
    get_old_index_names,
    indexes_matching_feeds,
    indexes_matching_no_feeds,
    add_remove_aliases_atomically,
    delete_indexes,
    refresh_index,
)

from .app_feeds import (
    parse_feed_config,
)
from .app_metrics import (
    metric_counter,
    metric_inprogress,
    metric_timer,
    get_metrics,
)
from .app_redis import (
    redis_get_client,
    acquire_and_keep_lock,
    set_feed_updates_seed_url_init,
    set_feed_updates_seed_url,
    set_feed_updates_url,
    get_feed_updates_url,
    redis_set_metrics,
)
from .app_utils import (
    get_raven_client,
    async_repeat_until_cancelled,
    cancel_non_current_tasks,
    sleep,
    main,
)

EXCEPTION_INTERVALS = [1, 2, 4, 8, 16, 32, 64]
METRICS_INTERVAL = 1

UPDATES_INTERVAL = 1


async def run_outgoing_application():
    logger = get_root_logger('outgoing')

    with logged(logger, 'Examining environment', []):
        env = normalise_environment(os.environ)
        es_endpoint, redis_uri, sentry = get_common_config(env)
        feed_endpoints = [parse_feed_config(feed) for feed in env['FEEDS']]

    raven_client = get_raven_client(sentry)
    conn = aiohttp.TCPConnector(use_dns_cache=False, resolver=aiohttp.AsyncResolver())
    session = aiohttp.ClientSession(connector=conn, skip_auto_headers=['Accept-Encoding'])
    redis_client = await redis_get_client(redis_uri)

    metrics_registry = CollectorRegistry()
    metrics = get_metrics(metrics_registry)

    await acquire_and_keep_lock(logger, redis_client, raven_client, EXCEPTION_INTERVALS,
                                'lock')

    await create_outgoing_application(
        logger, metrics, raven_client, redis_client, session, feed_endpoints, es_endpoint,
    )

    await create_metrics_application(
        logger, metrics, metrics_registry, redis_client, raven_client,
        session, feed_endpoints, es_endpoint,
    )

    async def cleanup():
        await cancel_non_current_tasks()
        await raven_client.remote.get_transport().close()

        redis_client.close()
        await redis_client.wait_closed()

        await session.close()
        # https://github.com/aio-libs/aiohttp/issues/1925
        await asyncio.sleep(0.250)

    return cleanup


async def create_outgoing_application(logger, metrics, raven_client, redis_client, session,
                                      feed_endpoints, es_endpoint):
    async def ingester():
        await ingest_feeds(
            logger, metrics, raven_client, redis_client, session, feed_endpoints, es_endpoint,
        )
    asyncio.get_event_loop().create_task(
        async_repeat_until_cancelled(logger, raven_client, EXCEPTION_INTERVALS, ingester)
    )


async def ingest_feeds(logger, metrics, raven_client, redis_client, session,
                       feed_endpoints, es_endpoint):
    all_feed_ids = feed_unique_ids(feed_endpoints)
    indexes_without_alias, indexes_with_alias = await get_old_index_names(
        logger, session, es_endpoint,
    )

    indexes_to_delete = indexes_matching_no_feeds(
        indexes_without_alias + indexes_with_alias, all_feed_ids)
    await delete_indexes(
        logger, session, es_endpoint, indexes_to_delete,
    )

    def feed_ingester(ingest_type_logger, feed_lock, feed_endpoint, ingest_func):
        async def _feed_ingester():
            await ingest_func(ingest_type_logger, metrics, redis_client, session, feed_lock,
                              feed_endpoint, es_endpoint)
        return _feed_ingester

    await asyncio.gather(*[
        async_repeat_until_cancelled(ingest_type_logger, raven_client,
                                     feed_endpoint.exception_intervals, ingester)
        for feed_endpoint in feed_endpoints
        for feed_lock in [asyncio.Lock()]
        for feed_logger in [get_child_logger(logger, feed_endpoint.unique_id)]
        for feed_func_ingest_type in [(ingest_feed_full, 'full'), (ingest_feed_updates, 'updates')]
        for ingest_type_logger in [get_child_logger(feed_logger, feed_func_ingest_type[1])]
        for ingester in [feed_ingester(ingest_type_logger, feed_lock, feed_endpoint,
                                       feed_func_ingest_type[0])]
    ])


def feed_unique_ids(feed_endpoints):
    return [feed_endpoint.unique_id for feed_endpoint in feed_endpoints]


async def ingest_feed_full(logger, metrics, redis_client, session, feed_lock, feed, es_endpoint):
    with \
            logged(logger, 'Full ingest', []), \
            metric_timer(metrics['ingest_feed_duration_seconds'], [feed.unique_id, 'full']), \
            metric_inprogress(metrics['ingest_inprogress_ingests_total']):

        await set_feed_updates_seed_url_init(logger, redis_client, feed.unique_id)
        indexes_without_alias, _ = await get_old_index_names(
            logger, session, es_endpoint,
        )
        indexes_to_delete = indexes_matching_feeds(indexes_without_alias, [feed.unique_id])
        await delete_indexes(logger, session, es_endpoint, indexes_to_delete)

        index_name = get_new_index_name(feed.unique_id)
        await create_index(logger, session, es_endpoint, index_name)

        href = feed.seed
        while href:
            updates_href = href
            href = await ingest_feed_page(
                logger, metrics, session, 'full', feed_lock, feed, es_endpoint, [index_name], href
            )
            await sleep(logger, feed.polling_page_interval)

        await refresh_index(logger, session, es_endpoint, index_name)

        await add_remove_aliases_atomically(
            logger, session, es_endpoint, index_name, feed.unique_id,
        )

        await set_feed_updates_seed_url(logger, redis_client, feed.unique_id, updates_href)


async def ingest_feed_updates(logger, metrics, redis_client, session, feed_lock, feed,
                              es_endpoint):
    with \
            logged(logger, 'Updates ingest', []), \
            metric_timer(metrics['ingest_feed_duration_seconds'], [feed.unique_id, 'updates']):

        href = await get_feed_updates_url(logger, redis_client, feed.unique_id)
        indexes_without_alias, indexes_with_alias = await get_old_index_names(
            logger, session, es_endpoint,
        )

        # We deliberatly ingest into both the live and ingesting indexes
        indexes_to_ingest_into = indexes_matching_feeds(
            indexes_without_alias + indexes_with_alias, [feed.unique_id])

        while href:
            updates_href = href
            href = await ingest_feed_page(logger, metrics, session, 'updates', feed_lock, feed,
                                          es_endpoint, indexes_to_ingest_into, href)

        for index_name in indexes_matching_feeds(indexes_with_alias, [feed.unique_id]):
            await refresh_index(logger, session, es_endpoint, index_name)
        await set_feed_updates_url(logger, redis_client, feed.unique_id, updates_href)

    await sleep(logger, UPDATES_INTERVAL)


async def ingest_feed_page(logger, metrics, session, ingest_type, feed_lock, feed, es_endpoint,
                           index_names, href):
    with \
            logged(logger, 'Polling/pushing page', []), \
            metric_timer(metrics['ingest_page_duration_seconds'],
                         [feed.unique_id, ingest_type, 'total']):

        with \
                logged(logger, 'Polling page (%s)', [href]), \
                metric_timer(metrics['ingest_page_duration_seconds'],
                             [feed.unique_id, ingest_type, 'pull']):
            # Lock so there is only 1 request per feed at any given time
            async with feed_lock:
                feed_contents = await get_feed_contents(session, href, feed.auth_headers(href))

        with logged(logger, 'Parsing JSON', []):
            feed_parsed = ujson.loads(feed_contents)

        with logged(logger, 'Converting to bulk Elasticsearch items', []):
            es_bulk_items = feed.convert_to_bulk_es(feed_parsed, index_names)

        with \
                metric_timer(metrics['ingest_page_duration_seconds'],
                             [feed.unique_id, ingest_type, 'push']), \
                metric_counter(metrics['ingest_activities_nonunique_total'],
                               [feed.unique_id], len(es_bulk_items)):
            await es_bulk(logger, session, es_endpoint, es_bulk_items)

        return feed.next_href(feed_parsed)


async def get_feed_contents(session, href, headers):
    async with session.get(href, headers=headers) as result:
        if result.status != 200:
            raise Exception(await result.text())

        return await result.read()


async def create_metrics_application(parent_logger, metrics, metrics_registry, redis_client,
                                     raven_client, session, feed_endpoints, es_endpoint):
    logger = get_child_logger(parent_logger, 'metrics')

    async def poll_metrics():
        with logged(logger, 'Polling', []):
            searchable = await es_searchable_total(logger, session, es_endpoint)
            metrics['elasticsearch_activities_total'].labels('searchable').set(searchable)

            await set_metric_if_can(
                metrics['elasticsearch_activities_total'],
                ['nonsearchable'],
                es_nonsearchable_total(logger, session, es_endpoint),
            )
            await set_metric_if_can(
                metrics['elasticsearch_activities_age_minimum_seconds'],
                ['verification'],
                es_min_verification_age(logger, session, es_endpoint),
            )

            feed_ids = feed_unique_ids(feed_endpoints)
            for feed_id in feed_ids:
                try:
                    searchable, nonsearchable = await es_feed_activities_total(
                        logger, session, es_endpoint, feed_id)
                    metrics['elasticsearch_feed_activities_total'].labels(
                        feed_id, 'searchable').set(searchable)
                    metrics['elasticsearch_feed_activities_total'].labels(
                        feed_id, 'nonsearchable').set(nonsearchable)
                except ESMetricsUnavailable:
                    pass

        await redis_set_metrics(logger, redis_client, generate_latest(metrics_registry))
        await sleep(logger, METRICS_INTERVAL)

    asyncio.get_event_loop().create_task(
        async_repeat_until_cancelled(logger, raven_client, EXCEPTION_INTERVALS, poll_metrics)
    )


async def set_metric_if_can(metric, labels, get_value_coroutine):
    try:
        metric.labels(*labels).set(await get_value_coroutine)
    except ESMetricsUnavailable:
        pass


if __name__ == '__main__':
    main(run_outgoing_application)
