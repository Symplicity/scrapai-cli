"""Custom Scrapy signals for scrapai.

Signals are sentinel objects used as event identifiers. Extensions and
pipelines connect handlers to these signals via crawler.signals.connect().
"""

# Fired by DatabasePipeline after a successful DB commit.
# Kwargs: items (list of dicts that were actually persisted)
items_committed = object()
