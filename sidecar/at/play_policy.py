from __future__ import annotations


def drive_play_type(play_id: str, *, server_proxy: bool = False) -> str:
    # Logical episodes must resolve again through AT to retain refresh/source switching.
    return "t4" if server_proxy or play_id.startswith("msubep-") else "t4-direct"
