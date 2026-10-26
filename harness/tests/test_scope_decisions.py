"""Scope decisions are typed by the user and reach implementer, QA and the PR."""
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import test_harness as existing
import test_publication as publication

h = existing.h
TEXT = 'AC-33: solo cuentan los valores de las listas cerradas; otras redacciones son limitación conocida'


def decide(s, text=TEXT, typed=None, tty=True):
    typed = typed or 'DECIDIR ' + s['id']
    with patch('sys.stdin.isatty', return_value=tty), patch('builtins.input', return_value=typed), \
            contextlib.redirect_stdout(io.StringIO()):
        h.decision(s, text)


class ScopeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fx = existing.HarnessTests(); self.fx.setUp(); self.s = self.fx.s

    def tearDown(self): self.fx.tearDown()

    def test_user_decision_reaches_brief_and_qa_context(self):
        self.s['cycles'] = 16
        decide(self.s, '  ' + TEXT.replace(' ', '\n', 1))
        decide(self.s, 'AC-07: «hasta cuándo» solo con la prueba o la postulación')
        saved = h.load(self.s['id'])['scopeDecisions']
        self.assertEqual([(d['id'], d['cycle']) for d in saved], [('D-1', 16), ('D-2', 16)])
        self.assertEqual(saved[0]['text'], TEXT)
        self.assertEqual(h.brief(self.s)['scopeDecisions'], saved)
        self.s['qaVersion'] = 'v1'
        h.runtime_module('qa_context').capture(h.CoreContext(), self.s)
        self.assertEqual(self.s['qaContext']['scopeDecisions'], saved)

    def test_decision_needs_the_user_terminal_and_exact_confirmation(self):
        for kwargs in ({'tty': False}, {'typed': 'DECIDIR otro'}):
            with self.assertRaises(h.Blocked): decide(self.s, **kwargs)
        self.assertNotIn('scopeDecisions', h.load(self.s['id']))

    def test_decision_is_closed_after_approval(self):
        self.s['stage'] = 'documenting'; self.s['approval'] = {'version': 'v1'}
        with self.assertRaises(h.Blocked): decide(self.s)
        self.s['stage'] = 'blocked'
        with self.assertRaises(h.Blocked): decide(self.s)
        self.assertNotIn('scopeDecisions', self.s)


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        self.fx = publication.PublicationTests(); self.fx.setUp()

    def tearDown(self): self.fx.tearDown()

    def test_pr_body_lists_scope_decisions(self):
        s = self.fx.s; github = h.runtime_module('github'); commands = []
        s['scopeDecisions'] = [{'id': 'D-1', 'text': TEXT, 'cycle': 16, 'at': 0}]
        s['publication']['repos']['back']['github'] = {'repository': 'owner/repo', 'base': 'main'}
        listed = lambda ctx, endpoint, key: [{'number': 7}] if commands else []
        with patch.object(github, 'pages', side_effect=listed), patch.object(github, 'validate_pr'), \
                patch.object(github, 'api', return_value={'number': 7, 'html_url': 'https://example.invalid/7'}), \
                patch.object(h, 'run', side_effect=lambda argv, **kwargs: commands.append(argv)):
            github.ensure_pr(h.CoreContext(), s, 'back')
        body = Path(commands[0][commands[0].index('--body-file') + 1]).read_text()
        self.assertIn('Decisiones de alcance del usuario', body)
        self.assertIn('- D-1 (ciclo 16): ' + TEXT, body)


if __name__ == '__main__': unittest.main()
