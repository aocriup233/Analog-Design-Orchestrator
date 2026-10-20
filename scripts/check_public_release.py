"""Fail closed on unexpected wheel/sdist members; never extract archives."""
import argparse
from pathlib import Path, PurePosixPath
import tarfile
import zipfile


def check_member(name, *, wheel):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('Unsafe archive member: ' + name)
    parts = path.parts if wheel else path.parts[1:]
    if not parts:
        return
    def public_module(items):
        return ((len(items) == 2 and items[0] == 'analog_agent') or
                (len(items) == 3 and items[:2] == ('analog_agent', 'proposers'))) and (
                    PurePosixPath(items[-1]).suffix in ('.py', '.mjs'))
    if wheel:
        metadata = parts[0].startswith('analog_agent-') and parts[0].endswith('.dist-info')
        allowed = public_module(parts) or (metadata and (
            (len(parts) == 2 and parts[1] in {'METADATA', 'WHEEL', 'entry_points.txt',
                                             'top_level.txt', 'RECORD', 'LICENSE'}) or
            (len(parts) == 3 and parts[1:] == ('licenses', 'LICENSE'))))
    else:
        allowed = (len(parts) == 1 and parts[0] in {
            'LICENSE', 'README.md', 'README.zh-CN.md', 'pyproject.toml',
            'MANIFEST.in', 'PKG-INFO', 'setup.cfg'}) or (
            parts[0] == 'src' and public_module(parts[1:])) or (
            len(parts) == 3 and parts[:2] == ('src', 'analog_agent.egg-info')
            and parts[2] in {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt',
                             'entry_points.txt', 'requires.txt', 'top_level.txt'})
    if not allowed:
        raise ValueError('Unapproved distribution member: ' + name)


def check_archive(path):
    count = 0
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    check_member(member.filename, wheel=True)
                    count += 1
    else:
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                if member.issym() or member.islnk():
                    raise ValueError('Archive links are not allowed')
                if member.isfile():
                    check_member(member.name, wheel=False)
                    count += 1
    print(f'PASS {path.name}: {count} approved files')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', type=Path, nargs='+')
    for item in parser.parse_args().archives:
        check_archive(item)
