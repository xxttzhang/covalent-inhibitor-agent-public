"""Line-oriented MCP relay to the fixed Library instance; never retry a call.

The scientific server persists studies and signed jobs on shared storage. Each
request uses a fresh server process, retaining its existing authorization path.
"""
import argparse
import base64
import json
import shlex
import sys

from ligand_bound_mcp import read_profile, remote_command


def command_for(command, message):
    raw = json.dumps({'command': command, 'message': message}, ensure_ascii=False).encode()
    program = (
        'import base64,json,subprocess; '
        f'p=json.loads(base64.b64decode({base64.b64encode(raw).decode()!r})); '
        "r=subprocess.run(['bash','-lc',p['command']],input=json.dumps(p['message'])+'\\n',"
        "capture_output=True,text=True,timeout=600); "
        "print(json.dumps({'returncode':r.returncode,'output':r.stdout}))"
    )
    return shlex.join(['python3', '-B', '-c', program])


def response_for(result, identifier):
    if not result.completed or result.truncated or result.returncode != 0:
        raise ValueError('Incomplete remote response; inspect original study before any retry')
    envelope = json.loads(result.output)
    if envelope['returncode'] != 0:
        raise ValueError('Scientific server failed; inspect original study before any retry')
    messages = [json.loads(line) for line in envelope['output'].splitlines() if line.strip()]
    matches = [item for item in messages if item.get('id') == identifier and item.get('jsonrpc') == '2.0']
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous scientific response; do not replay')
    return matches[0]


def main():
    from inspire import InspireClient
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--connection', required=True)
    args = parser.parse_args()
    profile = read_profile(args.connection)
    if (set(profile) != {'notebook', 'workspace', 'account', 'transport'}
            or profile['notebook'] != 'asgbie2' or profile['workspace'] != 'CPU资源空间'
            or profile['transport'] != 'jupyter' or not isinstance(profile['account'], str)
            or not profile['account']):
        raise ValueError('Library MCP requires its owner-bound asgbie2 Jupyter profile')
    command = remote_command(read_profile(args.binding), runtime=read_profile(args.runtime), verify_runtime_config=True)
    with InspireClient(account=profile['account'], allow_browser=True) as client:
        for line in sys.stdin:
            message = json.loads(line)
            if 'id' not in message:
                continue
            try:
                result = client.notebooks.exec('asgbie2', workspace='CPU资源空间',
                    command=command_for(command, message), transport='jupyter', timeout=620)
                response = response_for(result, message['id'])
            except Exception:
                # No second remote call: the first may already have registered a job.
                response = {'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32603,
                    'message': 'Library transport failed or outcome is unknown. Inspect the original study; do not repeat submission.'}}
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
