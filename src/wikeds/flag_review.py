"""Flags tab: look through saved anomaly flags, label them, see how often each detector was right."""
import inspect
import time
from urllib.parse import quote

import pandas as pd
import streamlit as st

from wikeds import config, flags
from wikeds.summary_store import open_store

KIND_NAMES = {"burst": "News burst", "vandalism": "Vandalism", "spike": "Rate spike"}

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


def _save_labels(key):
    """on_change for the table. Maps edited row numbers back to flag ids using the
    list we showed last time, so new flags arriving in between can't shift a label
    onto the wrong row."""
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
    st.caption("real = labeled real news or vandalism. Unlabeled and unsure flags are left out of precision.")

    view = df[df["kind"].isin(kinds)]
    if only_open:
        view = view[view["label"].isna()]
    view = view.reset_index(drop=True)
    st.session_state["flag_shown"] = view["flag_id"].tolist()
    if view.empty:
        st.caption("No flags match these filters.")
        return

    st.subheader("Flags")
    st.caption("Pick a label in the last column to save it. One row per episode: `peak z` and "
               "`edits` are the highest values seen while the flag lasted.")
    show = pd.DataFrame({
        "type": view["kind"].map(KIND_NAMES),
        "wiki": view["wiki"],
        "page": view["title"],
        "editor": view["editor"],
        "why": view["reason"],
        "peak z": view["peak_z"].round(1),
        "edits": view["edits"],
        "bytes": view["bytes_changed"],
        "first seen": _local(view["first_seen"]),
        "last seen": _local(view["last_seen"]),
        "link": [_link(s, t) for s, t in zip(view["server"], view["title"])],
        "label": view["label"],
    })
    key = f"flag_editor_{st.session_state.get('flag_gen', 0)}"
    st.data_editor(
        show, hide_index=True, key=key, on_change=_save_labels, args=(key,),
        disabled=[c for c in show.columns if c != "label"],
        column_config={
            "label": st.column_config.SelectboxColumn("label", options=flags.LABELS),
            "link": st.column_config.LinkColumn("link", display_text="open"),
        },
        **_ED_WIDTH,
    )
    st.download_button("Download CSV", df.to_csv(index=False).encode(), "anomaly_flags.csv",
                       "text/csv", key="dl_flags")