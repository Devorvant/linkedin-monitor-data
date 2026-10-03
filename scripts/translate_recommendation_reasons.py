#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path

from openai import OpenAI


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def reason_id(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def translate_reasons(reasons, model):
    payload = [{"id": reason_id(text), "en": text} for text in reasons]
    prompt = (
        "Translate these LinkedIn recommendation explanations from English to Russian. "
        "Preserve names, companies, role names, and technical terminology. "
        "Do not add facts or advice. Return only JSON: an array of objects "
        'with keys "id" and "ru".\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    client = OpenAI()
    response = client.responses.create(model=model, input=prompt)
    text = (response.output_text or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = text.strip(chr(96)).strip()
        if text.startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    result = {}
    for item in data:
        key = str(item.get("id") or "").strip()
        ru = str(item.get("ru") or "").strip()
        if key and ru:
            result[key] = ru
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recommendations", default="recommendations_current.json")
    ap.add_argument("--cache", default="recommendation_reason_translations.json")
    ap.add_argument("--model", default="gpt-5.6-luna")
    args = ap.parse_args()

    rec_path = Path(args.recommendations)
    cache_path = Path(args.cache)
    data = load_json(rec_path, {"items": []})
    cache = load_json(cache_path, {"schema_version": 1, "translations": {}})
    translations = dict(cache.get("translations") or {})

    missing = []
    for item in data.get("items") or []:
        reason = str(item.get("reason") or "").strip()
        if reason and reason_id(reason) not in translations:
            missing.append(reason)
    missing = list(dict.fromkeys(missing))

    if missing:
        translated = translate_reasons(missing, args.model)
        for reason in missing:
            key = reason_id(reason)
            ru = translated.get(key)
            if ru:
                translations[key] = {
                    "source": reason,
                    "ru": ru,
                    "model": args.model,
                }
        print(f"translated={len(translated)}/{len(missing)} model={args.model}")
    else:
        print("translated=0 cache_hit_all=true")

    applied = 0
    for item in data.get("items") or []:
        reason = str(item.get("reason") or "").strip()
        cached = translations.get(reason_id(reason)) if reason else None
        if isinstance(cached, dict) and cached.get("ru"):
            item["reason_ru"] = cached["ru"]
            applied += 1

    cache_path.write_text(
        json.dumps({"schema_version": 1, "translations": translations}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rec_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"reason_ru_applied={applied} cache={len(translations)}")


if __name__ == "__main__":
    main()
