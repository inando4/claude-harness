"""The job graph is derived from code; a dedup-only shrink does not block the job."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_harness as existing

h = existing.h
FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_GRAPHIFY_CALLS'], 'a') as f: f.write(json.dumps(args) + '\\n')
if args == ['--version']: print('graphify 0.0-test'); sys.exit(0)
if args[0] == 'query': print('entry point'); sys.exit(0)
mode = os.environ['FAKE_GRAPHIFY_MODE']
if mode == 'dedup' and '--allow-dedup-shrink' not in args:
    print('[graphify extract] error: dedup merged 3 node(s); writing would shrink the graph from 10 nodes to 7 nodes. '
          'Refusing to overwrite graphify-out/graph.json. Pass --allow-dedup-shrink to write the smaller graph.', file=sys.stderr)
    sys.exit(1)
if mode == 'broken':
    print('error: extraction crashed', file=sys.stderr); sys.exit(1)
pathlib.Path('graphify-out').mkdir(exist_ok=True)
pathlib.Path('graphify-out/graph.json').write_text(json.dumps({'nodes': [], 'edges': []}))
'''


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.fx = existing.HarnessTests(); self.fx.setUp()
        self.s = self.fx.s
        self.bin = tempfile.TemporaryDirectory(); tool = Path(self.bin.name)/'graphify'
        tool.write_text(FAKE); tool.chmod(0o755)
        self.calls = Path(self.bin.name)/'calls.jsonl'

    def tearDown(self):
        h._execution = None; self.bin.cleanup(); self.fx.tearDown()

    def graph(self, mode):
        env = {'PATH': self.bin.name + os.pathsep + os.environ['PATH'], 'FAKE_GRAPHIFY_MODE': mode,
               'FAKE_GRAPHIFY_CALLS': str(self.calls)}
        with patch.dict(os.environ, env): h.graph(self.s)

    def extractions(self):
        return [args for args in map(json.loads, self.calls.read_text().splitlines()) if args[0] == '.']

    def test_dedup_shrink_refusal_is_accepted_and_recorded(self):
        self.graph('dedup')
        self.assertEqual(self.extractions(), [['.', '--code-only'], ['.', '--code-only', '--allow-dedup-shrink']])
        self.assertIn('main', self.s['graphs'])
        step = [row for row in self.s['steps'] if row['id'] == 'graph-main'][-1]
        self.assertEqual(step['status'], 'passed'); self.assertIn('duplicados', step['reason'])

    def test_other_graphify_failures_still_block_without_retry(self):
        with self.assertRaises(h.Blocked): self.graph('broken')
        self.assertEqual(self.extractions(), [['.', '--code-only']])
        self.assertNotIn('main', self.s.get('graphs', {}))

    def test_clean_extraction_runs_once(self):
        self.graph('clean')
        self.assertEqual(self.extractions(), [['.', '--code-only']])
        self.assertEqual([row['reason'] for row in self.s['steps'] if row['id'] == 'graph-main'][-1], 'grafo actualizado')


if __name__ == '__main__': unittest.main()
