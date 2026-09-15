"""Encrypted core-data backups. Restore only to a new directory; never starts checks."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

from google_mail import protected

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10000
ACCOUNT_DB = 'results/accounts.sqlite3'
USER = r'results/users/([a-f0-9]{32})/results/'
CORE = re.compile(USER + r'(workspace/(profiles|schedule)\.json|google_connections/[a-f0-9]{32}\.bin|monitors/[a-f0-9]{32}/service/[0-9]+/state\.json)')
ARCHIVE_NAME = re.compile(r'fm-core-[0-9]{8}T[0-9]{12}Z-[a-f0-9]{8}\.fmbackup')


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


def validate_contents(files):
    """Check the database, JSON and encrypted Gmail tokens without making requests."""
    with tempfile.TemporaryDirectory(prefix='fm-verify-') as folder:
        database = Path(folder) / 'accounts.sqlite3'
        database.write_bytes(files[ACCOUNT_DB])
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Backup account database is damaged.')
            users = db.execute('SELECT id,email,salt,digest,owner FROM users').fetchall()
    owners = sum(row[4] == 1 for row in users)
    if users and owners != 1:
        raise ValueError('Backup must have exactly one owner.')
    user_ids = set()
    for ident, email, salt, digest, owner in users:
        if (not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident)
                or ident in user_ids or not isinstance(email, str) or '@' not in email
                or not isinstance(salt, bytes) or len(salt) != 16
                or not isinstance(digest, bytes) or len(digest) != 32 or owner not in (0, 1)):
            raise ValueError('Invalid account in backup.')
        user_ids.add(ident)
    for name, content in files.items():
        if name == ACCOUNT_DB:
            continue
        match = CORE.fullmatch(name)
        if not match or match[1] not in user_ids:
            raise ValueError('Backup contains data for an unknown account.')
        if name.endswith('.bin'):
            value = json.loads(protected(content, decrypt=True))
            if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k]
                    for k in ('email', 'client_id', 'access_token', 'refresh_token')):
                raise ValueError('Invalid Gmail connection in backup.')
            continue
        value = json.loads(content)
        if name.endswith('/profiles.json'):
            from workspace import Workspace
            if not isinstance(value, list) or len(value) > 1000:
                raise ValueError('Invalid monitor list in backup.')
            ids = set()
            for profile in value:
                if (not isinstance(profile, dict) or not isinstance(profile.get('id'), str)
                        or not re.fullmatch('[a-f0-9]{32}', profile['id']) or profile['id'] in ids
                        or type(profile.get('revision')) is not int or profile['revision'] < 1):
                    raise ValueError('Invalid monitor in backup.')
                Workspace.validate(profile)
                ids.add(profile['id'])
        elif name.endswith('/schedule.json'):
            if (not isinstance(value, dict) or any(not re.fullmatch('[a-f0-9]{32}', k)
                    or type(v) not in (int, float) or not 0 <= v < 1e12 for k, v in value.items())):
                raise ValueError('Invalid schedule in backup.')
        elif not isinstance(value, dict) or value.get('notified_state') not in ('UNKNOWN', 'UP', 'DOWN'):
            raise ValueError('Invalid notification state in backup.')
    return len(users)


def read_archive(archive):
    """Authenticate and validate the entire archive before any restoration writes."""
    archive = Path(archive)
    if archive.stat().st_size > MAX_BYTES * 2:
        raise ValueError('Backup exceeds the size limit.')
    raw = protected(archive.read_bytes(), decrypt=True)
    if len(raw) > MAX_BYTES + 2 * 1024 * 1024:
        raise ValueError('Backup exceeds the size limit.')
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        items = bundle.infolist()
        names = [item.filename for item in items]
        if (len(items) > MAX_FILES + 1 or len(set(names)) != len(names)
                or sum(item.file_size for item in items) > MAX_BYTES + 1024 * 1024
                or 'manifest.json' not in names or ACCOUNT_DB not in names
                or any(name not in ('manifest.json', ACCOUNT_DB) and not CORE.fullmatch(name) for name in names)):
            raise ValueError('Unexpected backup contents.')
        if any(item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000 for item in items):
            raise ValueError('Links and directories are not allowed in a backup.')
        manifest = json.loads(bundle.read('manifest.json'))
        files = {name: bundle.read(name) for name in names if name != 'manifest.json'}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    if (not isinstance(manifest, dict) or manifest.get('format') != 'form-monitor-core-v1'
            or manifest.get('files') != hashes):
        raise ValueError('Backup checksum or format does not match.')
    manifest['accounts'] = validate_contents(files)
    return manifest, files


def create_backup(root):
    """Caller must exclude account/config/token writers and active monitor children."""
    root = Path(root).resolve()
    source = root / ACCOUNT_DB
    if not source.is_file() or source.is_symlink():
        raise ValueError('Account database was not found.')
    folder = root / 'backups'
    if folder.is_symlink():
        raise ValueError('Backup directory must not be a link.')
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    with tempfile.TemporaryDirectory(prefix='.fm-stage-', dir=folder) as stage:
        snapshot = Path(stage) / 'accounts.sqlite3'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
            with closing(sqlite3.connect(snapshot)) as destination:
                db.backup(destination)
        files = {ACCOUNT_DB: snapshot.read_bytes()}
        total = len(files[ACCOUNT_DB])
        for path in sorted((root / 'results/users').rglob('*')):
            if path.is_symlink():
                raise ValueError('User data must not contain links.')
            name = path.relative_to(root).as_posix()
            if not path.is_file() or not CORE.fullmatch(name):
                continue
            total += path.stat().st_size
            if total > MAX_BYTES or len(files) >= MAX_FILES:
                raise ValueError('Core data exceeds the backup limit. Contact the server owner.')
            files[name] = path.read_bytes()
        if sum(map(len, files.values())) > MAX_BYTES:
            raise ValueError('Core data exceeds the backup limit.')
        manifest = dict(format='form-monitor-core-v1', created_utc=datetime.now(timezone.utc).isoformat(),
                        files={name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
                        excludes=['check history', 'screenshots', 'server environment secrets'])
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for name, value in files.items():
                bundle.writestr(name, value)
            bundle.writestr('manifest.json', encode_json(manifest))
        temporary = Path(stage) / 'backup.fmbackup'
        temporary.write_bytes(protected(payload.getvalue()))
        os.chmod(temporary, 0o600)
        read_archive(temporary)
        name = 'fm-core-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8] + '.fmbackup'
        final = folder / name
        temporary.replace(final)
    # Only rotate our own files, and only after a new copy has passed verification.
    for old in backup_files(root)[7:]:
        old.unlink()
    return final


def backup_files(root):
    return sorted((path for path in (Path(root) / 'backups').glob('fm-core-*.fmbackup')
                   if ARCHIVE_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()),
                  key=lambda path: path.name, reverse=True)


def restore_backup(archive, destination):
    """The output is a staged data root with every schedule paused. No live writes."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Restore destination must be a new directory.')
    manifest, files = read_archive(archive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.fm-restore-', dir=destination.parent) as stage:
        for name, value in files.items():
            path = Path(stage) / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(b'{}' if name.endswith('/schedule.json') else value)
            os.chmod(path, 0o600)
        # Exclusive creation prevents replacing an existing data directory.
        destination.mkdir(mode=0o700)
        try:
            shutil.move(str(Path(stage) / 'results'), str(destination / 'results'))
        except BaseException:
            if not any(destination.iterdir()):
                destination.rmdir()
            raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['verify', 'restore'])
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'restore':
            if args.destination is None:
                parser.error('--destination is required for restore')
            manifest = restore_backup(args.archive, args.destination)
            print('Restored to a new directory. All schedules are paused; no checks were sent.')
        else:
            manifest, _ = read_archive(args.archive)
            print('Backup verified: database, checksums and Gmail token decryption passed.')
        print('Accounts:', manifest['accounts'])
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError, TypeError):
        parser.exit(1, 'Backup operation failed. Check the archive, encryption key and destination.\n')


if __name__ == '__main__':
    main()
