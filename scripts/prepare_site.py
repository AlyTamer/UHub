"""Preserve UHub's tracked static assets, excluding collector infrastructure."""
from pathlib import Path
import shutil
import subprocess


def prepare_site(root: Path, destination: Path) -> None:
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    destination.mkdir(parents=True, exist_ok=True)
    for name in filter(None, tracked):
        relative = Path(name)
        if relative.parts[0] in {'.github', 'scripts', 'tests', 'README.md', 'ROOM_FEED_SETUP.md'}:
            continue
        source = root / relative
        if source.is_symlink() or not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if not (destination / 'index.html').is_file():
        raise RuntimeError('Missing index.html: run this workflow in the UHub repository.')
    (destination / '.nojekyll').touch()


if __name__ == '__main__':
    prepare_site(Path.cwd(), Path.cwd() / '_site')
