"""Stdio MCP over an installation-bound Jupyter transport; never retry a call."""
import argparse
import base64
import json
import secrets
import shlex
import sys

from ligand_bound_mcp import read_profile, remote_command


LIMIT = 16 * 1024 * 1024


def command_for(command, message, marker):
    raw = json.dumps(message, ensure_ascii=False).encode() + b'\n'
    if len(raw) > 1_000_000:
        raise ValueError('MCP request exceeds transport limit')
    payload = base64.b64encode(raw).decode('ascii')
    program = (
        'import base64,json,subprocess; '
        f'p=subprocess.run({command!r},shell=True,executable="/bin/bash",'
        f'input=base64.b64decode({payload!r}),capture_output=True); '
        f'print({marker!r}+json.dumps(dict(returncode=p.returncode,'
        'stdout=base64.b64encode(p.stdout).decode("ascii"))))')
    return shlex.join(['python3', '-B', '-c', program])


def decode_response(result, marker, request_id):
    if not result.completed or result.truncated or result.returncode != 0:
        raise RuntimeError('Uncertain MCP transport outcome '
                           f'(completed={result.completed}, truncated={result.truncated}, '
                           f'returncode={result.returncode}); observe workflow state, never replay')
    lines = [line[len(marker):] for line in result.output.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise ValueError('Missing or ambiguous MCP transport receipt; do not replay')
    receipt = json.loads(lines[0])
    if receipt['returncode'] != 0:
        raise RuntimeError('Bound remote MCP process failed; do not replay')
    raw = base64.b64decode(receipt['stdout'], validate=True)
    if len(raw) > LIMIT:
        raise ValueError('MCP response exceeds bounded transport')
    value = json.loads(raw)
    if value.get('jsonrpc') != '2.0' or value.get('id') != request_id:
        raise ValueError('MCP response identity mismatch')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--connection', required=True)
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    profile = read_profile(args.connection)
    if (set(profile) != {'notebook', 'workspace', 'account'}
            or profile['notebook'] != 'asgbie4' or profile['workspace'] != 'CPU资源空间'
            or not isinstance(profile['account'], str) or not profile['account']):
        raise ValueError('Expected exact registered asgbie4 Jupyter connection')
    command = remote_command(read_profile(args.binding), verify_runtime_config=True,
                             runtime=read_profile(args.runtime))
    from inspire import InspireClient
    with InspireClient(account=profile['account'], allow_browser=True, operation_timeout=600) as client:
        for line in sys.stdin:
            message = json.loads(line)
            # The existing server ignores notifications and persists scientific state on disk.
            if message.get('id') is None:
                continue
            marker = 'MCP_RECEIPT_' + secrets.token_hex(16) + ':'
            result = client.notebooks.exec(profile['notebook'], workspace=profile['workspace'],
                transport='jupyter', command=command_for(command, message, marker),
                timeout=590, max_output_bytes=LIMIT * 2)
            value = decode_response(result, marker, message['id'])
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()
