"""Preserve nested JUnit coverage in broad Maven CLI class selectors."""
from copy import deepcopy
from pathlib import Path
import re


def upgrade(cfg):
    result=deepcopy(cfg)
    for phase in ('checks','integrationChecks'):
        for step in result.get(phase,[]):
            command=step.get('command',[])
            if not command or Path(command[0]).name not in {'mvn','mvnw'}:continue
            for i,arg in enumerate(command):
                if not arg.startswith(('-Dtest=','-Dit.test=')):continue
                flag,value=arg.split('=',1);patterns=value.split(',')
                for pattern in tuple(patterns):
                    nested=pattern in {'*Test','*Tests','*IT','*ITCase'} or bool(re.fullmatch(r'![A-Za-z0-9_.*?/]+(?:Test|Tests|IT|ITCase)',pattern))
                    if nested and pattern+'$*' not in patterns:patterns.append(pattern+'$*')
                command[i]=flag+'='+','.join(patterns)
    return result


def validate(h):
    h.require(upgrade(h.config())==h.config(),
              'El selector Maven global excluye @Nested: incluye *Test$*, *Tests$* o *IT$* junto a sus patrones. Conserva exclusiones explícitas.')
