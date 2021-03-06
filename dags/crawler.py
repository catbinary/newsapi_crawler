import abc
import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from distutils.util import strtobool

import redis
from airflow import DAG, AirflowException
from airflow.logging_config import log
from airflow.operators import BaseOperator
from google.api_core.exceptions import DeadlineExceeded
from google.cloud import bigquery
from google.cloud import firestore, pubsub_v1
from google.cloud.client import ClientWithProject
from google.cloud.pubsub_v1 import publisher
from newsapi import NewsApiClient
from pymongo import MongoClient

NEWSAPI_TOKEN = open(os.environ.get('NEWSAPI_TOKEN_FILE'), 'r').read()
NEWSAPI_DEV_MODE = strtobool(os.environ.get('NEWSAPI_DEV_MODE'))

MONGO_HOST = os.environ.get('MONGO_HOST')
MONGO_PORT = int(os.environ.get('MONGO_PORT'))
MONGO_PASSWORD = os.environ.get('MONGO_PASSWORD')
MONGO_USER = os.environ.get('MONGO_USER')

REDIS_HOST = os.environ.get('REDIS_HOST')
REDIS_PORT = int(os.environ.get('REDIS_PORT'))

GOOGLE_CLOUD_PROJECT = open(os.environ.get('GOOGLE_CLOUD_PROJECT_FILE'), 'r').read()
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')

GOOGLE_CLOUD_TIMEOUT = int(os.environ.get('GOOGLE_CLOUD_TIMEOUT'))
GOOGLE_CLOUD_REQUEST_LIMIT = int(os.environ.get('GOOGLE_CLOUD_REQUEST_LIMIT'))

BIGQUERY_DATASET_NAME = 'newsapi'
BIGQUERY_TABLE_NAME = 'articles'
BIGQUERY_TABLE_SCHEMA = json.load(open(os.environ.get('GOOGLE_BIGQUERY_TABLE_SCHEMA_FILE'), 'r'))

NEWSAPI_QUERY_KEYWORDS = open(os.environ.get('NEWSAPI_QUERY_KEYWORDS_FILE'), 'r').read().split()

NEWSAPI_URL = 'https://newsapi.org/v2/everything'

MONGO_URI = f'mongodb://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}'

log.debug(f'NEWSAPI_TOKEN: {NEWSAPI_TOKEN}')

COLLECTION_SOURCES_KEY = 'sources'
COLLECTION_ARTICLES_KEY = 'articles'
SOURCES_SCHEMA_KEY = 'sources_schema'
ARTICLES_SCHEMA_KEY = 'article_schema'

SOURCES_SCHEMA_SOURCES_KEY = 'sources-schema'
ARTICLES_SCHEMA_SOURCES_KEY = 'articles-schema'
GOOGLE_TIMEOUT_KEY = 'GOOGLE_TIMEOUT'

# start date
_now = datetime.now()
start_date = datetime(_now.year, _now.month, _now.day)

default_args = {
    'owner': 'airflow',
    'depends_on_past': True,
    'start_date': start_date - timedelta(days=2),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),

    'method': 'GET',
    'headers': {'Authorization': f'Bearer {NEWSAPI_TOKEN}'},
    'http_conn_id': 'newsapi',
    'endpoint': '/',
    'log_response': False,
}

default_dag = DAG('newsapi_crawler', default_args=default_args, schedule_interval=timedelta(days=1), )


def get_redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT)


def get_google_timeout():
    redis_client = get_redis_client()
    return redis_client.get(GOOGLE_TIMEOUT_KEY)


def set_google_timeout():
    redis_client = get_redis_client()
    return redis_client.set(GOOGLE_TIMEOUT_KEY, GOOGLE_CLOUD_TIMEOUT, 1)


def _get_google_cloud_client(google_cloud_module) -> ClientWithProject:
    # check timeout
    if get_google_timeout():
        log.info('waiting for GOOGLE TIMEOUT')
        time.sleep(GOOGLE_CLOUD_TIMEOUT)

    client = None
    while client is None:
        try:
            client = google_cloud_module.Client()
        except DeadlineExceeded:
            log.info('google DeadlineExceeded exception')
            time.sleep(GOOGLE_CLOUD_TIMEOUT)

    # set timeout
    set_google_timeout()

    return client


def get_store_client() -> firestore.Client:
    store_client = _get_google_cloud_client(firestore)
    return store_client


def get_publisher_client() -> pubsub_v1.PublisherClient:
    publisher_client = _get_google_cloud_client(publisher)
    return publisher_client


