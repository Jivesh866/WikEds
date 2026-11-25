import pandas as pd

from wikeds import anomaly

NOW = 1_800_000_030            # 30s into a minute
END = (NOW // 60) * 60
COLS = ["event_time", "wiki", "type", "title", "user", "bot", "old_len", "new_len", "namespace"]


def ev(t, wiki="enwiki", title="A", user="u", bot=0, old=100, new=110, ns=0, typ="edit"):
    return (t, wiki, typ, title, user, bot, old, new, ns)


def frame(rows):
    return pd.DataFrame(rows, columns=COLS)


def steady(wiki, start, end, pattern=(1, 2, 3)):
    rows = []
    for i, m in enumerate(range(start, end, 60)):
        for j in range(pattern[i % 3]):
            rows.append(ev(m + j, wiki=wiki, title=f"p{i}-{j}", user=f"u{i}-{j}"))
    return rows


# ---------------------------------------------------------------- rate spikes
def test_rate_spike_flags_only_the_wiki_that_jumped():
    start = END - 125 * 60
    rows = steady("enwiki", start, END - 300) + steady("dewiki", start, END)
    for m in range(END - 300, END, 60):                      # enwiki goes to 20/min
        rows += [ev(m + j, wiki="enwiki", title=f"s{m}-{j}", user=f"s{j}") for j in range(20)]
    out = anomaly.rate_spikes(frame(rows), NOW)
    assert list(out["wiki"]) == ["enwiki"]
    assert out.iloc[0]["z"] > 10 and out.iloc[0]["edits_recent"] == 100


def test_rate_spike_needs_enough_baseline():
    rows = [ev(END - 600 + i, title=f"t{i}") for i in range(600)]   # only 10 minutes of data
    assert anomaly.rate_spikes(frame(rows), NOW).empty


def test_rate_spike_ignores_bots():
    start = END - 125 * 60
    rows = steady("enwiki", start, END - 300)
    for m in range(END - 300, END, 60):
        rows += [ev(m + j, bot=1, title=f"b{m}-{j}") for j in range(20)]
    assert anomaly.rate_spikes(frame(rows), NOW).empty


# ---------------------------------------------------------------- page bursts
def _burst_frame():
    rows = [ev(NOW - 7200, title="Filler", user="x")]        # gives the baseline some history
    rows += [ev(NOW - 600 + i * 10, title="Breaking", user=f"u{i % 6}") for i in range(10)]
    rows += [ev(NOW - 300, title="Quiet", user="q")]
    rows += [ev(NOW - 600 + i * 10, title="War", user="same") for i in range(10)]
    rows += [ev(NOW - 600 + i * 10, title="Talk page", user=f"t{i % 5}", ns=1) for i in range(10)]
    return frame(rows)


def test_page_burst_finds_the_news_page():
    out = anomaly.page_bursts(_burst_frame(), NOW)
    assert list(out["title"]) == ["Breaking"]                # War: 1 editor, Quiet: 1 edit, Talk: ns 1
    assert out.iloc[0]["editors"] == 6 and out.iloc[0]["z"] >= 5


def test_page_burst_can_include_other_namespaces():
    out = anomaly.page_bursts(_burst_frame(), NOW, articles_only=False)
    assert set(out["title"]) == {"Breaking", "Talk page"}


def test_page_burst_empty_without_baseline():
    rows = [ev(NOW - 300 + i, title="Breaking", user=f"u{i}") for i in range(10)]
    assert anomaly.page_bursts(frame(rows), NOW).empty


# ---------------------------------------------------------------- suspicious edits
def test_suspicious_edits_flags_blanking_removal_and_anon_spam():
    rows = [ev(NOW - 10 * i, title=f"n{i}", user=f"u{i}", old=1000, new=1000 + (i % 5 - 2) * 10)
            for i in range(60)]
    rows += [
        ev(NOW - 5, title="Big", user="vandal", old=5000, new=0),
        ev(NOW - 6, title="Cut", user="editor", old=8000, new=3000),
        ev(NOW - 7, title="Spam", user="203.0.113.7", old=500, new=9000),
        ev(NOW - 8, title="BotCut", user="bot", bot=1, old=9000, new=10),
    ]
    out = anomaly.suspicious_edits(frame(rows), NOW).set_index("title")
    assert set(out.index) == {"Big", "Cut", "Spam"}
    assert out.loc["Big", "reason"] == "page blanked"
    assert out.loc["Cut", "reason"] == "large removal"
    assert out.loc["Spam", "reason"] == "large addition by anonymous" and out.loc["Spam", "anon"]


def test_big_addition_by_a_named_editor_is_not_flagged():
    rows = [ev(NOW - 10 * i, title=f"n{i}", old=1000, new=1000 + (i % 5 - 2) * 10) for i in range(60)]
    rows.append(ev(NOW - 5, title="Big article", user="Alice", old=500, new=9000))
    assert anomaly.suspicious_edits(frame(rows), NOW).empty


def test_is_anon():
    users = pd.Series(["192.168.0.1", "2001:db8::1", "Alice", None, "Foo:bar"])
    assert list(anomaly.is_anon(users)) == [True, True, False, False, False]