"""Keep check evidence out of directories that Playwright clears on startup."""
from copy import deepcopy
from pathlib import Path
import re


def upgrade_legacy_vitest(cfg):
    """Migrate only the harness's known Vitest argument/report pair, not user paths."""
    result = deepcopy(cfg)
    old = 'test-results/harness-unit.xml'
    new = '.harness/reports/harness-unit.xml'
    for step in result.get('checks', []):
        argument = '--outputFile.junit=' + old
        if argument not in step.get('command', []):
            continue
        repo = step.get('repo', result.get('primaryRepository'))
        prefix = Path(step.get('cwd', '.'))
        matched = []
        for name in step.get('reports', []):
            report = result.get('testReports', {}).get(name)
            report_repo = report.get('repo', result.get('primaryRepository')) if isinstance(report, dict) else result.get('primaryRepository')
            pattern = report.get('glob') if isinstance(report, dict) else report
            if repo == report_repo and pattern == str(prefix / old):
                matched.append(name)
        if not matched:
            continue
        step['command'] = [('--outputFile.junit=' + new) if v == argument else v for v in step['command']]
        for name in matched:
            report = result['testReports'][name]
            if isinstance(report, dict): report['glob'] = str(prefix / new)
            else: result['testReports'][name] = str(prefix / new)
        policy = result.get('publication', {}).get('repositories', {}).get(repo)
        if policy is not None:
            path = str(prefix / '.harness/reports')
            if path not in policy.setdefault('disposablePaths', []): policy['disposablePaths'].append(path)
    return result


def validate(h, s=None):
    """Detect default/literal outputDir collisions without executing project JS."""
    cfg = h.config()
    for name in ('e2e', 'apiE2E'):
        suite = cfg.get(name, {})
        if not suite or suite.get('enabled', True) is False or suite.get('runner') != 'playwright-test':
            continue
        tree = h.repository_root(s, suite.get('repo'))
        cwd = h.safe_path(tree, suite.get('cwd', '.'))
        outputs = {'test-results'}
        if suite.get('outputDir'): outputs.add(suite['outputDir'])
        command = suite.get('command', [])
        for i, arg in enumerate(command):
            if arg.startswith('--output='): outputs.add(arg.split('=', 1)[1])
            if arg == '--output' and i + 1 < len(command): outputs.add(command[i + 1])
        for config in cwd.glob('playwright.config.*'):
            outputs.update(re.findall(r'\boutputDir\s*:\s*[\x27\x22]([^\x27\x22]+)[\x27\x22]', config.read_text()))
        roots = [(cwd / output).resolve() for output in outputs]
        for report_name, report in cfg.get('testReports', {}).items():
            repo = report.get('repo') if isinstance(report, dict) else None
            pattern = report.get('glob', '') if isinstance(report, dict) else report
            path = (h.repository_root(s, repo) / pattern).resolve()
            # A report glob under a cleaned directory is unsafe as well.
            h.require(not any(path == root or path.is_relative_to(root) for root in roots),
                      f'{report_name}: el informe de checks comparte salida con {name}/Playwright; '
                      'muévelo junto con su comando a .harness/reports/, fuera de test-results/outputDir.')
