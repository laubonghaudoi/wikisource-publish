#!/usr/bin/env python3
"""Store a Wikimedia Bot Password locally without echoing the secret."""
import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile


DEFAULT = Path.home() / '.config' / 'wikisource-publish' / 'credentials.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=DEFAULT)
    args = parser.parse_args()
    if not os.isatty(0):
        parser.error('Run this setup in your own interactive terminal; do not pass secrets as command arguments.')
    print('Create a dedicated password at https://zh.wikisource.org/wiki/Special:BotPasswords')
    print('Allow editing existing pages, creating/editing/moving pages, and uploading new files.')
    print('Enter the full login name shown by Wikimedia (usually Account@Application).')
    username = input('Bot login name: ').strip()
    if not username or '@' not in username:
        parser.error('Expected a nonempty Account@Application bot login name.')
    password = getpass.getpass('Bot Password (hidden): ').strip()
    if not password:
        parser.error('Password cannot be empty.')
    path = args.path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.credentials-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'username': username, 'password': password}, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f'Saved credentials securely to {path}. No password was printed.')


if __name__ == '__main__':
    main()
