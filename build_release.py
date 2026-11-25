"""Build the portable Windows release from the project folder."""

from importlib.metadata import distribution
from pathlib import Path
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'
APP = DIST / 'AutoClicker'
LIBRARIES = (
    'customtkinter', 'darkdetect', 'packaging', 'PyAutoGUI', 'MouseInfo',
    'PyGetWindow', 'PyMsgBox', 'PyRect', 'PyScreeze', 'PyTweening',
    'pynput', 'six', 'PyInstaller',
)


def copy_notices():
    notices = APP / 'Third-party notices'
    notices.mkdir(exist_ok=True)
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', notices / 'Python-LICENSE.txt')
    libraries = []
    for name in LIBRARIES:
        package = distribution(name)
        libraries.append(f'{name} {package.version} — https://pypi.org/project/{name}/{package.version}/')
        for file in package.files or ():
            if file.name.lower().startswith(('license', 'copying', 'authors', 'notice')):
                source = package.locate_file(file)
                if source.is_file():
                    shutil.copy2(source, notices / f'{name}-{file.name}')
    (notices / 'Packages.txt').write_text('\n'.join(libraries) + '\n', encoding='utf-8')


def main():
    if sys.platform != 'win32' or platform.machine().upper() not in ('AMD64', 'X86_64'):
        raise SystemExit('Build this release with 64-bit Python on Windows x64.')
    subprocess.run(
        [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
         '--debug', 'noarchive', '--name', 'AutoClicker', '--collect-data', 'customtkinter',
         '--hidden-import', 'pynput.keyboard._win32', '--hidden-import', 'pynput.mouse._win32',
         '--exclude-module', 'numpy', '--exclude-module', 'psutil',
         '--distpath', str(DIST), '--workpath', str(BUILD), '--specpath', str(BUILD),
         str(ROOT / 'autoclickerrecord.py')],
        cwd=ROOT,
        check=True,
    )
    shutil.copy2(ROOT / 'README.md', APP / 'README.md')
    copy_notices()
    archive = shutil.make_archive(str(DIST / 'AutoClicker-windows-x64'), 'zip', root_dir=DIST, base_dir=APP.name)
    print(archive)


if __name__ == '__main__':
    main()
