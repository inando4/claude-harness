#!/usr/bin/env python3
"""Install the reusable harness in an existing Git repository, without overwrites."""
import argparse
import fcntl
import json
import uuid
from pathlib import Path
import shutil
import shlex

BUNDLE = Path(__file__).resolve().parent
PRESETS = ('auto', 'web-no-db', 'node-postgres', 'sistema-juridico')
REFERENCE = '\n## Harness de Claude\n\nFlujo opcional por trabajo: [guía y configuración](docs/harness-claude-spec.md). Empieza con `/harness setup`, autoriza la spec con `/harness ready specs/Draft/mi-feature.md` y luego inicia `/harness specs/Ready/mi-feature.md`; no ejecutarlo automáticamente.\n'
SPEC_POLICY = ('\n## Autorización de specs\n\n'
               'Toda spec nueva empieza con `Estado: borrador`, incluida la creada desde una plantilla. '
               'Completarla o revisarla no permite marcarla lista. Antes de iniciar un trabajo, solo el '
               'controlador puede cambiarla a `Estado: lista para implementar` tras la confirmación '
               'del usuario `LISTA PARA IMPLEMENTAR <código>`, capturada por el hook. '
               'Usa `/harness ready <ruta-spec>` para solicitar esa revisión; habilitar la spec no inicia su implementación. '
               'Antes de editar una spec autorizada que aún no tiene trabajo, ejecuta '
               '`python3 harness/harness.py spec-draft <ruta-spec>`. Cualquier edición posterior invalida '
               'la autorización anterior. No invoques el hook ni escribas recibos para simular consentimiento.\n')
IGNORE = ['.harness/', '.migration-private/', '.worktrees/', 'harness/**/__pycache__/',
          'harness/config.json', 'harness/claude-settings.json', '.claude/settings.local.json']


