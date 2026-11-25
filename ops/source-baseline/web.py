"""Prepare and start the new checkout with external, isolated acceptance state.

No production service is changed. Start never stops/replaces an existing listener.
Provider credentials stay in the owner-only external config, never in receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[2]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        os.chmod(path, 0o600)
        stream.write(value)


def prepare(args):
    root = Path(args.runtime).expanduser().resolve()
    if root.is_relative_to(SOURCE) or SOURCE.is_relative_to(root):
        raise ValueError("Runtime must be separate from the source checkout")
    config = root / "config/openscience.json"
    if config.exists():
        raise ValueError("Runtime already configured; existing settings are never overwritten")
    python = Path(args.python).absolute()
    bun = Path(args.bun).resolve(strict=True)
    if subprocess.check_output([str(bun), "--version"], text=True).strip() != "1.3.14":
        raise ValueError("Bun 1.3.14 required by packageManager")
    subprocess.run([str(python), "-c", "import covscreen, covscreen_library_first, covscreen_ligand_first, covscreen_de_novo"], check=True)
    for name in ("config", "data", "cache", "state", "home", "logs", "tools", "configs", "science-data/configs", "projects/acceptance"):
        (root / name).mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    target = root / "tools/bun"
    shutil.copyfile(bun, target)
    target.chmod(0o755)
    if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(bun.read_bytes()).digest():
        raise ValueError("Standalone Bun copy differs")
    key = root / "config/native-approval.key"
    write(key, secrets.token_hex(32) + "\n")
    token = secrets.token_hex(32)
    write(root / "config/connector.key", token + "\n")
    value = {"snapshot": False, "autoupdate": False, "plugin": [],
             "skills": {"paths": [str(SOURCE / "openscience-custom/backend/cli/integrations/design-route-skills/skills"),
                                  str(SOURCE / "openscience-custom/backend/cli/integrations/library-first-skills/skills")]},
             "mcp": {"covalent-discovery": {"type": "remote", "enabled": True, "timeout": 240000,
                 "url": f"http://127.0.0.1:{args.science_port}/covalent-discovery", "oauth": False,
                 "headers": {"Authorization": "Bearer " + token}}}}
    if args.provider_config:
        # Deliberately excludes plugins, MCP registrations, skills and data pointers.
        original = json.loads(Path(args.provider_config).read_text())
        for field in ("provider", "model", "small_model", "billing", "sandbox"):
            if field in original:
                value[field] = original[field]
    write(config, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    write(root / "binding.json", json.dumps({"source": str(SOURCE), "python": str(python),
        "bun": str(target), "backend_port": args.backend_port, "web_port": args.web_port,
        "science_port": args.science_port,
        "project": str(root / "projects/acceptance")}, indent=2) + "\n")
    subprocess.run(["git", "init", "-b", "acceptance", str(root / "projects/acceptance")], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({"prepared": True, "runtime": str(root), "providers": list(value.get("provider", {})),
                      "scientific_runtime_configured": False, "source": str(SOURCE)}, ensure_ascii=False))


def settings(args):
    root = Path(args.runtime).expanduser().resolve(strict=True)
    binding = json.loads((root / "binding.json").read_text())
    if binding["source"] != str(SOURCE):
        raise ValueError("Runtime belongs to a different source checkout")
    env = dict(os.environ)
    # Explicit roots prevent fallback to production XDG directories or ~/.openscience.
    env.update(OPENSCIENCE_TEST_HOME=str(root / "home"), OPENSCIENCE_CONFIG_DIR=str(root / "config"),
               OPENSCIENCE_DATA_DIR=str(root / "data"), XDG_CONFIG_HOME=str(root / "xdg-config"),
               XDG_DATA_HOME=str(root / "xdg-data"), XDG_CACHE_HOME=str(root / "cache"), XDG_STATE_HOME=str(root / "state"),
               COVALENT_RUNTIME_ROOT=str(root), COVALENT_PIPELINE_ROOT=str(SOURCE / "science"),
               COVALENT_POSTPROCESS_PYTHON=binding["python"], OPENSCIENCE_RDKIT_PYTHON=binding["python"],
               COVALENT_APPROVAL_KEY_FILE=str(root / "config/native-approval.key"),
               OPENSCIENCE_DISABLE_PROJECT_CONFIG="true", OPENSCIENCE_DISABLE_DEFAULT_PLUGINS="true",
               OPENSCIENCE_DISABLE_LSP_DOWNLOAD="true", OPENSCIENCE_DISABLE_SHARE="true",
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               VITE_OPENSCIENCE_SERVER_URL=f"http://127.0.0.1:{binding['backend_port']}",
               PATH=str(root / "tools") + os.pathsep + os.environ.get("PATH", ""))
    for name in ("OPENSCIENCE_CONFIG", "OPENSCIENCE_CONFIG_CONTENT", "OPENSCIENCE_RESEARCH_HOST_CATALOG_SHA256"):
        env.pop(name, None)
    return root, binding, env


def start(args):
    root, binding, env = settings(args)
    selected = ("science", "backend", "web") if args.component == "all" else (args.component,)
    for port in (binding[name + "_port"] for name in selected):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise ValueError(f"Port {port} already occupied; existing service left running")
    cli = SOURCE / "openscience-custom/backend/cli/src/index.ts"
    web = SOURCE / "openscience-custom/frontend/workspace"
    commands = [("science", [binding["bun"], "--no-env-file", "run", str(SOURCE / "ops/source-baseline/connector.ts")], binding["project"]),
                ("backend", [binding["bun"], "--no-env-file", "run", "--conditions=browser", str(cli),
                 "serve", "--port", str(binding["backend_port"])], binding["project"]),
                ("web", [binding["bun"], "--no-env-file", "run", "dev", "--host", "127.0.0.1", "--port", str(binding["web_port"]), "--strictPort"], str(web))]
    receipt = {"source": str(SOURCE), "started": time.time(), "processes": {}}
    for name, command, cwd in commands:
        if name not in selected:
            continue
        with (root / f"logs/{name}.log").open("ab", buffering=0) as log:
            child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        receipt["processes"][name] = {"pid": child.pid, "command": command}
    (root / f"processes-{args.component}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"started": receipt["processes"], "url": f"http://127.0.0.1:{binding['web_port']}",
                      "health_verified": False}))


def status(args):
    root, binding, _ = settings(args)
    result = {}
    for name, url in (("backend", f"http://127.0.0.1:{binding['backend_port']}/global/health"),
                      ("web", f"http://127.0.0.1:{binding['web_port']}/"),
                      ("science", f"http://127.0.0.1:{binding['science_port']}/health")):
        try:
            request = urllib.request.Request(url)
            if name == "science":
                request.add_header("Authorization", "Bearer " + (root / "config/connector.key").read_text().strip())
            with urllib.request.urlopen(request, timeout=10) as response:
                result[name] = {"status": response.status, "url": url}
        except Exception as error:
            result[name] = {"error": str(error), "url": url}
    healthy = all(item.get("status") == 200 for item in result.values())
    required = ("configs/inspire-executor.json", "configs/inspire-refinement.json", "science-data/configs/local.inspire.yaml")
    print(json.dumps({"services": result, "services_healthy": healthy,
        "missing_execution_configs": [name for name in required if not (root / name).is_file()],
        "scope": "Service health and configuration presence only; not scientific execution acceptance"}, ensure_ascii=False))
    return 0 if healthy else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start", "status"))
    parser.add_argument("--runtime", default=str(SOURCE.parent / "covalent-agent-runtime"))
    parser.add_argument("--python")
    parser.add_argument("--bun")
    parser.add_argument("--provider-config")
    parser.add_argument("--backend-port", type=int, default=4300)
    parser.add_argument("--web-port", type=int, default=4445)
    parser.add_argument("--science-port", type=int, default=4301)
    parser.add_argument("--component", choices=("all", "science", "backend", "web"), default="all")
    args = parser.parse_args()
    if args.action == "prepare" and not (args.python and args.bun):
        parser.error("prepare requires --python and --bun")
    return {"prepare": prepare, "start": start, "status": status}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
