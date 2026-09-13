# Shared local MongoDB

This folder is the standalone `shared-mongo` project. The Compose project has one MongoDB
container, one persistent named volume, and one Docker network. Keep this stack
running while developing any of the apps; each app uses its own database names
inside the same MongoDB server.

```sh
cd /home/winenpot/code/shared-mongo
docker -H unix:///var/run/docker.sock compose up -d --wait
docker -H unix:///var/run/docker.sock compose ps
```

On the host, connect with `mongodb://127.0.0.1:27018/`. For containers, attach
the app to the external Docker network `shared-dev-mongo_default` and connect
with `mongodb://mongo:27017/`. Start this Compose project first so that network
exists. Port 27018 is bound only to localhost. Avoid `docker compose down -v`:
that removes the volume and every app's local databases. Ordinary `docker
compose down` keeps the data.

The merchant snapshot is made by `seed_merchant.py`. It copies the selected
merchant collections from a read-only production account into local databases
`atpg` and `store_db`, replaces user password hashes, removes logged IPs and
geolocation, and skips GridFS image bytes. Other fields may still contain
sensitive data. It is a development fixture, not a complete production backup.
Other apps need their own copy/anonymization scripts
that target their respective database names; they do not need another MongoDB
container or volume. Never commit production credentials, BSON dumps, or local
snapshots; `.gitignore` excludes common files, but review `git status` before
committing. The local service has no authentication, so keep the port bound to
loopback and only attach trusted local app containers to its network.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export PROD_MONGO_URI='mongodb://<read-only-user>:<password>@<host>:27017/'
export DEV_MONGO_URI='mongodb://127.0.0.1:27018/'
.venv/bin/python seed_merchant.py --dry-run
.venv/bin/python seed_merchant.py
```

The volume belongs to the `shared-dev-mongo` Compose project, so moving an app
repository or stopping its container does not remove it. Back up the local
volume separately if retaining your development data matters.
