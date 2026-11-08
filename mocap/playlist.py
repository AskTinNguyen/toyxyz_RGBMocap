"""Project playlists: an ordered list of saved projects to review one after another.

A playlist is a small standalone JSON file chosen by the user (or the last one remembered in
config/local.json). It only references project folders; it is never written into a project and
never changes a project's motion, settings or identity.

File format (version 1):
    {"rgbmocap_playlist": 1, "items": [{"project": "<folder>", "model": "exmr", "label": "..."}]}
`project` is a workspace root (the folder with the top-level project.json) or one of its model
folders. `model` (optional) selects a model folder inside a workspace root. Relative paths are
resolved against the playlist file's folder, so a playlist can travel with its projects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

VERSION = 1
KEY = 'rgbmocap_playlist'
MAX_BYTES = 256 * 1024
MAX_ITEMS = 500
MODELS = ('exmr', 'flowhmr', 'gvhmr', 'mamma', 'prompthmr')
SETTINGS_KEY = 'playlist'


def make_item(project, model=None, label=None):
    item = {'project': str(project)}
    if model:
        if model not in MODELS:
            raise ValueError('Unknown model folder: ' + str(model))
        item['model'] = model
    if label:
        item['label'] = str(label)[:200]
    return item


def validate_item(value):
    if not isinstance(value, dict) or not isinstance(value.get('project'), str) or not value['project'].strip():
        raise ValueError('Each playlist item needs a project folder.')
    return make_item(value['project'], value.get('model') or None, value.get('label') or None)


def target(item, base=None):
    """Folder to open for an item: the model folder when a model is named, else the project folder."""
    path = Path(item['project'])
    if not path.is_absolute() and base is not None:
        path = Path(base) / path
    path = path.resolve()
    model = item.get('model')
    if model and (path / model / 'project.json').exists():
        return path / model
    return path


def describe(item):
    if item.get('label'):
        return item['label']
    path = Path(item['project'])
    name = path.name
    for suffix in ('_project',):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    parts = name.rsplit('_', 1)
    if len(parts) == 2 and len(parts[1]) == 14 and all(c in '0123456789abcdef' for c in parts[1]):
        name = parts[0]  # drop the app's 14-character hash suffix
    return name + (' / ' + item['model'] if item.get('model') else '')


def load(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Playlist file is too large.')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get(KEY) != VERSION or not isinstance(value.get('items'), list):
        raise ValueError('Not an RGB MoCap playlist (version %d).' % VERSION)
    if len(value['items']) > MAX_ITEMS:
        raise ValueError('Playlist has more than %d items.' % MAX_ITEMS)
    return [validate_item(item) for item in value['items']]


def save(path, items):
    from .storage import atomic_json
    path = Path(path)
    if len(items) > MAX_ITEMS:
        raise ValueError('Playlist has more than %d items.' % MAX_ITEMS)
    base = path.parent.resolve()
    out = []
    for item in items:
        item = validate_item(item)
        project = Path(item['project']).resolve()
        try:  # keep paths next to or below the playlist relative so the folder can move as a whole
            rel = os.path.relpath(project, base)
            if not rel.startswith('..') and not os.path.isabs(rel):
                item['project'] = rel
        except ValueError:  # different drive
            pass
        out.append(item)
    value = {KEY: VERSION, 'items': out}
    if len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_BYTES:
        raise ValueError('Playlist file is too large.')
    atomic_json(path, value)


def resolve_items(items, base):
    """Make relative item paths absolute against the playlist file's folder."""
    out = []
    for item in items:
        item = dict(item)
        path = Path(item['project'])
        if not path.is_absolute():
            item['project'] = str((Path(base) / path).resolve())
        out.append(item)
    return out


def neighbour(index, count, step, wrap=False):
    """Index after moving `step` from `index` in a list of `count` items; None past either end unless wrap."""
    if count <= 0:
        return None
    if index is None or index < 0:
        return 0 if step >= 0 else count - 1
    nxt = index + step
    if wrap:
        return nxt % count
    return nxt if 0 <= nxt < count else None


def discover(folder, depth=2, limit=MAX_ITEMS):
    """Workspace roots at most `depth` levels below `folder`, sorted by name. Model folders are not listed
    separately (their workspace root is)."""
    from .project_workspace import is_workspace
    folder = Path(folder)
    found = []

    def walk(path, level):
        if len(found) >= limit:
            return
        if is_workspace(path):
            found.append(make_item(path.resolve()))
            return
        if level >= depth:
            return
        try:
            children = sorted((p for p in path.iterdir() if p.is_dir() and not p.name.startswith('.')), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children:
            walk(child, level + 1)

    walk(folder, 0)
    return found


def remembered(settings):
    """Last playlist file and auto-advance choice from local settings (config/local.json)."""
    value = settings.get(SETTINGS_KEY) if isinstance(settings, dict) else None
    if not isinstance(value, dict):
        return None, True
    file = value.get('file') if isinstance(value.get('file'), str) else None
    auto = value.get('auto_advance', True)
    return file, auto if isinstance(auto, bool) else True


def remember(file=None, auto_advance=True, root=None):
    """Store the last playlist file and auto-advance choice in config/local.json (merged with other keys)."""
    from .resource_safety import path as settings_path, read_settings, MAX_BYTES as SETTINGS_MAX
    from .storage import atomic_json
    target_file = settings_path(root)
    settings = read_settings(target_file)
    settings[SETTINGS_KEY] = {'file': str(file) if file else None, 'auto_advance': bool(auto_advance)}
    if len(json.dumps(settings, ensure_ascii=False, indent=2).encode('utf-8')) > SETTINGS_MAX:
        raise ValueError('Local settings exceed the size limit.')
    atomic_json(target_file, settings)
