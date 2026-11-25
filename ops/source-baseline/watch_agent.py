"""Read-only, compact SSE monitor for the existing platform backend."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request


def clean(value):
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(value))


class Monitor:
    def __init__(self, session=None, emit=print):
        self.session = session
        self.emit = emit
        self.seen = set()
        self.statuses = {}

    def line(self, label, sid, value):
        self.emit(f'[{label}] [{(sid or "服务")[-8:]}] {clean(value)}')

    def handle(self, event):
        kind = event.get('type', '')
        props = event.get('properties') or {}
        part = props.get('part') or {}
        sid = props.get('sessionID') or part.get('sessionID') or (props.get('info') or {}).get('sessionID')
        if self.session and sid != self.session:
            return
        if kind == 'message.part.updated':
            if part.get('type') == 'tool':
                state = part.get('state') or {}
                if (state.get('time') or {}).get('compacted') is not None:
                    return
                status = state.get('status')
                if status not in ('running', 'completed', 'error'):
                    return
                key = (sid, part.get('id') or part.get('callID'), status)
                if key in self.seen:
                    return
                self.seen.add(key)
                tool = part.get('tool', '').removeprefix('covalent-discovery_')
                action = (state.get('input') or {}).get('action', '')
                label = {'running': '工具开始', 'completed': '工具返回', 'error': '工具错误'}[status]
                detail = f'{tool} {action}'.strip()
                if status == 'error':
                    detail += ' — ' + clean(state.get('error', '未知错误'))[:700]
                self.line(label, sid, detail)
            elif part.get('type') == 'text' and (part.get('time') or {}).get('end'):
                key = (sid, part.get('id'), 'text')
                if key not in self.seen:
                    self.seen.add(key)
                    value = part.get('text', '')
                    self.line('回答', sid, value[:1600] + ('\n…完整内容见网页或原始日志。' if len(value)>1600 else ''))
        elif kind == 'permission.asked':
            key = (sid, props.get('id'), 'permission')
            if key not in self.seen:
                self.seen.add(key)
                self.line('等待授权', sid, f'{props.get("permission", "")} {", ".join(props.get("patterns", []))}；请在网页核对后处理')
        elif kind == 'session.error':
            error = props.get('error') or {}
            data = error.get('data') or {}
            value = f'{data.get("statusCode", "")} {data.get("message") or error.get("name", "未知错误")}'.strip()
            key = (sid, value, 'error')
            if key not in self.seen:
                self.seen.add(key)
                self.line('会话错误', sid, value[:1000])
        elif kind in ('session.status', 'session.idle'):
            status = 'idle' if kind == 'session.idle' else (props.get('status') or {}).get('type')
            if status and self.statuses.get(sid) != status:
                self.statuses[sid] = status
                self.line({'busy': '处理中', 'idle': '本轮停止', 'retry': '重试等待'}.get(status, '状态'), sid,
                          '是否成功请看回答及工具结果' if status == 'idle' else status)
        elif kind == 'message.updated':
            info = props.get('info') or {}
            if info.get('role') == 'assistant':
                key = (sid, info.get('id'), 'model')
                if key not in self.seen:
                    self.seen.add(key)
                    self.line('模型', sid, f'{info.get("providerID", "")}/{info.get("modelID", "")}')


def consume(stream, monitor, raw=None):
    data = []
    def dispatch():
        if data:
            try:
                event = json.loads('\n'.join(data))
                if isinstance(event, dict): monitor.handle(event)
            except json.JSONDecodeError:
                monitor.emit('[解析提示] 一条非完整 JSON 事件未展示；请查原始记录。')
            data.clear()
    for line in stream:
        if raw:
            raw.write(line)
            raw.flush()
        text = line.decode('utf-8', errors='replace').rstrip('\r\n')
        if not text:
            dispatch()
        elif text.startswith('data:'):
            data.append(text[5:].lstrip(' '))
    dispatch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:4311')
    parser.add_argument('--directory', default=str(Path(__file__).resolve().parents[3]/'covalent-agent-runtime/rc2/projects/acceptance'))
    parser.add_argument('--session', help='Only display this session; raw recording still includes the project event stream')
    parser.add_argument('--replay', type=Path, help='Read a saved SSE log instead of connecting')
    args = parser.parse_args()
    monitor = Monitor(args.session, lambda text: print(text, flush=True))
    if args.replay:
        with args.replay.open('rb') as stream: consume(stream, monitor)
        return
    folder = Path(__file__).resolve().parents[3]/'outputs/agent-monitor'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/('events-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sse')
    request = urllib.request.Request(args.url.rstrip('/')+'/event?'+urllib.parse.urlencode({'directory': args.directory}), headers={'Accept': 'text/event-stream'})
    try:
        with urllib.request.urlopen(request, timeout=60) as stream:
            fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as raw:
                print(f'[已连接] 原始事件保存至 {path}\n[说明] 工具返回不代表科研检查通过；Ctrl+C 只退出监听。', flush=True)
                consume(stream, monitor, raw)
            print('[连接结束] 未取消或重新提交任何任务。', flush=True)
    except KeyboardInterrupt:
        print('\n[监听停止] 后端任务不受影响。')
    except (OSError, ValueError) as error:
        print(f'[连接错误] {clean(error)}；本程序未提交任务。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