def get_bigquery_client() -> bigquery.Client:
    bigquery_client = _get_google_cloud_client(bigquery)
    return bigquery_client


def get_mongo_client() -> MongoClient:
    mongo_client = MongoClient(MONGO_URI)
    return mongo_client


def save_to_mongo(collection: str, data: list):
    mongo_client = get_mongo_client()

    airflow_db = mongo_client.get_database('airflow')
    sources_collection = airflow_db.get_collection(collection)
    sources_collection.insert_many(data)


def get_google_store_source_key(date: str):
    return f'{COLLECTION_SOURCES_KEY}-{date}'


def get_google_store_source_schema_key(date: str):
    return f'source-schema-{date}'


def get_google_store_article_schema_key(date: str):
    return f'article-schema-{date}'


def get_newsapi_client() -> NewsApiClient:
    return NewsApiClient(NEWSAPI_TOKEN)


def check_pubsub_topic(pubsub_client: pubsub_v1.PublisherClient, topic):
    # check topic in pubsub
    project_path = pubsub_client.project_path(GOOGLE_CLOUD_PROJECT)
    topics = [topic_ref.name for topic_ref in pubsub_client.list_topics(project_path)]
    if topic not in topics:
        log.info(f'create pubsub topic {topic}')
        pubsub_client.create_topic(topic)


class NewsApiError(AirflowException): ...


class _BaseOperator(BaseOperator):
    task_id: str
    execution_date: datetime
    prev_execution_date: datetime

    def __init__(self, *args, **kwargs):
        super().__init__(task_id=self.task_id, dag=default_dag, *args, **kwargs)

    def execute(self, context: dict):
        setattr(self, 'execution_date', context.get('execution_date'))
        setattr(self, 'prev_execution_date', (context.get('execution_date') - timedelta(days=1)))

        return self.execute_(context)

    @abc.abstractmethod
    def execute_(self, context: dict):
        pass


class LoadsDataToBigQuery(_BaseOperator):
    task_id = 'loads_data_to_big_query'

    def execute_(self, context):
        current_date = self.execution_date.date().isoformat()

        firestore_client = get_store_client()
        articles_ref = firestore_client.collection(COLLECTION_ARTICLES_KEY)
        articles_date_ref = articles_ref.document(current_date)

        #
        bigquery_client = get_bigquery_client()
        table = bigquery_client.get_table(bigquery_client.dataset(BIGQUERY_DATASET_NAME).table(BIGQUERY_TABLE_NAME))

        for keyword in NEWSAPI_QUERY_KEYWORDS:
            keyword_ref = articles_date_ref.collection(keyword)

            for article in keyword_ref.stream():
                row = article.to_dict()
                row['id'] = article.id
                row['publishedAt'] = row['publishedAt'][:19]
                row['date'] = current_date

                bigquery_client.insert_rows_json(table, [row])


class CheckAndCreateBigQueryTable(_BaseOperator):
    task_id = 'check_and_create_big_query_table'

    def execute_(self, context):
        bigquery_client = get_bigquery_client()
        newsapi_dataset = bigquery_client.dataset(BIGQUERY_DATASET_NAME, GOOGLE_CLOUD_PROJECT)

        # create dataset exists true
        bigquery_client.create_dataset(newsapi_dataset, True)
        # create table exists true
        bigquery_client.create_table(newsapi_dataset.table(BIGQUERY_TABLE_NAME), True)

        table = bigquery_client.get_table(newsapi_dataset.table(BIGQUERY_TABLE_NAME))

        current_schema = table.schema
        current_names = [_.name for _ in current_schema]

        new_schema = current_schema[:]

        def _get_field_schema(field_name, field_schema):
            fields = []
            if 'fields' in field_schema:
                for _extra_field, _extra_schema in field_schema['fields'].items():
                    fields.append(_get_field_schema(_extra_field, _extra_schema))
            return bigquery.SchemaField(name=field_name, field_type=field_schema['type'], fields=fields)

        for field_name, field_schema in BIGQUERY_TABLE_SCHEMA.items():
            if field_name not in current_names:
                new_schema.append(_get_field_schema(field_name, field_schema))

        table.schema = new_schema
        table = bigquery_client.update_table(table, ["schema"])


