"""The `news` command: api, workers, migrations, config and keys."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from news.logs import setup_logging


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from news.config import seed
    from news.db.engine import dispose_engine, sessionmaker
    from news.env import env

    command.upgrade(Config("alembic.ini"), "head")

    async def run() -> None:
        async with sessionmaker()() as session:
            counts = await seed(session, env().config_dir)
        await dispose_engine()
        print(f"Seeded from {env().config_dir}: {counts}")

    asyncio.run(run())


def _config(action: str, overwrite: bool, out_dir: str | None) -> None:
    from news.config import export, seed
    from news.db.engine import dispose_engine, sessionmaker
    from news.env import env

    async def run() -> None:
        async with sessionmaker()() as session:
            if action == "reload":
                print(await seed(session, env().config_dir, overwrite=overwrite))
            else:
                for path in await export(session, Path(out_dir) if out_dir else env().config_dir):
                    print(f"Wrote {path}")
        await dispose_engine()

    asyncio.run(run())


def _create_key(name: str, role: str, out: str | None, client: str | None) -> None:
    from sqlalchemy import select

    from news.db.engine import dispose_engine, sessionmaker
    from news.db.models import Client
    from news.keys import create_key

    if role == "client" and not client:
        raise SystemExit("Client keys need --client NAME")

    async def run() -> str:
        async with sessionmaker()() as session:
            client_id = None
            if client:
                row_c = await session.scalar(select(Client).where(Client.name == client))
                if row_c is None:
                    row_c = Client(name=client)
                    session.add(row_c)
                    await session.flush()
                client_id = row_c.id
            row, key = await create_key(session, name, role, client_id)
            await session.commit()
        await dispose_engine()
        return key

    key = asyncio.run(run())
    if out:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        print(f"Created {role} key '{name}' ({key[:10]}…), written to {out}")
    else:
        print(key)


def _geonames(directory: str) -> None:
    from news.db.engine import dispose_engine, sessionmaker
    from news.geo import download, load

    dest = Path(directory)
    download(dest)

    async def run() -> int:
        async with sessionmaker()() as session:
            n = await load(session, dest)
        await dispose_engine()
        return n

    print(f"Loaded {asyncio.run(run())} places from GeoNames (CC BY 4.0)")


def _eval_precision(labels: str, k: int) -> None:
    """precision@k of the current ranking against hand labels: a CSV with
    columns cluster_id, important (1/0), and optionally topic."""
    import csv
    from collections import defaultdict

    from sqlalchemy import text

    from news.db.engine import dispose_engine, sessionmaker

    with open(labels, newline="") as f:
        rows = list(csv.DictReader(f))
    truth = {r["cluster_id"]: r.get("important", "0").strip() in ("1", "true", "yes") for r in rows}
    topic_of = {r["cluster_id"]: (r.get("topic") or "all").strip() or "all" for r in rows}

    async def run() -> dict[str, float]:
        async with sessionmaker()() as session:
            scored = (await session.execute(text("SELECT id::text AS id, score FROM clusters WHERE id::text = ANY(:ids)"),
                                            {"ids": list(truth)})).all()
        await dispose_engine()
        by_topic: dict[str, list[tuple[float, bool]]] = defaultdict(list)
        for r in scored:
            for t in {topic_of[r.id], "all"}:
                by_topic[t].append((float(r.score), truth[r.id]))
        return {t: sum(ok for _, ok in sorted(v, reverse=True)[:k]) / min(k, len(v)) for t, v in by_topic.items() if v}

    for topic, p in sorted(asyncio.run(run()).items()):
        print(f"{topic:30} precision@{k} = {p:.2f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="news")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("api", help="Run the API server")
    worker = sub.add_parser("worker", help="Run a pipeline worker")
    worker.add_argument("name", choices=["ingest", "normalise", "stories", "enrich", "deliver"])
    sub.add_parser("migrate", help="Apply database migrations and seed from config")

    geo = sub.add_parser("geonames", help="Download and load GeoNames places")
    geo.add_argument("action", choices=["load"])
    geo.add_argument("--dir", default="/models/geonames")

    ev = sub.add_parser("eval", help="Evaluate ranking against hand labels")
    ev.add_argument("action", choices=["precision"])
    ev.add_argument("--labels", required=True, help="CSV: cluster_id, important (1/0), topic (optional)")
    ev.add_argument("--k", type=int, default=10)

    cfg = sub.add_parser("config", help="Reload config files into the database, or export it back")
    cfg.add_argument("action", choices=["reload", "export"])
    cfg.add_argument("--overwrite", action="store_true", help="reload: files replace database values")
    cfg.add_argument("--dir", help="export: directory to write to (default: the config directory)")

    keys = sub.add_parser("keys", help="Create an API key")
    keys.add_argument("action", choices=["create"])
    keys.add_argument("--name", required=True)
    keys.add_argument("--role", required=True, choices=["admin", "moderator", "client", "viewer"])
    keys.add_argument("--out", help="Write the key to this file (mode 600) instead of printing it")
    keys.add_argument("--client", help="Client name (created if new); required for client keys")

    args = parser.parse_args(argv)
    setup_logging()

    if args.cmd == "api":
        import uvicorn

        uvicorn.run("news.api.app:create_app", factory=True, host="0.0.0.0", port=8000,
                    proxy_headers=True, forwarded_allow_ips="*", log_config=None)
    elif args.cmd == "worker":
        import importlib

        asyncio.run(importlib.import_module(f"news.workers.{args.name}").run())
    elif args.cmd == "geonames":
        _geonames(args.dir)
    elif args.cmd == "eval":
        _eval_precision(args.labels, args.k)
    elif args.cmd == "migrate":
        _migrate()
    elif args.cmd == "config":
        _config(args.action, args.overwrite, args.dir)
    elif args.cmd == "keys":
        _create_key(args.name, args.role, args.out, args.client)
    else:  # pragma: no cover
        parser.print_help()
        sys.exit(2)
