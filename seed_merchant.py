#!/usr/bin/env python3
"""Build a local development database from the production data.

Production is the only live database, so the guiding rule here is that this
script never writes to it. The production connection is opened separately from
the application's own configuration, it is opened read-only, and every write
goes to the target. The target is refused if it looks like the production host.

Why this copies everything instead of sampling: the business collections total
about 12 MB across roughly 19,000 documents. The 40 GB figure for production is
almost entirely GridFS photo bytes (`photos.chunks`) plus request logs. Copying
the business data whole is therefore cheap, and it avoids the real cost of a
percentage sample, which is dangling references: a sampled `location` leaves
`shopProducts`, `store_photos` and `store_notes` rows pointing at store codes
that no longer exist, and bugs that only appear with complete data stay hidden.

What is deliberately not copied:

- `photos.chunks`: the image bytes are skipped entirely. This is 39 GB of the
  40 GB total and the only real bottleneck. `photos.files` is still copied, so
  every photo reference resolves and the metadata is intact; requesting the
  bytes returns a 404 the same way a legacy on-disk photo already does. Real
  store photography should not sit on a laptop either.

`user_logs` is copied in full: 206k request logs are ~225 MB, which is a
reasonable local cost, and having the real volume is what makes the activity
dashboard and its date filtering behave as they do in production.

What is anonymised, because a development copy should not carry personal data:

- every password hash becomes the same known development password
- activity-log IP addresses and geolocation are dropped

Usage:
    export PROD_MONGO_URI='mongodb://...'          # read-only credentials
    export DEV_MONGO_URI='mongodb://127.0.0.1:27018/'
    python seed_merchant.py --dry-run
    python seed_merchant.py
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlsplit

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from werkzeug.security import generate_password_hash

# Copied in full: small, and interdependent by store code.
BUSINESS_COLLECTIONS = [
    "users",
    "location",
    "regions",
    "brands",
    "catalog_brands",
    "catalog_products",
    "shopProducts",
    "shopInventory",
    "store_photos",
    "shelf_photos",
    "store_notes",
    "store_reactions",
    "competitor_promotions",
    "contract_orders",
    "contract_proposal_rejections",
    "contracts",
]

DEV_PASSWORD = "devpassword"

# Insert in batches so a large collection is never held in memory in full.
BATCH_SIZE = 2_000


def _host_of(uri: str) -> str:
    # urlsplit needs a scheme it recognises to populate hostname.
    return urlsplit(uri.replace("mongodb://", "http://", 1)).hostname or ""


def _assert_distinct(prod_uri: str, dev_uri: str) -> None:
    """Refuse to run unless the target is demonstrably not production."""
    prod_host = _host_of(prod_uri)
    dev_host = _host_of(dev_uri)

    if not prod_host or not dev_host:
        sys.exit("Could not parse a host from one of the URIs. Check both values.")
    if prod_host == dev_host:
        sys.exit(
            f"Refusing to run: target host {dev_host!r} is the production host.\n"
            "DEV_MONGO_URI must point at a local or otherwise separate server."
        )
    if dev_host not in {"localhost", "127.0.0.1", "::1"}:
        sys.exit(
            f"Refusing to run: target host {dev_host!r} is not local.\n"
            "This script is only intended to populate a local development database."
        )
    if urlsplit(dev_uri.replace("mongodb://", "http://", 1)).port != 27018:
        sys.exit("Refusing to run: DEV_MONGO_URI must use the shared local port 27018.")


def _copy_collection(source, target, name: str, transform=None, limit=None, sort=None) -> int:
    """Stream a collection into the target in batches.

    Documents are inserted in batches rather than accumulated and written once,
    so that a large collection such as user_logs (206k documents) does not have
    to be held in memory in its entirety.
    """
    cursor = source[name].find({})
    if sort:
        cursor = cursor.sort(*sort)
    if limit:
        cursor = cursor.limit(limit)

    target[name].drop()

    batch = []
    copied = 0
    for document in cursor:
        if transform:
            document = transform(document)
        if document is None:
            continue
        batch.append(document)
        if len(batch) >= BATCH_SIZE:
            target[name].insert_many(batch, ordered=False)
            copied += len(batch)
            batch = []

    if batch:
        target[name].insert_many(batch, ordered=False)
        copied += len(batch)
    return copied


def _anonymise_user(document: dict, password_hash: str) -> dict:
    if "password" in document:
        document["password"] = password_hash
    return document


def _anonymise_log(document: dict) -> dict:
    client = document.get("client")
    if isinstance(client, dict):
        client.pop("ip_address", None)
    document.pop("geolocation", None)
    return document


def _copy_photo_metadata(source, target, dry_run: bool) -> int:
    """Copy the GridFS file records without their bytes.

    photos.chunks holds 39 GB of image data and is skipped entirely: it is the
    only part of production that is actually large.

    Each record keeps its original _id, because `store_photos` and
    `shelf_photos` reference those ids, but `length` is set to zero. That
    matters: with a non-zero length and no chunks, GridFS opens the file
    successfully and then raises CorruptGridFile partway through streaming,
    after the response headers have already been sent, which produces a
    truncated image response instead of a clean failure. A zero length means
    the read completes immediately and the route returns an empty body, which
    the browser renders as a broken image and no server error.
    """
    files = list(source["photos.files"].find({}))
    if dry_run:
        return len(files)

    target["photos.files"].drop()
    target["photos.chunks"].drop()
    if files:
        for record in files:
            record["length"] = 0
            record.pop("md5", None)
        target["photos.files"].insert_many(files, ordered=False)
    return len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be copied without writing to the target",
    )
    parser.add_argument(
        "--log-limit",
        type=int,
        default=0,
        help="copy only the N most recent activity logs (default: copy all)",
    )
    arguments = parser.parse_args()

    prod_uri = os.environ.get("PROD_MONGO_URI", "").strip()
    dev_uri = os.environ.get("DEV_MONGO_URI", "").strip()
    if not prod_uri or not dev_uri:
        print(
            "Set PROD_MONGO_URI and DEV_MONGO_URI.\n"
            "PROD_MONGO_URI should use read-only credentials.",
            file=sys.stderr,
        )
        return 2

    _assert_distinct(prod_uri, dev_uri)

    # readPreference=secondary keeps the load off the primary where a replica
    # set exists; it is harmless on a standalone server.
    source_client = MongoClient(prod_uri, readPreference="secondaryPreferred")
    target_client = MongoClient(dev_uri)

    try:
        source = source_client["atpg"]
        target = target_client["atpg"]

        if arguments.dry_run:
            print("Dry run: nothing will be written.\n")

        password_hash = generate_password_hash(DEV_PASSWORD)
        total = 0

        for name in BUSINESS_COLLECTIONS:
            if name not in source.list_collection_names():
                print(f"  {name:32s} absent in production, skipped")
                continue
            if arguments.dry_run:
                count = source[name].estimated_document_count()
                print(f"  {name:32s} {count:>7d} documents would be copied")
                total += count
                continue
            transform = None
            if name == "users":
                transform = lambda d: _anonymise_user(d, password_hash)  # noqa: E731
            count = _copy_collection(source, target, name, transform=transform)
            print(f"  {name:32s} {count:>7d} documents")
            total += count

        # Counters live in a separate database and drive new store codes.
        if not arguments.dry_run:
            counters = list(source_client["store_db"]["counters"].find({}))
            target_client["store_db"]["counters"].drop()
            if counters:
                target_client["store_db"]["counters"].insert_many(counters)
            print(f"  {'store_db.counters':32s} {len(counters):>7d} documents")

        # user_logs is copied in full by default. It is ~225 MB, which is an
        # acceptable local cost, and the real volume is what makes the activity
        # dashboard and its date filters behave as they do in production.
        # --log-limit trades that fidelity for a faster copy.
        log_total = source["user_logs"].estimated_document_count()
        if arguments.dry_run:
            log_count = min(arguments.log_limit, log_total) if arguments.log_limit else log_total
        else:
            log_count = _copy_collection(
                source,
                target,
                "user_logs",
                transform=_anonymise_log,
                limit=arguments.log_limit or None,
                sort=("timestamp", -1) if arguments.log_limit else None,
            )
        suffix = " (most recent)" if arguments.log_limit else " (all)"
        print(f"  {'user_logs':32s} {log_count:>7d} documents{suffix}")

        files = _copy_photo_metadata(source, target, arguments.dry_run)
        print(f"  {'photos.files':32s} {files:>7d} records (metadata only)")
        print(f"  {'photos.chunks':32s} {0:>7d} skipped - 39 GB of image bytes")

        if arguments.dry_run:
            print("\nDry run complete. Target was not modified.")
            return 0

        # Rebuild source indexes without depending on the merchant repository.
        for name in [*BUSINESS_COLLECTIONS, "user_logs", "photos.files"]:
            if name not in source.list_collection_names():
                continue
            for index in source[name].list_indexes():
                if index["name"] == "_id_":
                    continue
                options = {key: value for key, value in index.items()
                           if key not in {"v", "key", "ns"}}
                target[name].create_index(list(index["key"].items()), **options)
        print("\nIndexes created.")

        print(f"\nDevelopment database ready: {total + log_count} documents.")
        print(f"Every user's password is now {DEV_PASSWORD!r}.")
        print("Photo bytes were not copied; photo routes return an empty image locally.")
        return 0
    except PyMongoError as error:
        print(f"MongoDB error: {error}", file=sys.stderr)
        return 1
    finally:
        source_client.close()
        target_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
