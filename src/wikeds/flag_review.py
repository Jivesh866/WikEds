"""Flags tab: look through saved anomaly flags, label them, see how often each detector was right.

Everyone can read this tab. Changing labels needs the LABEL_PASSWORD (see config).
"""
import hmac
import inspect
import time
from urllib.parse import quote

import pandas as pd
import streamlit as st

from wikeds import config, flags
from wikeds.summary_store import open_store

KIND_NAMES = {"burst": "News burst", "vandalism": "Vandalism", "spike": "Rate spike"}
MAX_TRIES = 5          # wrong passwords before a pause
PAUSE_SECONDS = 60

# newer streamlit wants width="stretch", older ones use_container_width
_DF_WIDTH = ({"width": "stretch"} if "width" in inspect.signature(st.dataframe).parameters
             else {"use_container_width": True})
_ED_WIDTH = ({"width": "stretch"} if "width" in inspect.signature(st.data_editor).parameters
             else {"use_container_width": True})


def _link(server, title):
    if not server or not title:
        return None
    return f"https://{server}/wiki/{quote(str(title).replace(' ', '_'))}"


def _local(unix):
    return (pd.to_datetime(unix, unit="s", utc=True)
            .dt.tz_convert(config.TIMEZONE).dt.strftime("%d %b %H:%M"))


# ---------------------------------------------------------------- label password
def password_ok(entered):
    """Constant-time check against LABEL_PASSWORD. False when none is set."""
    if not config.LABEL_PASSWORD:
        return False
    return hmac.compare_digest(str(entered).encode(), config.LABEL_PASSWORD.encode())


def can_label():
    """No password set: open locally, locked on cloud. Password set: only after unlocking."""
    if config.LABEL_PASSWORD:
        return bool(st.session_state.get("labels_unlocked"))
    return not config.IS_CLOUD


def _rerun():
    try:
        st.rerun(scope="fragment")
    except (TypeError, st.errors.StreamlitAPIException):
        st.rerun()


def _access_box():
    if not config.LABEL_PASSWORD:
        if config.IS_CLOUD:
            st.info("Labels are read-only: set LABEL_PASSWORD in the app secrets to be able to edit them. "
                    "Everything else here is open.")
        return
    if st.session_state.get("labels_unlocked"):
        c1, c2 = st.columns([5, 1])
        c1.caption("Label editing is unlocked for this session.")
        if c2.button("Lock", key="lock_labels"):
            st.session_state["labels_unlocked"] = False
            _rerun()
        return
    with st.form("unlock_labels"):
        entered = st.text_input("Password to edit labels", type="password")
        go = st.form_submit_button("Unlock")
    st.caption("You can read everything here without it.")
    if not go:
        return
    wait = st.session_state.get("unlock_pause_until", 0) - time.time()
    if wait > 0:
        st.error(f"Too many wrong passwords. Try again in {int(wait) + 1} s.")
    elif password_ok(entered):
        st.session_state["labels_unlocked"] = True
        st.session_state["unlock_fails"] = 0
        _rerun()
    else:
        fails = st.session_state.get("unlock_fails", 0) + 1
        st.session_state["unlock_fails"] = fails
        if fails >= MAX_TRIES:
            st.session_state["unlock_pause_until"] = time.time() + PAUSE_SECONDS
            st.session_state["unlock_fails"] = 0
        time.sleep(1)    # slows down guessing
        st.error("Wrong password.")


def _save_labels(key):
    """on_change for the table. Maps edited row numbers back to flag ids using the
    list we showed last time, so new flags arriving in between can't shift a label
    onto the wrong row. Checks the password again here, not just in the page."""
    if not can_label():
        st.session_state["flag_note"] = "Labels are locked."
        return
    edits = st.session_state.get(key, {}).get("edited_rows", {})
    shown = st.session_state.get("flag_shown", [])
    changes = {shown[int(i)]: v["label"] for i, v in edits.items()
               if "label" in v and int(i) < len(shown)}
    if not changes:
        return
    try:
        store = open_store()
        try:
            for fid, label in changes.items():
                flags.set_label(store, fid, label)
        finally:
            store.close()
        st.session_state["flag_gen"] = st.session_state.get("flag_gen", 0) + 1   # fresh editor state
        st.session_state["flag_note"] = f"Saved {len(changes)} label(s)."
    except Exception as e:
        st.session_state["flag_note"] = f"Could not save labels: {e}"


