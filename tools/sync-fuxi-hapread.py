#!/usr/bin/env python3
"""Generate/check patch 0124 (fuxi-hapread, haptics step H1) from xnu/fuxi-hapread/.

xnu/fuxi-hapread/hooks.diff holds the two build hooks: bsd/dev/fuxi_hapread.c in bsd/conf/files, and its
init called in bsd/dev/sweet_ufs.c after fuxi_audio_dsp_init(). The new file bsd/dev/fuxi_hapread.c is a
snapshot of the canonical source next to it. Boot-arg fuxihapread=1 turns it on: a read-only dump of the
PMIC's haptics blocks (research_notes/loa-mic-rung.md 3.4); tools/test-fuxi-hapread.py checks it writes nothing.
Run it in WSL/Linux: Windows Python writes CRLF and the kernel build fails.

  --check          the checked-in patch must equal the generated one
  --xnu DIR        also check that 0124 applies after every earlier delta to bsd/conf/files and bsd/dev/sweet_ufs.c,
                   on DIR's stock bsd/conf/files (xnu-8796.141.3), with and without 0117 (only kernel-metal4wifi
                   applies 0117). Without it, the sweet_ufs.c hunks are still checked: earlier patches create that file.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-hapread'
PATCH = ROOT / 'xnu/patches/0124-fuxi-hapread.patch'
NEW = ('fuxi_hapread.c',)
HOOKED = ('bsd/conf/files', 'bsd/dev/sweet_ufs.c')
# The only lines hooks.diff may add, in order.
ADDED = [
    'bsd/dev/fuxi_hapread.c\t\tstandard',
    'extern void fuxi_hapread_init(void);',
    "\t/* Haptics step H1, boot-arg fuxihapread=1: a read-only look at the PMIC's haptics (bsd/dev/fuxi_hapread.c). */",
    '\tfuxi_hapread_init();',
]


def new_file(path, text):
    lines = text.splitlines(True)
    assert lines and text.endswith('\n'), path
    return '--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n' % (path, len(lines)) + ''.join('+' + l for l in lines)


def expected():
    hooks = (SRC / 'hooks.diff').read_text()
    paths = re.findall(r'^--- a/(\S+)\n', hooks, re.M)
    assert paths == list(HOOKED), 'hooks.diff: bsd/conf/files and bsd/dev/sweet_ufs.c only, got %s' % paths
    lines, added, hunks, i = hooks.splitlines(), [], 0, 0
    while i < len(lines):
        head = re.match(r'@@ -\d+,(\d+) \+\d+,(\d+) @@$', lines[i])
        i += 1
        if not head:
            assert re.match(r'(--- a/|\+\+\+ b/)\S+$', lines[i - 1]), 'hooks.diff: not a header or hunk: ' + lines[i - 1]
            continue
        old, new = int(head[1]), int(head[2])
        while old or new:
            line = lines[i]
            i += 1
            assert line[:1] in (' ', '+'), 'hooks.diff only adds lines: ' + line
            old -= line[:1] == ' '
            new -= 1
            if line[:1] == '+':
                added.append(line[1:])
            assert old >= 0 and new >= 0, head[0]
        hunks += 1
    assert hunks == 3, 'hooks.diff: the conf line, the extern, the call'
    assert added == ADDED, 'hooks.diff adds other lines: %r' % added
    return hooks + ''.join(new_file('bsd/dev/' + n, (SRC / n).read_text()) for n in NEW)


def chunks(text, paths):
    for chunk in re.split(r'(?=^--- (?:a/|/dev/null))', text, flags=re.M):
        match = re.search(r'^\+\+\+ b/(\S+)', chunk, re.M)
        if match and match[1] in paths:
            yield chunk


def baseline(tmp, stock, with_0117):
    """The two hooked files as every patch before 0124 leaves them (bsd/conf/files only with stock)."""
    paths = HOOKED if stock else HOOKED[1:]
    if stock:
        (tmp / 'bsd/conf').mkdir(parents=True)
        shutil.copyfile(stock / 'bsd/conf/files', tmp / 'bsd/conf/files')
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name >= PATCH.name:
            break
        if prior.name.startswith('0117-') and not with_0117:
            continue
        for chunk in chunks(prior.read_text(errors='surrogateescape'), paths):
            subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], input=chunk, text=True, cwd=tmp,
                           check=True, capture_output=True)
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--xnu', type=Path)
    args = ap.parse_args()
    text = expected()
    assert '\r' not in text
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name != PATCH.name:
            body = prior.read_text(errors='surrogateescape')
            assert not re.search(r'^\+\+\+ b/bsd/dev/fuxi_hapread\.c$', body, re.M), prior.name + ' already creates fuxi_hapread.c'
            assert not re.search(r'^[+-].*fuxi_hapread', body, re.M), prior.name + ' already names fuxi_hapread'
    if args.check:
        assert PATCH.exists() and PATCH.read_text() == text, 'fuxi-hapread sources differ; run tools/sync-fuxi-hapread.py'
    else:
        with open(PATCH, 'w', newline='') as f:
            f.write(text)
    said = []
    for with_0117 in (True, False):
        with tempfile.TemporaryDirectory(prefix='fuxi-hapread-') as tmp:
            paths = baseline(Path(tmp), args.xnu, with_0117)
            hooks = ''.join(chunks(text, paths))
            subprocess.run(['git', 'apply', '--check', '-'], input=hooks, text=True, cwd=tmp, check=True)
            said.append('%s %s' % ('+'.join(Path(p).name for p in paths), 'with 0117' if with_0117 else 'without 0117'))
    print('PASS: fuxi-hapread source and hooks.diff match 0124; its hunks apply to ' + ', '.join(said))


if __name__ == '__main__':
    main()
