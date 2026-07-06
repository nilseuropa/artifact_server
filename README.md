# Artifact Server

A small self-hosted companion server for the Artifact Android app.

It accepts the app's existing `.artifactbackup` archive, stores the collection
locally, exposes a browser UI for editing item metadata, and lets the app pull
the edited collection back as the same archive format.

## Run

```bash
python3 artifact_server.py --host 0.0.0.0 --port 8080
```

Open `http://SERVER:8080/` in a browser.

Data is stored under `data/` in this checkout:

- `data/artifact.sqlite3`
- `data/photos/`

## Access Token

Set `ARTIFACT_SYNC_TOKEN` to require a token for both the web UI and phone API:

```bash
ARTIFACT_SYNC_TOKEN='choose-a-long-random-token' python3 artifact_server.py --host 0.0.0.0 --port 8080
```

The browser UI will show a login page. In the Android app, enter the same
server URL and token in `Collection Settings`.

## Android Sync Flow

In the app:

1. Open `Collection Settings`.
2. Enter `Server URL`, for example `http://192.168.1.50:8080`.
3. Enter the access token if the server uses one.
4. Tap `Push to Server` to upload the complete phone collection.
5. Edit items in the browser UI.
6. Tap `Pull from Server` to replace the phone collection with the server copy.

This first version uses full collection push/pull. It does not merge concurrent
phone and server edits. Pulling from the server replaces the local collection,
photos, collection settings, and UID history with the server archive.

## API

The phone uses these endpoints:

- `POST /api/backup` with `Content-Type: application/zip` uploads and replaces the server collection.
- `GET /api/backup` downloads the current collection as an Artifact backup archive.
- `GET /api/status` returns counts and revision metadata as JSON.

When `ARTIFACT_SYNC_TOKEN` is set, pass it as:

```text
Authorization: Bearer <token>
```

## Notes