def flags_panel():
    note = st.session_state.pop("flag_note", None)
    if note:
        st.info(note)

    c1, c2 = st.columns([1, 2])
    days = c1.radio("Range", [1, 7, 30, 90], index=1, horizontal=True, key="flag_days",
                    format_func=lambda d: "Last 24 hours" if d == 1 else f"Last {d} days")
    kinds = c2.multiselect("Detectors", list(KIND_NAMES), default=list(KIND_NAMES),
                           format_func=KIND_NAMES.get, key="flag_kinds")
    only_open = st.checkbox("Only flags without a label", key="flag_unlabeled")

    try:
        store = open_store()
        try:
            df = flags.read(store, int(time.time()) - days * 86400)
        finally:
            store.close()
    except Exception as e:
        st.warning(f"Could not read flags: {e}")
        return
    if df.empty:
        st.info("No flags saved in this range yet. The listener checks for anomalies every "
                f"{config.DETECT_EVERY_SECONDS} s once it has about 30 minutes of data.")
        return

    st.subheader("How often were the flags right?")
    st.dataframe(flags.precision(df, KIND_NAMES), hide_index=True, **_DF_WIDTH)
    st.caption("real = labeled real news or vandalism. Unlabeled and unsure flags are left out of precision. "
               "reverted = vandalism flags whose page was reverted afterwards.")

    view = df[df["kind"].isin(kinds)]
    if only_open:
        view = view[view["label"].isna()]
    view = view.reset_index(drop=True)
    st.session_state["flag_shown"] = view["flag_id"].tolist()
    if view.empty:
        st.caption("No flags match these filters.")
        return

    st.subheader("Flags")
    _access_box()
    editable = can_label()
    st.caption("One row per episode: `peak z` and `edits` are the highest values seen while the flag lasted. "
               "`reverted` means a later edit to the page undid it, a strong sign of vandalism. "
               + ("Pick a label in the last column to save it." if editable else "Labels are read-only."))
    show = pd.DataFrame({
        "type": view["kind"].map(KIND_NAMES),
        "wiki": view["wiki"],
        "page": view["title"],
        "editor": view["editor"],
        "why": view["reason"],
        "summary": view["comment"].fillna("").astype(str).str.slice(0, 120),
        "reverted": view["reverted"].map({1: "yes"}).fillna(""),
        "peak z": view["peak_z"].round(1),
        "edits": view["edits"],
        "bytes": view["bytes_changed"],
        "first seen": _local(view["first_seen"]),
        "last seen": _local(view["last_seen"]),
        "link": [_link(s, t) for s, t in zip(view["server"], view["title"])],
        "diff": [flags.diff_url(s, n, o) for s, n, o in zip(view["server"], view["rev_new"], view["rev_old"])],
        "label": view["label"],
    })
    key = f"flag_editor_{st.session_state.get('flag_gen', 0)}"
    st.data_editor(
        show, hide_index=True, key=key, on_change=_save_labels, args=(key,),
        disabled=[c for c in show.columns if c != "label" or not editable],
        column_config={
            "label": st.column_config.SelectboxColumn("label", options=flags.LABELS),
            "link": st.column_config.LinkColumn("page link", display_text="open"),
            "diff": st.column_config.LinkColumn("diff", display_text="diff"),
        },
        **_ED_WIDTH,
    )
    st.download_button("Download CSV", df.to_csv(index=False).encode(), "anomaly_flags.csv",
                       "text/csv", key="dl_flags")