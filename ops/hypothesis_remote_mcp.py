"""SSH stdio MCP bridge. Never retries a call or rewrites approval envelopes."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import threading

def bridge_config(filename):
    if not filename:
        return {'legacy_root': '', 'release_roots': {}, 'ssh': [], 'delivery': None}
    value = json.loads(Path(filename).read_text())
    if not isinstance(value, dict) or set(value) != {'legacy_root', 'release_roots', 'ssh', 'delivery'}:
        raise ValueError('Expected an explicit SSH bridge configuration')
    roots = value['release_roots']
    if (not isinstance(roots, dict) or not roots or not isinstance(value['legacy_root'], str)
            or value['legacy_root'] not in roots):
        raise ValueError('Legacy root must have an exact registered release root')
    for item in [*roots, *roots.values()]:
        if (not isinstance(item, str) or not item.startswith('/inspire/')
                or str(PurePosixPath(item)) != item or '..' in PurePosixPath(item).parts
                or any(ord(char) < 32 for char in item)):
            raise ValueError('Expected normalized absolute Inspire roots')
    if (not isinstance(value['ssh'], list) or not value['ssh']
            or any(not isinstance(item, str) or not item or '\0' in item for item in value['ssh'])
            or Path(value['ssh'][0]).name != 'ssh'):
        raise ValueError('Expected an explicit SSH argument array')
    if not isinstance(value['delivery'], str) or not Path(value['delivery']).is_absolute():
        raise ValueError('Expected an absolute external delivery directory')
    return value


CONFIG = bridge_config(os.environ.get('COVSCREEN_BRIDGE_CONFIG'))
ROOT, DISCOVERY_RELEASE_ROOTS, SSH, DELIVERY = (CONFIG[key] for key in
    ('legacy_root', 'release_roots', 'ssh', 'delivery'))


def discovery_runtime(entrypoint='scripts/openscience_mcp_server.py'):
    """A missing pointer retains the legacy entry; an invalid one fails closed."""
    if entrypoint not in {'scripts/openscience_mcp_server.py', 'scripts/target_hypothesis_mcp.py'}:
        raise ValueError('Unregistered discovery entrypoint')
    if not ROOT:
        raise ValueError('Configure COVSCREEN_BRIDGE_CONFIG before connecting')
    pointer = Path(__file__).with_name('ligand-release.json')
    if not pointer.exists():
        return ROOT, ROOT + '/environment/activate_carsidock.sh', entrypoint
    value = json.loads(pointer.read_text())
    if not isinstance(value, dict):
        raise ValueError('Invalid Ligand-first runtime binding')
    identity, digest, data_root = (value.get(key) for key in ('release_id', 'manifest_sha256', 'data_root'))
    if (any(not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{64}', item)
            for item in (identity, digest))
            or not isinstance(data_root, str) or data_root not in DISCOVERY_RELEASE_ROOTS):
        raise ValueError('Invalid Ligand-first runtime binding')
    # Select an exact registered pair, never independently allow either root.
    # String equality also rejects traversal, normalization and cross-root aliases.
    release = DISCOVERY_RELEASE_ROOTS[data_root] + '/' + identity
    if value.get('release_root') != release:
        raise ValueError('Ligand-first release root differs from registered identity')
    program = (
        'import hashlib,json,pathlib,runpy\n'
        f'root=pathlib.Path({release!r})\n'
        "raw=(root/'manifest.json').read_bytes()\n"
        f"if hashlib.sha256(raw).hexdigest()!={digest!r}: raise ValueError('Release manifest changed')\n"
        "manifest=json.loads(raw)\n"
        "code=(root/'code_release.py').read_bytes()\n"
        "if hashlib.sha256(code).hexdigest()!=manifest['files']['code_release.py']: raise ValueError('Release verifier changed')\n"
        "namespace={'__name__':'release_verifier','__file__':str(root/'code_release.py')}\n"
        "exec(compile(code,str(root/'code_release.py'),'exec'),namespace)\n"
        f"namespace['verify'](root,{identity!r},{digest!r})\n"
        f"runpy.run_path(str(root/{('science/' + entrypoint)!r}),run_name='__main__')"
    )
    return data_root, release + '/science/environment/activate_carsidock.sh', '-B -c ' + shlex.quote(program)


def hypothesis_runtime():
    """Legacy overlays stay put; an explicit binding shares discovery's data root."""
    if not ROOT:
        raise ValueError('Configure COVSCREEN_BRIDGE_CONFIG before connecting')
    pointer = Path(__file__).with_name("hypothesis-release.json")
    if not pointer.exists():
        return ROOT, ROOT + '/environment/activate_carsidock.sh', 'scripts/target_hypothesis_mcp.py'
    value = json.loads(pointer.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid registered hypothesis release")
    if value.get('source') == 'discovery-release':
        if set(value) != {'source', 'data_root'}:
            raise ValueError('Ambiguous shared hypothesis binding')
        if not Path(__file__).with_name('ligand-release.json').is_file():
            raise ValueError('Shared hypothesis requires an immutable discovery release')
        runtime = discovery_runtime('scripts/target_hypothesis_mcp.py')
        if value['data_root'] != runtime[0]:
            raise ValueError('Hypothesis data root must match the registered discovery binding')
        return runtime
    if 'source' in value:
        raise ValueError('Unknown hypothesis source binding')
    version = value.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        raise ValueError("Invalid registered hypothesis release")
    explicit = "data_root" in value
    data_root = value.get("data_root", ROOT)
    if explicit and (not isinstance(data_root, str) or data_root not in DISCOVERY_RELEASE_ROOTS
            or discovery_runtime()[0] != data_root):
        raise ValueError("Hypothesis data root must match the registered discovery binding")
    release = data_root + "/releases/hypothesis-" + version
    program = (
        "import hashlib,json,pathlib,runpy\n"
        f"root=pathlib.Path({release!r})\n"
        "manifest=json.loads((root/'manifest.json').read_text())\n"
        f"if hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()!={version!r}: raise ValueError('Release manifest changed')\n"
        "if not all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in manifest.items()): raise ValueError('Release code changed')\n"
    )
    if explicit:
        # Historical launch.py embeds its base root, and its legacy MCP ignores
        # COVSCREEN_PROJECT_ROOT. Copying that overlay alone would split storage.
        # The new content-addressed overlay must bind its base code and data to
        # the exact registered destination; never rewrite a frozen launch here.
        program += (
            "import ast\n"
            "assignments=[node for node in ast.parse((root/'launch.py').read_text()).body "
            "if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='root' for target in node.targets)]\n"
            "if len(assignments)!=1: raise ValueError('Hypothesis launch root is not explicitly bound')\n"
            "binding=assignments[0].value\n"
            "if not (isinstance(binding,ast.Call) and isinstance(binding.func,ast.Name) "
            "and binding.func.id=='Path' and len(binding.args)==1 and not binding.keywords "
            "and isinstance(binding.args[0],ast.Constant) "
            f"and binding.args[0].value=={data_root!r}): raise ValueError('Hypothesis launch root differs from registered data root')\n"
        )
    program += "runpy.run_path(str(root/'launch.py'),run_name='__main__')"
    return data_root, data_root + '/environment/activate_carsidock.sh', "-c " + shlex.quote(program)


def hypothesis_command():
    return hypothesis_runtime()[2]


def relative_artifact(value, data_root=ROOT, *, allowed_roots=DISCOVERY_RELEASE_ROOTS):
    if data_root not in allowed_roots:
        raise ValueError("Unregistered hypothesis evidence storage")
    prefix = data_root + "/results/target-hypotheses/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("Artifact is outside hypothesis evidence storage")
    relative = value[len(prefix):]
    if not re.fullmatch(r"hyp-[a-f0-9]{24}/proposal-[a-f0-9]{12}/(?:hypothesis\.json|report\.md)", relative):
        raise ValueError("Unexpected hypothesis artifact path")
    return relative


def deliver(message, data_root=ROOT, *, delivery=DELIVERY, ssh=SSH, allowed_roots=DISCOVERY_RELEASE_ROOTS):
    for block in message.get("result", {}).get("content", []):
        if block.get("type") != "text":
            continue
        try:
            value = json.loads(block["text"])
        except (ValueError, TypeError):
            if message.get("result", {}).get("isError"):
                if block.get("text") == "'evidence_id'":
                    block["text"] += (
                        "\nMissing top-level evidence_id. For read/finalize pass the hyp- collection ID "
                        "returned by collect as evidence_id. Do not remove this argument when fixing citations."
                    )
                elif block.get("text", "").startswith("Unknown evidence IDs:"):
                    block["text"] += (
                        "\nThis error concerns hypothesis.claims[].evidence_ids or parent_evidence_ids, "
                        "NOT the top-level evidence_id. Keep the top-level hyp- collection ID. "
                        "Read the SAME collection and use exact source_index keys for citations "
                        "(only sources actually returned, such as PMID:... or PDB:...). "
                        "A hyp- collection ID is not a source citation. Do not recollect or drop evidence_id."
                    )
            continue
        # Tool output may be persisted and read with a per-line length limit.
        # Preserve all evidence, but keep source_index/schema accessible as lines
        # rather than hiding them after the sequence in one enormous JSON line.
        block["text"] = json.dumps(value, ensure_ascii=False, indent=2)
        if not isinstance(value, dict) or value.get("schema_version") != "target-hypothesis-v1" or "artifacts" not in value:
            continue
        if data_root not in allowed_roots:
            raise ValueError("Unregistered hypothesis evidence storage")
        originals = dict(value["artifacts"])
        # New De novo execution compilers replay the complete sealed collection,
        # not only the readable proposal. Transfer it without collecting again.
        if value.get('hypothesis', {}).get('route') == 'de-novo':
            from denovo_evidence_transfer import transfer
            value['evidence_transfer'] = transfer(value['evidence_id'], value['evidence_manifest_sha256'],
                data_root, delivery, ssh)
        records = {}
        for name, filename in originals.items():
            relative = relative_artifact(filename, data_root, allowed_roots=allowed_roots)
            # A failed transfer fails this tool response; it never replays finalize.
            data = subprocess.run(ssh + ["cat -- " + shlex.quote(filename)],
                check=True, stdout=subprocess.PIPE, timeout=45).stdout
            if len(data) > 20_000_000:
                raise ValueError("Hypothesis artifact exceeded transfer limit")
            destination = Path(delivery) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != data:
                    raise ValueError("Immutable local hypothesis artifact differs")
            else:
                with destination.open("xb") as stream:
                    stream.write(data)
            value["artifacts"][name] = str(destination)
            records[name] = {"source": filename, "path": str(destination),
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        value["artifact_transfer"] = records
        block["text"] = json.dumps(value, ensure_ascii=False, indent=2)
        if "structuredContent" in message["result"]:
            message["result"]["structuredContent"] = value
    return message


def route_runtime(route):
    if route not in {"target-hypothesis", "covalent-discovery"}:
        raise ValueError("Unknown fixed bridge route")
    return hypothesis_runtime() if route == 'target-hypothesis' else discovery_runtime()


def route_command(route, *, runtime=None):
    if route not in {"target-hypothesis", "covalent-discovery"}:
        raise ValueError("Unknown fixed bridge route")
    data_root, activation, entrypoint = runtime or route_runtime(route)
    return (f"source {shlex.quote(activation)} >/dev/null && "
        "unset TARGET_HYPOTHESIS_DELIVERY_ROOT && "
        f"export COVALENT_APPROVAL_KEY_FILE={shlex.quote(data_root + '/.secrets/native-approval.key')} && "
        f"export COVSCREEN_PROJECT_ROOT={shlex.quote(data_root)} && "
        "export TARGET_HYPOTHESIS_PLATFORM_TOOLS=inspire_screening_task && "
        f"cd {shlex.quote(data_root)} && exec python -u {entrypoint}")


def relay(command, data_root, *, ssh=SSH, delivery=DELIVERY, allowed_roots=DISCOVERY_RELEASE_ROOTS):
    if not ssh or delivery is None:
        raise ValueError('Configure COVSCREEN_BRIDGE_CONFIG before connecting')
    process = subprocess.Popen(ssh + [command], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def forward():
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    break
                process.stdin.write(chunk)
                process.stdin.flush()
        finally:
            process.stdin.close()

    threading.Thread(target=forward, daemon=True).start()
    for line in process.stdout:
        message = json.loads(line)
        try:
            # Keep delivery bound to the process that produced the response,
            # even if an operator activates a different pointer meanwhile.
            message = deliver(message, data_root, ssh=ssh, delivery=delivery, allowed_roots=allowed_roots)
        except Exception as error:
            message["result"] = {"isError": True, "content": [{"type": "text", "text":
                "Artifact delivery failed; remote result may already exist. Do not repeat finalize automatically. " + str(error)}]}
        print(json.dumps(message, ensure_ascii=False), flush=True)
    raise SystemExit(process.wait())


def main():
    route = sys.argv[1]
    runtime = route_runtime(route)
    command = route_command(route, runtime=runtime)
    if route == "covalent-discovery":
        os.execvp(SSH[0], SSH + [command])
    relay(command, runtime[0])


if __name__ == "__main__":
    main()