class GetData(_BaseOperator):
    task_id = 'get_data_api'

    def execute_(self, context):
        newsapi_client = get_newsapi_client()
        page_size = 10

        firestore_client = get_store_client()
        articles_ref = firestore_client.collection(COLLECTION_ARTICLES_KEY)
        articles_date_ref = articles_ref.document(self.execution_date.date().isoformat())

        for keyword in NEWSAPI_QUERY_KEYWORDS:
            page = 1

            while True:
                if NEWSAPI_DEV_MODE and page > 10:
                    break

                result = newsapi_client.get_everything(
                    q=keyword,
                    from_param=self.prev_execution_date.isoformat(),
                    to=self.execution_date.isoformat(),
                    page_size=page_size,
                    page=page,
                    sort_by='publishedAt'
                )

                articles = result['articles']
                if not len(articles):
                    break

                for article in articles:
                    hash = hashlib.md5(bytes(article['url'], 'utf8')).hexdigest()

                    article['id'] = hash
                    article['keyword'] = keyword
                    article['date'] = self.execution_date.date().isoformat()

                    articles_date_ref.collection(keyword).document(hash).set(article)

                page += 1


############
# sources

class ClearOldSourceData(_BaseOperator):
    task_id = 'clear-old-source-data'

    def execute_(self, context: dict):
        firestore_client = get_store_client()
        _date = (self.prev_execution_date.date() - timedelta(days=1))

        sources_ref = firestore_client.collection(COLLECTION_SOURCES_KEY)

        _date = (self.prev_execution_date.date() - timedelta(days=1))
        while sources_ref.document(_date.isoformat()).get().exists:
            sources_ref.document(_date.isoformat()).delete()
            _date = (_date - timedelta(days=1))


class CheckAndPublishNewsSources(_BaseOperator):
    task_id = 'publish_news_sources'

    def execute_(self, context: dict):
        # get pubsub client
        pubsub_client = get_publisher_client()
        topic = pubsub_client.topic_path(GOOGLE_CLOUD_PROJECT, COLLECTION_SOURCES_KEY)

        # check topic in pubsub
        check_pubsub_topic(pubsub_client, topic)

        # get sore client
        firestore_client = get_store_client()

        execution_date = self.execution_date.date().isoformat()
        prev_execution_date = self.prev_execution_date.isoformat()

        sources_ref = firestore_client.collection(COLLECTION_SOURCES_KEY)

        if not sources_ref.document(prev_execution_date).get().exists:
            return

        # get previous news_sources
        current_keys = list(sources_ref.document(execution_date).get().to_dict())
        prev_keys = list(sources_ref.document(prev_execution_date).get().to_dict())

        remove = set(prev_keys).difference(set(current_keys))
        new = set(current_keys).difference(set(prev_keys))

        if len(remove):
            pubsub_client.publish(topic, bytes(json.dumps({'remove': list(remove)}), 'utf8'))
        if len(new):
            pubsub_client.publish(topic, bytes(json.dumps({'new': list(new)}), 'utf8'))


class GetNewsSources(_BaseOperator):
    task_id = 'get_news_sources'

    def execute_(self, context: dict):
        newsapi_client = get_newsapi_client()
        sources = newsapi_client.get_sources()['sources']

        #
        log.info(f'sources_count: {len(sources)}')
        execution_date = self.execution_date.date().isoformat()

        # get sore client
        firestore_client = get_store_client()

        # get collection
        sources_ref = firestore_client.collection(COLLECTION_SOURCES_KEY)

        # set new sources
        if sources_ref.document(execution_date).get().exists:
            log.warning(f'overriding sources for date {execution_date}')

        sources_ref.document(execution_date).set({source['id']: source for source in sources})


clear_old_sources = ClearOldSourceData()
publish_news_sources = CheckAndPublishNewsSources()
get_news_sources = GetNewsSources()

clear_old_sources.set_upstream(publish_news_sources)
publish_news_sources.set_upstream(get_news_sources)


# sources
############

class ClearOldSchemas(_BaseOperator):
    task_id = 'clear_old_schemas'

    def execute_(self, context: dict):
        firestore_client = get_store_client()

        sources_schema_ref = firestore_client.collection(SOURCES_SCHEMA_KEY)
        article_schema_ref = firestore_client.collection(ARTICLES_SCHEMA_KEY)

        # delete source schemas
        _date = (self.prev_execution_date.date() - timedelta(days=1))
        while sources_schema_ref.document(_date.isoformat()).get().exists:
            sources_schema_ref.document(_date.isoformat()).delete()
            _date = (_date - timedelta(days=1))

        # delete articles schemas
        _date = (self.prev_execution_date.date() - timedelta(days=1))
        while article_schema_ref.document(_date.isoformat()).get().exists:
            article_schema_ref.document(_date.isoformat()).delete()
            _date = (_date - timedelta(days=1))


