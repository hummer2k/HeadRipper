#!/usr/bin/env python3
"""Headspace Manager: resolve names, download, tag and organize audio for Jellyfin.

Commands:
    resolve   map configured names to Headspace items
    download  download the selected audio into the cache
    build     tag and copy the cache into the Jellyfin layout
    covers    fetch and crop album art
    all       resolve + download + build + covers
    sync      mirror the library to a mounted path
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from mutagen.id3 import (
    COMM,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TXXX,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "headspace.toml"
TOKEN_FILE = SCRIPT_DIR / "Saved" / "BearerID.env"
API = "https://api.prod.headspace.com"
YEAR = str(date.today().year)

TOPICS = {
    "MEDITATE": [58, 60, 61],
    "SLEEP": [41, 42, 43, 44, 86, 89],
    "FOCUS": [62, 65, 175, 176, 177, 211],
}

SEARCH_TYPES = "animation,course,activity_group,obstacle,oneoff,sleepcast,playlist,video,meditation"

DURATION_RE = re.compile(r"(?:^|[-_])(\d{1,2})\s*(?:min|m)(?=[-_]|$)", re.I)
NARRATOR_RE = re.compile(r"-n\(([^)]+)\)", re.I)
LANGUAGE_RE = re.compile(r"-l\(([^)]+)\)", re.I)
DURATION_SUFFIX = re.compile(r" \(\d+ min\)$")

LOG_PATH: Path | None = None


class ManagerError(Exception):
    pass


class AuthError(ManagerError):
    pass


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    if LOG_PATH:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    value = value.replace(":", "")
    return " ".join(value.split()).casefold()


def sanitize(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.rstrip(" .") or "untitled"


def display_name(value: str) -> str:
    value = (value or "").strip()
    return value[:1].upper() + value[1:] if value else value


def load_token() -> str:
    if not TOKEN_FILE.exists():
        raise ManagerError(f"missing token file: {TOKEN_FILE}")
    text = TOKEN_FILE.read_text(encoding="utf-8").strip()
    token = text.split("=", 1)[1] if "=" in text else text
    token = token.strip().strip('"').strip("'")
    if not token:
        raise ManagerError(f"empty token in {TOKEN_FILE}")
    return token


class Api:
    def __init__(self, token: str):
        self.token = token
        self._cache: dict[tuple[str, str], object] = {}

    def _headers(self, lang: str) -> dict[str, str]:
        return {
            "hs-client-platform": "iOS",
            "hs-client-version": "301190000",
            "hs-languagepreference": lang,
            "Accept": "*/*",
            "Accept-Language": lang,
            "Origin": "https://my.headspace.com",
            "Referer": "https://my.headspace.com/",
            "Authorization": f"Bearer {self.token}",
        }

    def get(self, path: str, lang: str = "de-DE"):
        key = (path, lang)
        if key in self._cache:
            return self._cache[key]
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = requests.get(API + path, headers=self._headers(lang), timeout=60)
                if response.status_code == 401:
                    raise AuthError(
                        "401 Unauthorized: the token in Saved/BearerID.env is invalid or expired."
                    )
                response.raise_for_status()
                payload = response.json()
                self._cache[key] = payload
                return payload
            except AuthError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    raise ManagerError(f"GET {path} failed: {last_error}") from last_error
                time.sleep(2 * attempt)
        raise ManagerError(f"GET {path} failed")

    def get_bytes(self, path: str, lang: str = "de-DE") -> bytes:
        response = requests.get(API + path, headers=self._headers(lang), timeout=60)
        if response.status_code == 401:
            raise AuthError("401 Unauthorized: token invalid or expired")
        response.raise_for_status()
        return response.content

    def download(self, media_id: int | str, destination: Path, retries: int = 3) -> str:
        if destination.exists() and destination.stat().st_size > 0:
            return "skip"
        url = f"{API}/content-aggregation/v1/content/media-items/{media_id}/download?container=mp3"
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = requests.get(
                    url, headers=self._headers("de-DE"), timeout=60,
                    allow_redirects=False, stream=True,
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise ManagerError(f"redirect without Location for {media_id}")
                    response.close()
                    response = requests.get(location, timeout=120, stream=True)
                if response.status_code == 401:
                    raise AuthError("401 during download: token invalid or expired")
                response.raise_for_status()
                destination.parent.mkdir(parents=True, exist_ok=True)
                tmp = destination.with_name(destination.name + ".part")
                with tmp.open("wb") as handle:
                    for chunk in response.iter_content(1 << 16):
                        if chunk:
                            handle.write(chunk)
                response.close()
                tmp.rename(destination)
                return "ok"
            except AuthError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt == retries:
                    raise ManagerError(f"download {media_id} failed: {last_error}") from last_error
                time.sleep(2 * attempt)
        raise ManagerError(f"download {media_id} failed")


def probe(api: Api) -> None:
    api.get(
        "/content-aggregation/v1/content/view-models/library/topics-category-menu"
        "?location=MEDITATE&tag=&topicId=58",
        "de-DE",
    )


def load_config(path: Path) -> dict:
    if not path.exists():
        raise ManagerError(f"config not found: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    root = Path(os.path.expanduser(str(data.get("root", ""))))
    if not str(root):
        raise ManagerError("config: root is required")
    cache = Path(os.path.expanduser(str(data.get("cache", "Saved/Audio"))))
    if not cache.is_absolute():
        cache = SCRIPT_DIR / cache
    items = []
    for entry in data.get("items", []):
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        items.append({
            "name": name,
            "group_id": entry.get("group_id"),
            "english_name": entry.get("english_name"),
            "english_fallback": bool(entry.get("english_fallback", False)),
            "category": str(entry.get("category", "Meditation")),
        })
    return {
        "root": root,
        "cache": cache,
        "language": str(data.get("language", "de")).lower(),
        "narrator": str(data.get("narrator", "")).strip(),
        "durations": [int(value) for value in data.get("durations", [10, 15, 20])],
        "items": items,
        "sleepcasts_all": bool(data.get("sleepcasts_all", False)),
        "sleepcasts_date": data.get("sleepcasts_date"),
        "sync_target": data.get("sync_target"),
    }


def lang_tag(language: str) -> str:
    return "de-DE" if language.startswith("de") else "en-US"


def lang_code(language: str) -> str:
    return "de" if language.startswith("de") else "en"


def fetch_tiles(api: Api, language: str) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for location, topics in TOPICS.items():
        for topic_id in topics:
            path = (
                "/content-aggregation/v1/content/view-models/library/topics-category-menu"
                f"?location={location}&tag=&topicId={topic_id}"
            )
            try:
                payload = api.get(path, lang_tag(language))
            except ManagerError as exc:
                if "404" not in str(exc):
                    log(f"[warn] tiles {location}/{topic_id} ({language}): {exc}")
                continue
            for entry in payload.get("included", []):
                if entry.get("type") != "library-content-tile":
                    continue
                attrs = entry.get("attributes") or {}
                title = attrs.get("title")
                if not title:
                    continue
                key = normalize(title)
                if key in index:
                    continue
                index[key] = {
                    "title": title,
                    "content_id": attrs.get("contentId"),
                    "entity_id": attrs.get("entityId"),
                    "content_type": (attrs.get("contentType") or "").upper(),
                    "location": location,
                    "topic_id": topic_id,
                    "image_id": attrs.get("imageMediaId"),
                    "language": lang_code(language),
                }
    return index


def fetch_group_meta(api: Api, group_id: int, language: str) -> tuple[str | None, int | None]:
    payload = api.get(f"/content/activity-groups/{group_id}", lang_tag(language))
    attrs = (payload.get("data") or {}).get("attributes") or {}
    name = attrs.get("name")
    image_id = None
    for entry in payload.get("included", []):
        if entry.get("type") != "mediaItems":
            continue
        filename = ((entry.get("attributes") or {}).get("filename") or "").lower()
        if "header" in filename and filename.endswith((".png", ".jpg", ".jpeg")):
            image_id = entry.get("id")
            break
    return name, image_id


def search_group(api: Api, name: str, language: str) -> tuple[int | None, str | None]:
    path = (
        f"/search/v2/search?allowedContentTypes={SEARCH_TYPES}"
        f"&query={quote(name)}"
    )
    try:
        results = api.get(path, lang_tag(language))
    except ManagerError:
        return None, None
    for hit in results.get("results", [])[:10]:
        content_id = hit.get("contentId")
        if content_id is None:
            continue
        try:
            activity = api.get(f"/content/v1/activities/{content_id}", lang_tag(language))
        except ManagerError:
            continue
        group_id = activity.get("primaryActivityGroupId")
        if not group_id:
            continue
        group_name, _ = fetch_group_meta(api, group_id, language)
        if group_name and normalize(group_name) == normalize(name):
            return int(group_id), group_name
    return None, None


def parse_media(media_id: str, filename: str, activity: str | None, size: int | None) -> dict:
    duration = None
    match = DURATION_RE.search(filename)
    if match:
        duration = int(match.group(1))
    narrator = None
    match = NARRATOR_RE.search(filename)
    if match:
        narrator = match.group(1).strip()
    language = None
    match = LANGUAGE_RE.search(filename)
    if match:
        language = match.group(1).strip().lower()
    level = None
    session = None
    if activity:
        match = re.search(r"Übung (\d+) von Stufe (\d+)", activity)
        if match:
            session, level = int(match.group(1)), int(match.group(2))
        else:
            match = re.search(r"Session (\d+) of Level (\d+)", activity)
            if match:
                session, level = int(match.group(1)), int(match.group(2))
            else:
                match = re.search(r"Übung (\d+)", activity)
                if match:
                    session = int(match.group(1))
                else:
                    match = re.search(r"Session (\d+)", activity)
                    if match:
                        session = int(match.group(1))
    return {
        "media_id": media_id,
        "filename": filename,
        "duration": duration,
        "narrator": narrator,
        "language": language,
        "level": level,
        "session": session,
        "activity": activity,
        "size": size or 0,
    }


def fetch_group_tracks(api: Api, group_id: int, language: str) -> dict:
    payload = api.get(
        f"/content/activities?activityGroupIds={group_id}&limit=100&page=0",
        lang_tag(language),
    )
    activities = payload.get("data", [])
    variation_to_activity: dict[str, str] = {}
    for activity in activities:
        name = (activity.get("attributes") or {}).get("name") or ""
        refs = (activity.get("relationships") or {}).get("variations", {}).get("data") or []
        for ref in refs:
            variation_to_activity[str(ref.get("id"))] = name

    media_info: dict[str, dict] = {}
    for entry in payload.get("included", []):
        if entry.get("type") == "activityVariations":
            ref = (entry.get("relationships") or {}).get("mediaItem", {}).get("data")
            if not ref or ref.get("id") is None:
                continue
            media_info.setdefault(str(ref["id"]), {})["activity"] = variation_to_activity.get(
                str(entry.get("id"))
            )
        elif entry.get("type") == "mediaItems":
            attrs = entry.get("attributes") or {}
            media_info.setdefault(str(entry.get("id")), {}).update({
                "filename": attrs.get("filename"),
                "size": attrs.get("fileSizeInBytes") or 0,
                "duration_ms": attrs.get("durationInMs"),
            })

    tracks = []
    for media_id, info in media_info.items():
        filename = info.get("filename")
        if not filename:
            continue
        tracks.append(parse_media(media_id, filename, info.get("activity"), info.get("size")))

    return {
        "group_id": group_id,
        "activities": activities,
        "tracks": tracks,
        "kind": "course" if len(activities) > 1 else "single",
    }


def resolve_item(api: Api, item: dict, config: dict, index_de: dict, index_en: dict) -> dict:
    language = config["language"]
    result = {
        "request_name": item["name"],
        "group_id": None,
        "group_title": None,
        "image_id": None,
        "language": language,
        "status": "ok",
        "message": None,
        "source": None,
    }

    if item.get("group_id"):
        group_id = int(item["group_id"])
        group_name, image_id = fetch_group_meta(api, group_id, language)
        result.update({
            "group_id": group_id,
            "group_title": group_name or item["name"],
            "image_id": image_id,
            "source": "group_id",
        })
        return result

    index = index_de if language.startswith("de") else index_en
    tile = index.get(normalize(item["name"]))
    if tile:
        result.update({
            "group_id": tile["entity_id"],
            "group_title": tile["title"],
            "image_id": tile["image_id"],
            "language": tile["language"],
            "source": "tile",
        })
        return result

    if item.get("english_name"):
        tile_en = index_en.get(normalize(item["english_name"]))
        if tile_en:
            if item.get("english_fallback"):
                result.update({
                    "group_id": tile_en["entity_id"],
                    "group_title": tile_en["title"],
                    "image_id": tile_en["image_id"],
                    "language": "en",
                    "source": "english",
                })
                return result
            result.update({
                "status": "english_only",
                "message": f"only English: {item['english_name']}",
                "group_id": tile_en["entity_id"],
                "group_title": tile_en["title"],
                "source": "english",
            })
            return result

    group_id, group_name = search_group(api, item["name"], language)
    if group_id:
        _, image_id = fetch_group_meta(api, group_id, language)
        result.update({
            "group_id": group_id,
            "group_title": group_name or item["name"],
            "image_id": image_id,
            "source": "search",
        })
        return result

    result.update({"status": "missing", "message": "not found"})
    return result


def select_tracks(tracks: list[dict], kind: str, config: dict) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    wanted_language = lang_code(config["language"])
    candidates = [t for t in tracks if (t["language"] or wanted_language) == wanted_language]
    if not candidates:
        return [], [f"no {wanted_language} audio"]

    narrator = config.get("narrator") or ""
    if narrator:
        matching = [t for t in candidates if (t["narrator"] or "").casefold() == narrator.casefold()]
        if matching:
            candidates = matching
        else:
            available = sorted({(t["narrator"] or "?") for t in candidates})
            notes.append(f"narrator {narrator} not found, available: {', '.join(available)}")

    durations = set(config.get("durations") or [])
    if not durations:
        return candidates, notes

    in_range = [t for t in candidates if t["duration"] in durations]
    if kind == "course":
        if not in_range:
            return [], [f"no tracks in durations {sorted(durations)}"]
        return in_range, notes

    if in_range:
        return in_range, notes

    available = sorted({t["duration"] for t in candidates if t["duration"]})
    notes.append(f"no {sorted(durations)} min variant, using {available} min")
    return candidates, notes


def build_plan(api: Api, config: dict, indexes: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    plan: list[dict] = []
    reports: list[dict] = []
    for item in config["items"]:
        resolved = resolve_item(api, item, config, indexes["de"], indexes["en"])
        reports.append({
            "name": item["name"],
            "status": resolved["status"],
            "group_id": resolved["group_id"],
            "group_title": resolved["group_title"],
            "source": resolved["source"],
            "message": resolved["message"],
        })
        if resolved["status"] != "ok":
            continue
        try:
            group = fetch_group_tracks(api, resolved["group_id"], resolved["language"])
        except ManagerError as exc:
            reports[-1]["status"] = "error"
            reports[-1]["message"] = str(exc)
            continue

        kind = group["kind"]
        selected, notes = select_tracks(group["tracks"], kind, config)
        if notes:
            reports[-1]["message"] = "; ".join(notes)
        if not selected:
            reports[-1]["status"] = "no_audio"
            continue

        for track in selected:
            narrator = display_name(track["narrator"] or config.get("narrator") or "Headspace")
            category = item.get("category") or "Meditation"
            genre = "Sleep" if category.lower() == "sleep" else "Meditation"
            if kind == "course":
                album = f"{resolved['group_title']} ({track['duration']} min)"
                title = track["activity"] or f"Übung {track['session']}"
            else:
                album = resolved["group_title"]
                title = f"{resolved['group_title']} ({track['duration']} min)"
            plan.append({
                "request_name": item["name"],
                "group_id": resolved["group_id"],
                "group_title": resolved["group_title"],
                "kind": kind,
                "category": category,
                "genre": genre,
                "artist": narrator,
                "album": album,
                "title": title,
                "level": track["level"],
                "session": track["session"],
                "duration": track["duration"],
                "narrator": track["narrator"],
                "language": track["language"] or lang_code(resolved["language"]),
                "media_id": track["media_id"],
                "image_id": resolved["image_id"],
                "filename": track["filename"],
                "size": track["size"],
            })
    return plan, reports


def build_sleepcast_plan(api: Api, config: dict) -> tuple[list[dict], list[dict]]:
    if not config.get("sleepcasts_all"):
        return [], []
    payload = api.get(
        "/content-aggregation/v1/content/view-models/library/topics-category-menu"
        "?location=SLEEP&tag=&topicId=41",
        "en-US",
    )
    tiles = []
    seen_content: set[int] = set()
    for entry in payload.get("included", []):
        if entry.get("type") != "library-content-tile":
            continue
        attrs = entry.get("attributes") or {}
        if (attrs.get("contentType") or "").upper() != "SLEEPCAST":
            continue
        content_id = attrs.get("contentId")
        if content_id in seen_content:
            continue
        seen_content.add(content_id)
        tiles.append(attrs)

    target_date = config.get("sleepcasts_date") or date.today().isoformat()
    plan: list[dict] = []
    reports: list[dict] = []
    for tile in tiles:
        title = tile.get("title") or f"Sleepcast {tile.get('contentId')}"
        try:
            assets = api.get(
                "/content-interface/v3/playable-assets"
                f"?contentId={tile.get('contentId')}&date={target_date}"
                "&parentContentId=&audioDescriptionEnabled=false",
                "en-US",
            )
        except ManagerError as exc:
            reports.append({"name": title, "status": "error", "message": str(exc)})
            continue
        mixed = None
        for entry in assets or []:
            track_type = ((entry.get("metadata") or {}).get("trackType") or "").upper()
            if track_type == "MIXED":
                mixed = entry
                break
        if mixed is None and assets:
            mixed = assets[0]
        if not mixed:
            reports.append({"name": title, "status": "no_audio", "message": "no playable asset"})
            continue
        duration = (mixed.get("duration") or {}).get("durationInMins")
        plan.append({
            "request_name": title,
            "group_id": None,
            "group_title": title,
            "kind": "sleepcast",
            "category": "Sleepcasts",
            "genre": "Sleepcast",
            "artist": "Headspace",
            "album": title,
            "title": f"{title} ({duration} min)" if duration else title,
            "level": None,
            "session": None,
            "duration": duration,
            "narrator": "Headspace",
            "language": "en",
            "media_id": mixed.get("mediaItemId"),
            "image_id": tile.get("imageMediaId"),
            "filename": title,
            "size": 0,
        })
    reports.append({
        "name": f"Sleepcasts ({target_date})",
        "status": "ok",
        "group_id": None,
        "group_title": f"{len(plan)} sleepcasts",
        "source": "sleepcasts",
        "message": None,
    })
    return plan, reports


def cache_filename(entry: dict) -> str:
    if entry["kind"] == "sleepcast":
        return f"{sanitize(entry['title'])} - {entry['media_id']}.mp3"
    parts = [sanitize(entry["group_title"])]
    if entry["level"]:
        parts.append(f"L{entry['level']}")
    if entry["session"]:
        parts.append(f"S{entry['session']:02d}")
    if entry["duration"]:
        parts.append(f"{entry['duration']}min")
    if entry["narrator"]:
        parts.append(sanitize(entry["narrator"]))
    parts.append(str(entry["media_id"]))
    return " - ".join(parts) + ".mp3"


def download_plan(api: Api, plan: list[dict], config: dict) -> tuple[int, int, int]:
    downloaded = skipped = failed = 0
    manifest_entries: list[dict] = []
    manifest_path = config["cache"] / "cache_manifest.json"
    existing: list[dict] = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("entries", [])
        except Exception:
            existing = []
    known = {(e.get("media_id"), e.get("category")) for e in existing}

    for entry in plan:
        category = entry["category"]
        folder = config["cache"] / sanitize(category) / sanitize(entry["group_title"])
        destination = folder / cache_filename(entry)
        try:
            result = api.download(entry["media_id"], destination)
        except ManagerError as exc:
            failed += 1
            log(f"  [fail] {entry['title']}: {exc}")
            continue
        if result == "skip":
            skipped += 1
        else:
            downloaded += 1
            log(f"  [ok] {entry['group_title']} / {entry['title']}")
        record = dict(entry)
        record["source"] = str(destination)
        manifest_entries.append(record)
        known.add((entry["media_id"], category))

    merged = { (e.get("media_id"), e.get("category")): e for e in existing }
    for entry in manifest_entries:
        merged[(entry["media_id"], entry["category"])] = entry
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"entries": list(merged.values())}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return downloaded, skipped, failed


def write_tags(path: Path, entry: dict, track: int, disc: int | None, total_discs: int | None) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=entry["title"]))
    tags.add(TPE1(encoding=3, text=entry["artist"]))
    tags.add(TPE2(encoding=3, text=entry["artist"]))
    tags.add(TALB(encoding=3, text=entry["album"]))
    tags.add(TRCK(encoding=3, text=f"{track:02d}"))
    if disc is not None:
        tags.add(TPOS(encoding=3, text=f"{disc}/{total_discs or disc}"))
    tags.add(TCON(encoding=3, text=entry["genre"]))
    tags.add(TDRC(encoding=3, text=YEAR))
    tags.add(TPUB(encoding=3, text="Headspace"))
    tags.add(COMM(
        encoding=3, lang="eng", desc="source",
        text=(
            f"Headspace | mediaId={entry['media_id']} | narrator={entry['narrator']} "
            f"| duration={entry['duration']}min"
        ),
    ))
    tags.add(TXXX(encoding=3, desc="HEADSPACE_MEDIA_ID", text=str(entry["media_id"])))
    tags.save(path, v2_version=4)


def build_library(config: dict, *, dry_run: bool, move: bool) -> tuple[int, int, int]:
    manifest_path = config["cache"] / "cache_manifest.json"
    if not manifest_path.exists():
        raise ManagerError(
            f"no cache manifest at {manifest_path}; run 'download' first"
        )
    entries = json.loads(manifest_path.read_text(encoding="utf-8")).get("entries", [])
    if not entries:
        raise ManagerError("cache manifest is empty; run 'download' first")

    albums: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    missing = 0
    for entry in entries:
        source = Path(entry.get("source", ""))
        if not source.exists():
            missing += 1
            continue
        albums[(entry["category"], entry["artist"], entry["album"])].append(entry)
    if missing:
        log(f"[build] {missing} cache files already gone (built before?)")

    moved = skipped = failed = 0
    for (category, artist, album), album_entries in sorted(albums.items()):
        levels = sorted({int(entry["level"] or 0) for entry in album_entries})
        multi_disc = len(levels) > 1 and levels != [0]
        max_level = max(levels) if multi_disc else 0
        for level in levels:
            group = [entry for entry in album_entries if int(entry["level"] or 0) == level]
            group.sort(key=lambda entry: (entry.get("session") or 0, entry.get("media_id") or 0))
            for index, entry in enumerate(group, 1):
                source = Path(entry["source"])
                folder = config["root"] / sanitize(category) / sanitize(artist) / sanitize(album)
                if multi_disc:
                    folder = folder / f"CD{level} - Stufe {level}"
                destination = folder / f"{index:02d} - {sanitize(entry['title'])}.mp3"
                if dry_run:
                    log(f"  [plan] {destination.relative_to(config['root'])}")
                    continue
                if destination.exists():
                    skipped += 1
                    continue
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    write_tags(
                        source, entry, index,
                        level if multi_disc else None,
                        max_level if multi_disc else None,
                    )
                    if move:
                        shutil.move(str(source), str(destination))
                    else:
                        shutil.copy2(source, destination)
                    moved += 1
                    log(f"  [ok] {destination.relative_to(config['root'])}")
                except Exception as exc:
                    failed += 1
                    log(f"  [fail] {entry['title']}: {exc}")
    if not dry_run and move:
        remaining = [
            entry for entry in entries
            if Path(entry.get("source", "")).exists()
        ]
        manifest_path.write_text(
            json.dumps({"entries": remaining}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return moved, skipped, failed


def render_cover(api: Api, image_id: int, destination: Path) -> None:
    import tempfile

    payload = api.get_bytes(f"/content-aggregation/v1/content/media-items/{image_id}/download")
    if not shutil.which("magick"):
        raise ManagerError("ImageMagick (magick) not found")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        raw = tmp_dir / "image"
        raw.write_bytes(payload)
        source = raw
        if payload.startswith(b"%PDF"):
            if not shutil.which("pdftoppm"):
                raise ManagerError("pdftoppm not found")
            source = tmp_dir / "page"
            subprocess.run(
                ["pdftoppm", "-png", "-r", "200", "-f", "1", "-l", "1", str(raw), str(source)],
                check=True, capture_output=True,
            )
            pages = sorted(tmp_dir.glob("page*.png"))
            if not pages:
                raise ManagerError("pdftoppm produced no page")
            source = pages[0]
        subprocess.run(
            [
                "magick", str(source), "-resize", "1000x1000^", "-gravity", "center",
                "-extent", "1000x1000", "-quality", "90", str(destination),
            ],
            check=True, capture_output=True,
        )


def build_image_map(api: Api, config: dict) -> dict[str, int]:
    index_de = fetch_tiles(api, "de-DE")
    index_en = fetch_tiles(api, "en-US")
    image_map: dict[str, int] = {}
    for index in (index_de, index_en):
        for tile in index.values():
            if tile.get("image_id"):
                image_map[normalize(tile["title"])] = int(tile["image_id"])
    for item in config["items"]:
        if item.get("group_id") and item.get("name"):
            try:
                _, image_id = fetch_group_meta(api, int(item["group_id"]), config["language"])
            except ManagerError:
                continue
            if image_id:
                image_map[normalize(item["name"])] = int(image_id)
    return image_map


def covers_command(api: Api, config: dict, *, dry_run: bool) -> tuple[int, int, int]:
    if not config["root"].exists():
        log(f"[covers] root does not exist: {config['root']}")
        return 0, 0, 0
    image_map = build_image_map(api, config)
    manifest_path = config["cache"] / "cache_manifest.json"
    if manifest_path.exists():
        entries = json.loads(manifest_path.read_text(encoding="utf-8")).get("entries", [])
        for entry in entries:
            if entry.get("image_id"):
                image_map[normalize(entry["album"])] = int(entry["image_id"])

    albums = []
    for path in config["root"].rglob("*"):
        if not path.is_dir():
            continue
        if re.match(r"^CD\d+ - Stufe \d+$", path.name):
            continue
        direct_mp3 = any(path.glob("*.mp3"))
        cd_children = any(
            child.is_dir()
            and re.match(r"^CD\d+ - Stufe \d+$", child.name)
            and any(child.glob("*.mp3"))
            for child in path.iterdir()
        )
        if direct_mp3 or cd_children:
            albums.append(path)
    done = skipped = failed = 0
    for album in sorted(albums):
        cover = album / "cover.jpg"
        if cover.exists() and cover.stat().st_size > 0:
            skipped += 1
            continue
        name = album.name
        key = DURATION_SUFFIX.sub("", name)
        image_id = image_map.get(normalize(key)) or image_map.get(normalize(name))
        if not image_id:
            failed += 1
            log(f"  [warn] no cover image for {album.relative_to(config['root'])}")
            continue
        if dry_run:
            log(f"  [plan] cover {album.relative_to(config['root'])} <- {image_id}")
            continue
        try:
            render_cover(api, image_id, cover)
            done += 1
            log(f"  [ok] cover {album.relative_to(config['root'])}")
        except Exception as exc:
            failed += 1
            log(f"  [fail] cover {album.relative_to(config['root'])}: {exc}")
    return done, skipped, failed


def sync_command(config: dict, target: Path, *, delete: bool, dry_run: bool) -> int:
    if not shutil.which("rsync"):
        raise ManagerError("rsync not found")
    if not target.exists():
        raise ManagerError(f"sync target does not exist: {target}")
    if target.resolve() == config["root"].resolve():
        raise ManagerError("sync target must differ from the library root")
    command = ["rsync", "-av"]
    if delete:
        command.append("--delete")
    if dry_run:
        command.append("-n")
    command.extend([f"{config['root']}/", f"{target}/"])
    log(f"[sync] {' '.join(command)}")
    result = subprocess.run(command)
    return result.returncode


def print_reports(reports: list[dict]) -> None:
    for report in reports:
        status = report.get("status", "?")
        name = report.get("name", "?")
        message = report.get("message")
        extra = f" ({message})" if message else ""
        log(f"  [{status}] {name}{extra}")


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", action="append", default=[], help="Ad-hoc item name (repeatable)")
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)


def main() -> int:
    global LOG_PATH
    parser = argparse.ArgumentParser(description="Headspace Manager")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--root", help="Override the library root")
    parser.add_argument("--cache", help="Override the download cache")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", help="Write a log file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("resolve", "download", "build", "covers", "all"):
        sub = subparsers.add_parser(command)
        add_common(sub)
        if command in ("build",):
            sub.add_argument("--move", action="store_true", help="Move from cache instead of copy")
        if command in ("download", "all"):
            sub.add_argument("--skip-sleepcasts", action="store_true")
        if command == "all":
            sub.add_argument("--move", action="store_true")

    sync = subparsers.add_parser("sync")
    sync.add_argument("--target", help="Mounted destination path")
    sync.add_argument("--no-delete", action="store_true", help="Do not delete extra files")
    sync.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)

    args = parser.parse_args()
    if args.log:
        LOG_PATH = Path(os.path.expanduser(args.log))

    try:
        config = load_config(Path(args.config).expanduser())
        if args.root:
            config["root"] = Path(os.path.expanduser(args.root))
        if args.cache:
            cache = Path(os.path.expanduser(args.cache))
            config["cache"] = cache if cache.is_absolute() else SCRIPT_DIR / cache
        if args.command in ("resolve", "download", "build", "covers", "all"):
            names = getattr(args, "name", []) or []
            if names:
                config["items"] = [
                    {
                        "name": name,
                        "group_id": None,
                        "english_name": None,
                        "english_fallback": False,
                        "category": "Meditation",
                    }
                    for name in names
                ]
        token = load_token()
        api = Api(token)
        probe(api)
        log(f"[auth] token OK ({TOKEN_FILE})")
    except AuthError as exc:
        log(f"[auth] {exc}")
        return 2
    except ManagerError as exc:
        log(f"[error] {exc}")
        return 2

    try:
        if args.command == "resolve":
            indexes = {"de": fetch_tiles(api, "de-DE"), "en": fetch_tiles(api, "en-US")}
            plan, reports = build_plan(api, config, indexes)
            if config.get("sleepcasts_all"):
                sleep_plan, sleep_reports = build_sleepcast_plan(api, config)
                plan += sleep_plan
                reports += sleep_reports
            log(f"[resolve] {len(plan)} downloadable tracks")
            print_reports(reports)
            return 0

        if args.command == "download":
            indexes = {"de": fetch_tiles(api, "de-DE"), "en": fetch_tiles(api, "en-US")}
            plan, reports = build_plan(api, config, indexes)
            if config.get("sleepcasts_all") and not getattr(args, "skip_sleepcasts", False):
                sleep_plan, sleep_reports = build_sleepcast_plan(api, config)
                plan += sleep_plan
                reports += sleep_reports
            print_reports(reports)
            if args.dry_run:
                total = sum(entry.get("size") or 0 for entry in plan)
                log(f"[download] dry run: {len(plan)} tracks, {total / 1e9:.2f} GB")
                return 0
            downloaded, skipped, failed = download_plan(api, plan, config)
            log(f"[download] downloaded={downloaded} skipped={skipped} failed={failed}")
            return 1 if failed else 0

        if args.command == "build":
            moved, skipped, failed = build_library(
                config, dry_run=args.dry_run, move=getattr(args, "move", False)
            )
            log(f"[build] built={moved} skipped={skipped} failed={failed}")
            return 1 if failed else 0

        if args.command == "covers":
            done, skipped, failed = covers_command(api, config, dry_run=args.dry_run)
            log(f"[covers] created={done} skipped={skipped} failed={failed}")
            return 1 if failed else 0

        if args.command == "all":
            indexes = {"de": fetch_tiles(api, "de-DE"), "en": fetch_tiles(api, "en-US")}
            plan, reports = build_plan(api, config, indexes)
            if config.get("sleepcasts_all") and not getattr(args, "skip_sleepcasts", False):
                sleep_plan, sleep_reports = build_sleepcast_plan(api, config)
                plan += sleep_plan
                reports += sleep_reports
            print_reports(reports)
            if not args.dry_run:
                downloaded, skipped, failed = download_plan(api, plan, config)
                log(f"[download] downloaded={downloaded} skipped={skipped} failed={failed}")
                moved, build_skipped, build_failed = build_library(
                    config, dry_run=False, move=getattr(args, "move", False)
                )
                log(f"[build] built={moved} skipped={build_skipped} failed={build_failed}")
                cover_done, cover_skipped, cover_failed = covers_command(api, config, dry_run=False)
                log(f"[covers] created={cover_done} skipped={cover_skipped} failed={cover_failed}")
                return 1 if failed or build_failed or cover_failed else 0
            total = sum(entry.get("size") or 0 for entry in plan)
            log(f"[all] dry run: {len(plan)} tracks, {total / 1e9:.2f} GB")
            return 0

        if args.command == "sync":
            target = Path(os.path.expanduser(args.target or config.get("sync_target") or ""))
            if not str(target):
                raise ManagerError("sync: provide --target or set sync_target in the config")
            return sync_command(config, target, delete=not args.no_delete, dry_run=args.dry_run)

    except AuthError as exc:
        log(f"[auth] {exc}")
        return 2
    except ManagerError as exc:
        log(f"[error] {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
