"""Shared request models — the payloads more than one route module consumes.

AddSeriesRequest is the add-anything payload: main's POST /api/series is the
primary consumer, and arcs.py both annotates with it (_add_arc) and CONSTRUCTS
one (populate_arc promotes a discovered CV arc into a real add). Living here
keeps main <-> arcs imports one-way.
"""
from pydantic import BaseModel


class AddSeriesRequest(BaseModel):
    locg_id: int | None = None
    cv_arc_id: int | None = None
    cv_volume_id: int | None = None   # the origin run, when followed via a storyline
    folder_path: str | None = None
    komga_id: str | None = None
    on_pull_list: bool = True
    # Metadata carried from the LOCG/ComicVine search result
    title: str | None = None
    publisher_name: str | None = None
    year_began: int | None = None
    # One-shot: LOCG search can hand back a book at the ISSUE level. When the client
    # picks one, it forwards the comic id + slug so the SERVER resolves it to its parent
    # series here — a comic id can never anchor a series, whatever the client computed.
    locg_comic_id: int | None = None
    locg_comic_slug: str | None = None