class CheckDataSchemas(_BaseOperator):
    task_id = 'check_data_schemas'

    def execute_(self, context):
        execution_date = self.execution_date.date().isoformat()
        prev_execution_date = self.prev_execution_date.isoformat()

        firestore_client = get_store_client()

        sources_schema_ref = firestore_client.collection(SOURCES_SCHEMA_KEY)
        article_schema_ref = firestore_client.collection(ARTICLES_SCHEMA_KEY)

        # get pubsub client
        pubsub_client = get_publisher_client()
        sources_schema_topic = pubsub_client.topic_path(GOOGLE_CLOUD_PROJECT, SOURCES_SCHEMA_SOURCES_KEY)
        articles_schema_topic = pubsub_client.topic_path(GOOGLE_CLOUD_PROJECT, ARTICLES_SCHEMA_SOURCES_KEY)

        # check topic in pubsub
        check_pubsub_topic(pubsub_client, sources_schema_topic)
        check_pubsub_topic(pubsub_client, articles_schema_topic)

        # source schema
        prev_sources_schema = sources_schema_ref.document(prev_execution_date).get()
        sources_schema = sources_schema_ref.document(execution_date).get()

        if prev_sources_schema.exists and sources_schema.exists:

            remove = set(prev_sources_schema.to_dict()['keys']).difference(set(sources_schema.to_dict()['keys']))
            new = set(sources_schema.to_dict()['keys']).difference(set(prev_sources_schema.to_dict()['keys']))

            if len(remove):
                pubsub_client.publish(sources_schema_topic, bytes(json.dumps({'remove': list(remove)}), 'utf8'))
            if len(new):
                pubsub_client.publish(sources_schema_topic, bytes(json.dumps({'new': list(new)}), 'utf8'))
        elif sources_schema.exists:
            pubsub_client.publish(
                articles_schema_topic,
                bytes(json.dumps({'new': list(sources_schema.to_dict()['keys'])}), 'utf8')
            )

        # article schema
        prev_article_schema = article_schema_ref.document(prev_execution_date).get()
        article_schema = article_schema_ref.document(execution_date).get()

        if prev_article_schema.exists and article_schema.exists:

            remove = set(prev_article_schema.to_dict()['keys']).difference(set(article_schema.to_dict()['keys']))
            new = set(article_schema.to_dict()['keys']).difference(set(prev_article_schema.to_dict()['keys']))

            if len(remove):
                pubsub_client.publish(articles_schema_topic, bytes(json.dumps({'remove': list(remove)}), 'utf8'))
            if len(new):
                pubsub_client.publish(articles_schema_topic, bytes(json.dumps({'new': list(new)}), 'utf8'))
        elif article_schema.exists:
            pubsub_client.publish(
                articles_schema_topic,
                bytes(json.dumps({'new': list(article_schema.to_dict()['keys'])}), 'utf8')
            )


class GetDataSchemas(_BaseOperator):
    task_id = 'get_data_schemas'

    def execute_(self, context):
        execution_date = self.execution_date.date().isoformat()

        client = get_newsapi_client()
        firestore_client = get_store_client()

        sources = client.get_sources(language='en').get('sources')
        top_headlines = client.get_top_headlines(page_size=1, page=1).get('articles')

        source = sources.pop()
        headline = top_headlines.pop()

        source_keys = list(source.keys())
        headline_keys = list(headline.keys())

        sources_schema_ref = firestore_client.collection('sources_schema')
        article_schema_ref = firestore_client.collection('article_schema')

        sources_schema_ref.document(execution_date).set({'keys': source_keys})
        article_schema_ref.document(execution_date).set({'keys': headline_keys})


class PingNewsApiOperator(_BaseOperator):
    task_id = 'ping_news_api'

    def execute_(self, context):
        get_newsapi_client()


#
ping_task = PingNewsApiOperator()
get_schemas_task = GetDataSchemas()
clear_old_schemas = ClearOldSchemas()
check_schemas_task = CheckDataSchemas()
get_data_task = GetData()
loads_data_task = LoadsDataToBigQuery()
check_data_schemas = CheckAndCreateBigQueryTable()

#
loads_data_task.set_upstream(check_data_schemas)
check_data_schemas.set_upstream(get_data_task)

# get data after clear
get_data_task.set_upstream(clear_old_schemas)
get_data_task.set_upstream(clear_old_sources)

clear_old_schemas.set_upstream(check_schemas_task)
check_schemas_task.set_upstream(get_schemas_task)

get_news_sources.set_upstream(ping_task)
get_schemas_task.set_upstream(ping_task)