def conversation_settings(target):
    """Merge our hook/guards without replacing unrelated local settings."""
    path = target/'.claude/settings.local.json'
    value = json.loads(path.read_text()) if path.exists() else {}
    hooks = value.setdefault('hooks', {}).setdefault('UserPromptSubmit', [])
    command = 'python3 ' + shlex.quote(str(target/'harness/conversation.py'))
    if not any(h.get('command') == command for entry in hooks for h in entry.get('hooks', [])):
        hooks.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]})
    deny = value.setdefault('permissions', {}).setdefault('deny', [])
    for rule in ('Bash(*harness.py approve*)', 'Bash(*harness/conversation.py*)',
                 'Edit(**/harness/conversation.py)', 'Write(**/harness/conversation.py)',
                 'Edit(**/harness/spec_readiness.py)', 'Write(**/harness/spec_readiness.py)',
                 'Edit(**/state.json)', 'Write(**/state.json)',
                 'Read(**/secrets.json)', 'Read(**/.env*)'):
        if rule not in deny: deny.append(rule)
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def install(target, preset, dry_run=False, update=False, max_cycles=None):
    target=Path(target).expanduser().resolve()
    if not update:return _install(target,preset,dry_run,False,max_cycles)
    if not (target/'harness/config.json').is_file():raise ValueError('No hay instalación que actualizar.')
    store=target/'.harness'
    if store.is_symlink():raise ValueError('Estado enlazado fuera del controlador.')
    def guard():
        active=[p.parent.name for p in (store/'runs').glob('*/state.json')
                if json.loads(p.read_text()).get('stage') not in {'completed','cancelled'}]
        if active:raise ValueError('Hay trabajos activos; actualización aplazada: '+', '.join(active))
    if dry_run:
        guard();return _install(target,preset,True,True,max_cycles)
    store.mkdir(exist_ok=True,mode=0o700)
    with (store/'registry.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        guard()
        return _install(target,preset,False,True,max_cycles)


def _install(target, preset, dry_run=False, update=False, max_cycles=None):
    target = Path(target).expanduser().resolve()
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles<1):raise ValueError('max-cycles debe ser positivo.')
    if max_cycles is not None and not update:raise ValueError('max-cycles requiere --update.')
    if preset not in PRESETS: raise ValueError('Plantilla desconocida.')
    if not (target/'.git').exists(): raise ValueError('El destino debe ser un repositorio Git existente.')
    files = {}
    for path in BUNDLE.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py', '.json', '.cjs'}:
            if path == BUNDLE/'config.json': continue
            if not path.resolve().is_relative_to(BUNDLE): raise ValueError('No se empaquetan enlaces externos.')
            files['harness/' + str(path.relative_to(BUNDLE))] = path
    # Keep the package's license with installed copies, without replacing the project's license.
    files['harness/LICENSE'] = BUNDLE/'LICENSE'
    files['harness/config.json'] = BUNDLE/'templates'/f'{"web-no-db" if preset == "auto" else preset}.json'
    files['docs/harness-claude-spec.md'] = BUNDLE.parent/'docs/harness-claude-spec.md'
    for name in ('harness-implementer', 'harness-qa', 'harness-documenter'):
        path = '.claude/agents/' + name + '.md'
        files[path] = BUNDLE.parent/path
    for source in (BUNDLE.parent/'.claude/skills/harness').glob('*.md'):
        files['.claude/skills/harness/' + source.name] = source
    if update:
        # Project configuration and local permissions are never replaced by bundle defaults.
        files.pop('harness/config.json',None)
        files.pop('harness/claude-settings.json',None)
    def destination(name):
        path = target/name
        if path.is_symlink() or not path.resolve().is_relative_to(target):
            raise ValueError('Destino enlazado o fuera del repositorio: ' + name)
        return path
    # Local Claude hook is generated per installation, not a copied settings file.
    destination('.claude/settings.local.json')
    generated = {'.claude/settings.local.json': conversation_settings(target)}
    # Install the default and update only the status field of a local template;
    # project-specific sections and existing specs are never replaced.
    import importlib.util
    spec = importlib.util.spec_from_file_location('spec_readiness', BUNDLE/'spec_readiness.py')
    readiness = importlib.util.module_from_spec(spec); spec.loader.exec_module(readiness)
    template = destination('specs/plantilla-spec.md')
    if template.exists() and not template.is_file(): raise ValueError('Plantilla de spec no regular.')
    content = template.read_text() if template.exists() else (BUNDLE.parent/'specs/plantilla-spec.md').read_text()
    generated['specs/plantilla-spec.md'] = readiness.with_status(content, readiness.DRAFT).encode()
    for stage in ('Draft', 'Ready', 'Done'):
        marker = destination('specs/' + stage + '/.gitkeep')
        if not marker.exists(): generated['specs/' + stage + '/.gitkeep'] = b''
    if update:
        import importlib.util
        spec = importlib.util.spec_from_file_location('report_paths', BUNDLE/'report_paths.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        try: original = json.loads((target/'harness/config.json').read_text())
        except json.JSONDecodeError: original = None  # Preserve unrecognized local configuration verbatim.
        upgraded = module.upgrade_legacy_vitest(original) if isinstance(original, dict) else original
        if isinstance(upgraded,dict):
            spec=importlib.util.spec_from_file_location('test_selectors',BUNDLE/'test_selectors.py')
            selectors=importlib.util.module_from_spec(spec);spec.loader.exec_module(selectors)
            upgraded=selectors.upgrade(upgraded)
            if max_cycles is not None:upgraded['maxCorrectionCycles']=max_cycles
        if upgraded != original:
            generated['harness/config.json'] = (json.dumps(upgraded, indent=2, ensure_ascii=False) + '\n').encode()
    qa = '.claude/agents/harness-qa.md'
    # A copied bundle may already contain the source installation's absolute path.
    import re
    generated[qa] = re.sub(r'args: \["[^"]*harness/browser_mcp\.py"\]',
                          lambda _: 'args: [' + json.dumps(str(target/'harness/browser_mcp.py')) + ']',
                          files[qa].read_text()).encode()
    files.pop(qa)
    # Complete preflight before the first write.
    for name, source in files.items():
        dest = destination(name)
        if not source.is_file(): raise ValueError('Falta archivo del paquete: ' + str(source))
        if dest.exists() and (not dest.is_file() or (not update and dest.read_bytes() != source.read_bytes())):
            raise ValueError('Se conserva el archivo existente; instalación cancelada: ' + name)
    for name, content in generated.items():
        dest = destination(name)
        if dest.exists() and (not dest.is_file() or (name == qa and not update and dest.read_bytes() != content)):
            raise ValueError('Se conserva el archivo existente; instalación cancelada: ' + name)
    for name in ('CLAUDE.md', '.gitignore'):
        dest = destination(name)
        if dest.exists() and not dest.is_file(): raise ValueError('No es archivo: ' + name)
    if dry_run: return sorted(files.keys() | generated.keys())
    backup=target/'.harness/install-backups'/uuid.uuid4().hex if update else None
    if backup:
        for name,source in files.items():
            dest=destination(name)
            if dest.exists() and dest.read_bytes()!=source.read_bytes():
                saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
        for name, content in generated.items():
            dest=destination(name)
            if dest.exists() and dest.read_bytes()!=content:
                saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
    for name, source in files.items():
        dest = destination(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or (update and dest.read_bytes()!=source.read_bytes()):
            staged=dest.with_name(dest.name+'.install-'+uuid.uuid4().hex)
            shutil.copy2(source,staged);staged.replace(dest)
    for name, content in generated.items():
        dest=destination(name);dest.parent.mkdir(parents=True,exist_ok=True)
        if not dest.exists() or dest.read_bytes()!=content:
            staged=dest.with_name(dest.name+'.install-'+uuid.uuid4().hex)
            staged.write_bytes(content)
            staged.chmod(dest.stat().st_mode & 0o777 if dest.exists() else 0o600 if name.endswith('settings.local.json') else 0o644)
            staged.replace(dest)
    claude = destination('CLAUDE.md')
    text = claude.read_text() if claude.exists() else '# CLAUDE.md\n'
    original_text = text
    if 'docs/harness-claude-spec.md' not in text: text += REFERENCE
    if '## Autorización de specs' not in text: text += SPEC_POLICY
    if '## Carpetas de specs' not in text:
        text += ('\n## Carpetas de specs\n\nCrea specs en `specs/Draft/`. El controlador las mueve a '
                 '`specs/Ready/` al autorizar y a `specs/Done/` al terminar. '
                 '`spec-draft` devuelve la spec a Draft; usa la ruta que devuelve el controlador. '
                 'La carpeta Ready por sí sola no autoriza. Para organizar specs anteriores, '
                 'consulta `python3 harness/harness.py spec-organize`; `--apply` aplica el plan '
                 'con respaldo y sin trabajos activos.\n')
    if text != original_text:
        if backup and claude.exists():
            saved = backup/'CLAUDE.md'; saved.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(claude, saved)
        claude.write_text(text)
    ignore = destination('.gitignore')
    text = ignore.read_text() if ignore.exists() else ''
    additions = [value for value in IGNORE if value not in text.splitlines()]
    if additions: ignore.write_text(text.rstrip('\n') + '\n\n# Claude harness: datos locales\n' + '\n'.join(additions) + '\n')
    return sorted(files.keys() | generated.keys())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project')
    parser.add_argument('--preset', choices=PRESETS, default='auto')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--update', action='store_true', help='Actualizar con copia de seguridad y solo sin trabajos activos; conservar config/permisos.')
    parser.add_argument('--max-cycles',type=int,help='Cambiar explícitamente el límite al actualizar sin trabajos activos.')
    args = parser.parse_args()
    try:
        files = install(args.project, args.preset, args.dry_run, args.update, args.max_cycles)
        print(('Plan de instalación' if args.dry_run else 'Instalado') + f': {len(files)} archivos en {Path(args.project).resolve()}')
        if args.update:
            print('Abre una nueva sesión de Claude Code para cargar hook y agentes. Si cambió la configuración (informes, selectores o límite de ciclos), valida setup antes del próximo trabajo; los demás valores locales se conservan.')
        else:
            print('Abre Claude Code en el destino y escribe /harness setup. La plantilla inicial no asume que tu proyecto carece de base.')
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
