"""RabbitMQ notification extension — publishes messages after DB commit.

Listens to the custom `items_committed` signal emitted by DatabasePipeline
after a successful commit. Only items that were actually persisted to the
database are published, eliminating phantom messages from duplicates or
failed inserts.

Uses pika (synchronous AMQP client) since the signal fires from synchronous
pipeline code. Connection is opened once at spider start and reused.

Enable in settings.py:
    EXTENSIONS = {
        "extensions.rabbitmq_notify.RabbitMQNotifyExtension": 500,
    }

Required settings (or set via environment):
    RABBITMQ_URL            - AMQP connection URL (default: amqp://guest:guest@localhost:5672/)
    RABBITMQ_EXCHANGE       - Exchange name (default: "" — the default exchange)
    RABBITMQ_ROUTING_KEY    - Routing key (default: scrapai_items)
    RABBITMQ_QUEUE          - Queue to declare (default: scrapai_items)
"""

import json
import logging
from datetime import datetime

from scrapy import signals

import signals as scrapai_signals

logger = logging.getLogger(__name__)


def _json_serializer(obj):
    """Fallback serializer for types json.dumps can't handle."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class RabbitMQNotifyExtension:
    """Synchronous RabbitMQ extension that publishes committed items via pika."""

    def __init__(self, amqp_url, exchange_name, routing_key, queue_name):
        self.amqp_url = amqp_url
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.queue_name = queue_name
        self.connection = None
        self.channel = None

    @classmethod
    def from_crawler(cls, crawler):
        import os

        ext = cls(
            amqp_url=crawler.settings.get(
                "RABBITMQ_URL",
                os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
            ),
            exchange_name=crawler.settings.get(
                "RABBITMQ_EXCHANGE",
                os.environ.get("RABBITMQ_EXCHANGE", ""),
            ),
            routing_key=crawler.settings.get(
                "RABBITMQ_ROUTING_KEY",
                os.environ.get("RABBITMQ_ROUTING_KEY", "scrapai_items"),
            ),
            queue_name=crawler.settings.get(
                "RABBITMQ_QUEUE",
                os.environ.get("RABBITMQ_QUEUE", "scrapai_items"),
            ),
        )
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(ext.items_committed, signal=scrapai_signals.items_committed)
        return ext

    def spider_opened(self, spider):
        """Open a persistent connection and declare the queue on spider start."""
        import pika

        try:
            params = pika.URLParameters(self.amqp_url)
            self.connection = pika.BlockingConnection(params)
            self.channel = self.connection.channel()

            # Declare queue (idempotent — creates if missing, no-op if exists)
            self.channel.queue_declare(queue=self.queue_name, durable=True)

            logger.info(
                f"RabbitMQ connected: queue={self.queue_name} "
                f"routing_key={self.routing_key}"
            )
        except Exception as e:
            logger.error(f"RabbitMQ connection failed: {e}")
            self.connection = None
            self.channel = None

    def spider_closed(self, spider):
        """Gracefully close the RabbitMQ connection on spider shutdown."""
        if self.connection and self.connection.is_open:
            self.connection.close()
            logger.info("RabbitMQ connection closed")

    def items_committed(self, items, spider):
        """Publish each committed item to RabbitMQ after DB write."""
        if not self.channel:
            return

        import pika

        published = 0
        for item in items:
            message_body = dict(item)
            message_body["spider"] = spider.name
            message_body["project"] = getattr(spider, "project", None)

            # Remove internal/non-serializable fields
            message_body.pop("spider_id", None)

            try:
                self.channel.basic_publish(
                    exchange=self.exchange_name,
                    routing_key=self.routing_key,
                    body=json.dumps(message_body, default=_json_serializer).encode(),
                    properties=pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=pika.DeliveryMode.Persistent,
                    ),
                )
                published += 1
            except Exception as e:
                logger.warning(f"RabbitMQ publish failed for {item.get('url')}: {e}")

        if published:
            logger.info(f"Published {published} items to RabbitMQ")
