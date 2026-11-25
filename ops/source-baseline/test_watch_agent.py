import io
import json
import unittest
from watch_agent import Monitor, consume


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.monitor = Monitor(emit=self.lines.append)

    def tool(self, status, **time):
        return {'type': 'message.part.updated', 'properties': {'part': {
            'id': 'p', 'sessionID': 'session1', 'type': 'tool', 'tool': 'status_tool',
            'state': {'status': status, 'input': {'action': 'status'}, 'time': time}}}}

    def test_compacted_history_is_not_presented_as_new_execution(self):
        self.monitor.handle(self.tool('completed', start=1, end=2, compacted=3))
        self.assertEqual(self.lines, [])

    def test_tool_duplicates_are_suppressed_but_lifecycle_retained(self):
        for state in ('pending', 'running', 'running', 'completed', 'completed'):
            self.monitor.handle(self.tool(state))
        self.assertEqual(len(self.lines), 2)
        self.assertIn('工具开始', self.lines[0])
        self.assertIn('工具返回', self.lines[1])

    def test_answer_shown_once_after_completion(self):
        for time in ({'start': 1}, {'start': 1, 'end': 2}, {'start': 1, 'end': 2}):
            self.monitor.handle({'type': 'message.part.updated', 'properties': {'part': {
                'id': 'text1', 'sessionID': 's', 'type': 'text', 'text': '最终回答', 'time': time}}})
        self.assertEqual(len(self.lines), 1)

    def test_raw_stream_preserved_and_error_visible(self):
        event = {'type': 'session.error', 'properties': {'sessionID': 's', 'error': {
            'data': {'statusCode': 402, 'message': 'Insufficient Balance'}}}}
        original = ('data: '+json.dumps(event)+'\n\ndata: {"type":"server.heartbeat"}\n\n').encode()
        raw = io.BytesIO()
        consume(io.BytesIO(original), self.monitor, raw)
        self.assertEqual(raw.getvalue(), original)
        self.assertEqual(len(self.lines), 1)
        self.assertIn('402 Insufficient Balance', self.lines[0])

    def test_session_filter_and_duplicate_idle(self):
        monitor = Monitor(session='wanted', emit=self.lines.append)
        monitor.handle(self.tool('running'))
        for kind in ('session.status', 'session.idle'):
            monitor.handle({'type': kind, 'properties': {'sessionID': 'wanted', 'status': {'type': 'idle'}}})
        self.assertEqual(len(self.lines), 1)
        self.assertIn('本轮停止', self.lines[0])

    def test_permission_is_visible_once(self):
        event = {'type': 'permission.asked', 'properties': {'id': 'permit', 'sessionID': 's', 'permission': 'mcp', 'patterns': ['tool']}}
        self.monitor.handle(event)
        self.monitor.handle(event)
        self.assertEqual(len(self.lines), 1)
        self.assertIn('等待授权', self.lines[0])


if __name__ == '__main__':
    unittest.main()
