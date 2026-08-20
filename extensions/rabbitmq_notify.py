"""RabbitMQ notification extension — publishes messages after DB commit.

Listens to the custom `items_committed` signal emitted by DatabasePipeline
after a successful commit. Only items that were actually persisted to the
database are published, eliminating phantom messages from duplicates or
failed inserts.

Uses pika (synchronous AMQP client) since the signal fires from synchronous
pipeline code. Connection is opened once at spider start and reused, with
automatic reconnection on failure.

Enable via environment variable:
    RABBITMQ_URL=amqp://guest:guest@localhost:5672/

The extension is auto-enabled in settings.py when RABBITMQ_URL is set.

Required settings (or set via environment):
    RABBITMQ_URL            - AMQP connection URL (default: amqp://guest:guest@localhost:5672/)
    RABBITMQ_EXCHANGE       - Exchange name (default: "" — the default exchange)
    RABBITMQ_ROUTING_KEY    - Routing key (default: scrapai_items)
    RABBITMQ_QUEUE          - Queue to declare (default: scrapai_items)
"""

import json
import logging
import os
from datetime import datetime

from scrapy import signals

import signals as scrapai_signals

logger = logging.getLogger(__name__)

# 32 MB max message size
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


def _json_serializer(obj):
    """Fallback serializer for types json.dumps can't handle."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _db_item_to_dict(db_item):
    """Serialize a ScrapedItem ORM object to a plain dict for publishing."""
    return {
        "id": db_item.id,
        "spider_id": db_item.spider_id,
        "url": db_item.url,
        "title": db_item.title,
        "content": db_item.content,
        "published_date": db_item.published_date,
        "author": db_item.author,
        "scraped_at": db_item.scraped_at,
        "metadata_json": db_item.metadata_json,
    }


class RabbitMQNotifyExtension:
    """Synchronous RabbitMQ extension that publishes committed items via pika."""

    MAX_RECONNECT_ATTEMPTS = 3

    def __init__(self, amqp_url, exchange_name, routing_key, queue_name):
        self.amqp_url = amqp_url
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.queue_name = queue_name
        self.connection = None
        self.channel = None

    @classmethod
    def from_crawler(cls, crawler):
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

    def _connect(self):
        """Open connection and declare queue. Returns True on success."""
        import pika

        try:
            params = pika.URLParameters(self.amqp_url)
            self.connection = pika.BlockingConnection(params)
            self.channel = self.connection.channel()
            self.channel.queue_declare(queue=self.queue_name, durable=True)
            return True
        except Exception as e:
            logger.error(f"RabbitMQ connection failed: {e}")
            self.connection = None
            self.channel = None
            return False

    def _ensure_connected(self):
        """Reconnect if the connection is dead. Returns True if usable."""
        if self.connection and self.connection.is_open:
            return True

        logger.warning("RabbitMQ connection lost, attempting reconnect...")
        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            if self._connect():
                logger.info(f"RabbitMQ reconnected (attempt {attempt})")
                return True
            logger.warning(f"RabbitMQ reconnect attempt {attempt} failed")
        return False

    def spider_opened(self, spider):
        """Open a persistent connection and declare the queue on spider start."""
        if self._connect():
            logger.info(
                f"RabbitMQ connected: queue={self.queue_name} "
                f"routing_key={self.routing_key}"
            )

    def spider_closed(self, spider):
        """Gracefully close the RabbitMQ connection on spider shutdown."""
        if self.connection and self.connection.is_open:
            self.connection.close()
            logger.info("RabbitMQ connection closed")

    def items_committed(self, items, spider):
        """Publish each committed db_item to RabbitMQ after DB write."""
        if not self._ensure_connected():
            logger.error(
                f"RabbitMQ unavailable, dropping {len(items)} messages"
            )
            return

        import pika

        published = 0
        skipped = 0
        for db_item in items:
            message_body = _db_item_to_dict(db_item)
            message_body["spider"] = spider.name
            message_body["project"] = getattr(spider, "project", None)

            body_bytes = json.dumps(
                message_body, default=_json_serializer
            ).encode()

            if len(body_bytes) > MAX_MESSAGE_BYTES:
                logger.warning(
                    f"Skipping oversized message ({len(body_bytes)} bytes) "
                    f"for {db_item.url}"
                )
                skipped += 1
                continue

            try:
                self.channel.basic_publish(
                    exchange=self.exchange_name,
                    routing_key=self.routing_key,
                    body=body_bytes,
                    properties=pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=pika.DeliveryMode.Persistent,
                    ),
                )
                published += 1
            except Exception as e:
                logger.warning(f"RabbitMQ publish failed for {db_item.url}: {e}")
                # Connection likely dead — try to reconnect for remaining items
                if not self._ensure_connected():
                    logger.error(
                        f"RabbitMQ reconnect failed, dropping remaining "
                        f"{len(items) - published - skipped} messages"
                    )
                    break

        if published:
            logger.info(f"Published {published} items to RabbitMQ")
        if skipped:
            logger.warning(f"Skipped {skipped} oversized items (>{MAX_MESSAGE_BYTES} bytes)")
