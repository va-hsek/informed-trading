"""Run on your machine: tests every configured news feed and prints OK/FAIL with the reason.

    python3 -m news.verify_feeds
"""
from __future__ import annotations

import yaml

from collectors.http import FetchError, get
from news.feeds import feed_url, parse_feed
from pipeline.run_action import ROOT


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "news_sources.yaml").read_text())
    for src in cfg["feeds"]:
        try:
            items = parse_feed(get(feed_url(src)), src)
            print(f"OK    {src['name']:<32} {len(items):>3} items, newest: {items[0].title[:60]!r}")
        except FetchError as e:
            print(f"FAIL  {src['name']:<32} {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
