"""Write a rekordbox.xml library containing the Mirlos request playlists.

rekordbox reads it from Preferences → Advanced → Database → rekordbox xml →
Imported Library. Dragging a track from the XML pane onto a deck or into the
collection imports it, and Auto Analysis analyzes it on import.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from .store import Request

PLAYLIST_NEW = "MIRLOS · NEW"
PLAYLIST_TOP = "MIRLOS · TOP"


def location(path: str | Path) -> str:
    """rekordbox-style file URL, e.g. file://localhost/C:/Music/a%20b.m4a"""
    posix = Path(path).resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix  # Windows drive path
    return "file://localhost" + quote(posix, safe="/:")


def build(new: list[Request], top: list[Request]) -> ET.ElementTree:
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="Mirlos", Version="0.1.0", Company="gitgiant")
    tracks = {r.id: r for r in new + top}
    coll = ET.SubElement(root, "COLLECTION", Entries=str(len(tracks)))
    for r in tracks.values():
        ET.SubElement(coll, "TRACK", {
            "TrackID": str(r.id),
            "Name": r.title,
            "Artist": r.uploader,
            "Kind": "M4A File",
            "TotalTime": str(r.duration or 0),
            "Comments": f"Requested by {r.guest_name} · {r.votes} vote(s) · youtu.be/{r.video_id}",
            "Location": location(r.path or ""),
        })
    pls = ET.SubElement(root, "PLAYLISTS")
    node = ET.SubElement(pls, "NODE", Type="0", Name="ROOT", Count="2")
    for name, items in ((PLAYLIST_NEW, new), (PLAYLIST_TOP, top)):
        pl = ET.SubElement(node, "NODE", Name=name, Type="1", KeyType="0", Entries=str(len(items)))
        for r in items:
            ET.SubElement(pl, "TRACK", Key=str(r.id))
    ET.indent(root)
    return ET.ElementTree(root)


def write(path: Path, new: list[Request], top: list[Request]) -> None:
    tmp = path.with_suffix(".xml.tmp")
    build(new, top).write(tmp, encoding="UTF-8", xml_declaration=True)
    os.replace(tmp, path)
