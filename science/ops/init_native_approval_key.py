"""Create an owner-only host/MCP approval key; never prints key bytes or overwrites."""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error('Use an explicit absolute path outside versioned source')
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(secrets.token_bytes(32))
        stream.flush()
        os.fsync(stream.fileno())
    print('Created owner-only native approval key; contents not printed.')


if __name__ == '__main__':
    main()
