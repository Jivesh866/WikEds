import pandas as pd

from wikeds import anomaly, config, detector

NOW = 1_800_000_030
COLS = ["event_time", "wiki", "server_name", "type", "title", "user", "bot", "old_len", "new_len", "namespace"]


def ev(i, wiki="enwiki", title="n", user="u", old=1000, new=1000, ns=0):
    return (NOW - 10 * i, wiki, "x.org", "edit", f"{title}{i}", f"{user}{i}", 0, old, new, ns)


def frame():
    rows = [ev(i, old=1000, new=1000 + (i % 5 - 2) * 10) for i in range(1, 61)]
    rows += [
        (NOW - 3, "enwiki", "en.wikipedia.org", "edit", "Article", "vandal", 0, 5000, 0, 0),
        (NOW - 4, "enwiki", "en.wikipedia.org", "edit", "User:Someone", "Someone", 0, 5000, 0, 2),
        (NOW - 5, "enwiki", "en.wikipedia.org", "edit", "Wikipedia:Archive", "Admin", 0, 9000, 0, 4),
        (NOW - 6, "wikidatawiki", "www.wikidata.org", "edit", "Q1", "Merger", 0, 5000, 0, 0),
    ]
    return pd.DataFrame(rows, columns=COLS)


def titles(df):
    return set(df["title"])


def test_default_keeps_old_behaviour():
    assert titles(anomaly.suspicious_edits(frame(), NOW)) == {"Article", "User:Someone", "Wikipedia:Archive", "Q1"}


def test_articles_only_and_skip_wikis():
    out = anomaly.suspicious_edits(frame(), NOW, articles_only=True, skip_wikis=("wikidatawiki",))
    assert titles(out) == {"Article"}
    assert titles(anomaly.suspicious_edits(frame(), NOW, articles_only=True)) == {"Article", "Q1"}


def test_detector_uses_the_config_scope(monkeypatch):
    found = detector.collect(frame(), NOW)
    assert {f["title"] for f in found if f["kind"] == "vandalism"} == {"Article"}   # defaults: on
    monkeypatch.setattr(config, "VANDALISM_ARTICLES_ONLY", False)
    monkeypatch.setattr(config, "VANDALISM_SKIP_WIKIS", ())
    found = detector.collect(frame(), NOW)
    assert len({f["title"] for f in found if f["kind"] == "vandalism"}) == 4