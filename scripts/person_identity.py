"""Resolve person profiles from explicit name/URL evidence, never slug guesses."""
import copy
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit


def name_key(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return re.sub(
        r"\s*[•·]\s*(?:[123]-й\+?|[123](?:st|nd|rd)\+?|отслеживаете|following)\s*$",
        "", text,
    ).strip()


def profile_url(value):
    try:
        url = urlsplit(str(value or "").strip())
        if url.scheme not in {"http", "https"} or url.hostname not in {"linkedin.com", "www.linkedin.com"}:
            return None
        match = re.match(r"^/in/([^/]+)(?:/|$)", url.path)
        if match:
            return f"https://www.linkedin.com/in/{match.group(1)}/"
    except ValueError:
        pass
    return None


def profile_key(value):
    url = profile_url(value)
    return unquote(urlsplit(url).path.split("/in/", 1)[1].strip("/")).casefold() if url else ""


def feed_profile_index(path):
    """Match parser rows using the analyzer's existing deterministic signal ID."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = data if isinstance(data, list) else data.get("records", data.get("items", []))
    result = {}
    for row in rows:
        if row.get("type") != "post":
            continue
        clean = lambda value: str(value or "").strip()
        key = "|".join([clean(row.get("post_id")), clean(row.get("author")),
                        clean(row.get("url")), clean(row.get("text"))[:500]])
        sid = "sig_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        entries = []
        if row.get("author_type") == "person":
            entries.append({"name": row.get("author"), "url": row.get("author_url"), "source": "feed_author"})
        entries.extend({"name": m.get("name"), "url": m.get("url"), "source": "feed_mention"}
                       for m in row.get("mentions") or [] if isinstance(m, dict))
        result[sid] = [entry for entry in entries if name_key(entry["name"]) and profile_url(entry["url"])]
    return result


def with_feed_profiles(signal, index):
    row = copy.deepcopy(signal)
    if signal.get("signal_id") in index:
        row["_person_profile_evidence"] = copy.deepcopy(index[signal["signal_id"]])
    return row


def person_profile(signal, name, crm=None):
    """Return an unambiguous URL and its provenance for this exact person."""
    target = name_key(name)
    if not target:
        return None, None
    candidates = {}

    def add(candidate_name, url, source):
        canonical = profile_url(url)
        if name_key(candidate_name) == target and canonical:
            candidates[profile_key(canonical)] = (canonical, {
                "name": name, "url": canonical, "source": source,
                "signal_id": signal.get("signal_id"),
            })

    for entry in signal.get("_person_profile_evidence") or []:
        add(entry.get("name"), entry.get("url"), entry.get("source") or "feed_mention")
    for person in signal.get("people") or []:
        if isinstance(person, dict):
            add(person.get("name"), person.get("profile_url") or person.get("url"), "signal_person")
    for link in signal.get("links") or []:
        if isinstance(link, dict):
            add(link.get("name") or link.get("person_name"), link.get("url"), "named_link")
    if signal.get("author_type") == "person" and name_key(signal.get("author")) == target:
        add(name, signal.get("source_url"), "author_source")
        for link in signal.get("links") or []:
            if isinstance(link, dict) and link.get("relation") == "author":
                add(name, link.get("url"), "author_link")

    # CRM is a fallback only when its URL has explicit identity provenance.
    # Legacy guessed URLs must not become trusted evidence on the next rebuild.
    if not candidates:
        for record in (crm or {}).get("items") or []:
            proof = record.get("profile_evidence") or {}
            if (record.get("kind") == "person" and name_key(record.get("name")) == target
                    and name_key(proof.get("name")) == target
                    and proof.get("source") in {"feed_author", "feed_mention", "signal_person", "named_link", "author_source", "author_link", "manual"}
                    and profile_key(proof.get("url")) == profile_key(record.get("profile_url"))):
                add(record.get("name"), record.get("profile_url"), proof["source"])
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return None, None
