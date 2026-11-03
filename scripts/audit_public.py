"""Check the publication file set without printing secret values or local paths."""
from pathlib import Path
import ipaddress
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SKIP = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'artifacts', 'test-results', 'playwright-report'}
PRIVATE_DIRS = {'data', 'salida', 'signing', 'downloads', '.mirror-receipts'}
PRIVATE_NAMES = {'.env', 'config.json', 'local.key', 'runtime.json', '.site-mirror-state.json', 'download-report.json'}
PRIVATE_SUFFIXES = {'.p12', '.pfx', '.pem', '.key', '.pass', '.sqlite', '.sqlite3', '.db', '.lnk', '.log', '.pyc', '.psd', '.psb'}
RULES = {
    'user profile path': re.compile(r'(?:[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\']+|/(?:Users|home)/[^/\s"\']+)'),
    'publisher workspace path': re.compile(r'(?:[A-Za-z]:[\\/]+(?:Escritorio|--- Materiales|--- Tools)[\\/])', re.I),
    'private key material': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'service token literal': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{25,}|github_pat_[A-Za-z0-9_]{25,}|sk-ant-[A-Za-z0-9_-]{20,}|pina_[A-Za-z0-9_-]{20,})\b'),
    'personal email': re.compile(r'\b[\w.+-]+@(?:gmail|hotmail|outlook|yahoo)\.[A-Za-z.]+\b', re.I),
}

def files():
    if (ROOT / '.git').exists():
        result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, check=True)
        return [ROOT / name for name in result.stdout.decode('utf-8').split('\0') if name]
    return [p for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts) & SKIP]

def main():
    failures = []
    checked = 0
    for file in files():
        rel = file.relative_to(ROOT)
        if set(rel.parts) & SKIP:
            failures.append((rel.as_posix(), 'installed or generated file tracked'))
            continue
        if set(rel.parts) & PRIVATE_DIRS or file.name in PRIVATE_NAMES or file.suffix.lower() in PRIVATE_SUFFIXES or (file.name.startswith('.env.') and file.name != '.env.example'):
            failures.append((rel.as_posix(), 'private/generated file'))
            continue
        checked += 1
        try:
            content = file.read_text(encoding='utf-8')
        except UnicodeError:
            failures.append((rel.as_posix(), 'unreviewed binary file'))
            continue
        for rule, pattern in RULES.items():
            if pattern.search(content):
                failures.append((rel.as_posix(), rule))
        for match in re.finditer(r'https?://(\d{1,3}(?:\.\d{1,3}){3})(?=[:/\s"\']|$)', content):
            try:
                addr = ipaddress.ip_address(match[1])
            except ValueError:
                continue
            if not (addr.is_loopback or addr.is_unspecified or addr.is_reserved):
                failures.append((rel.as_posix(), 'literal network address'))
    for name, reason in failures:
        print(f'FAIL {name}: {reason}')
    print(f'Publication audit: {checked} files checked; {len(failures)} findings.')
    return bool(failures)

if __name__ == '__main__':
    sys.exit(main())
