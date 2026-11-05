#!/usr/bin/env python3
"""Haptics step H1 (xnu/fuxi-hapread/fuxi_hapread.c, patch 0124, boot-arg fuxihapread=1) on the host.

1. Patch 0124 matches its sources (tools/sync-fuxi-hapread.py --check).
2. No PMIC write can be issued: with comments and strings removed, the file has exactly one store through a
   volatile pointer, in fhr_ask(), its value fixed to EXT_READL (opcode 1) of one byte, on an EE 0 observer channel;
   no other opcode shift, no write/WDATA/chnls name, no RW-channel address, only the three windows fuxi_keys.c maps
   (same addresses, same strides), and nothing else stores to MMIO.
3. The register list is the note's (research_notes/loa-mic-rung.md 3.4, row H1): the C table, and what the code really
   asks for against a model of the arbiter, in order, once each; SDAM46 is looked up and never read.
   --note FILE also parses that row from the note itself.
4. Every stop condition gives "no" with its reason, a clean dump gives "yes"; a read that never answers ends the
   dump after FHR_POLLS polls; a refused read is said and counted; without fuxihapread=1 nothing is mapped, read,
   printed or started.
5. The real file compiles for arm64 under XNU's warning set (-Weverything -Werror and XNU's exceptions), stub headers.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-hapread/fuxi_hapread.c'
KEYS = ROOT / 'xnu/fuxi-h1/fuxi_keys.c'

# research_notes/loa-mic-rung.md 3.4, row H1. SDAM46's PPID is the live DT's (pmk8550@0 sdam@9d00, the haptics' nvmem).
NOTE_PPIDS = {'HAP_CFG': 0x7f0, 'HAP_PTN': 0x7f1, 'HAP_HBST': 0x7f2, 'SDAM46': 0x09d}
NOTE_READS = {
    'HAP_CFG': [0x01, 0x04, 0x05, *range(0x09, 0x0f), 0x10, *range(0x46, 0x4f), *range(0x50, 0x54), *range(0x5c, 0x69)],
    'HAP_PTN': [0x01, 0x04, 0x05, *range(0x24, 0x2c), *range(0x70, 0x78)],
    'HAP_HBST': [0x00, 0x01],
}
NOTE_ORDER = [(NOTE_PPIDS[p], r) for p in ('HAP_CFG', 'HAP_PTN', 'HAP_HBST') for r in NOTE_READS[p]]
FIFO_PORTS = {(0x7f1, r) for r in (0x20, 0x21, 0x22, 0x23, 0x2c)}   # HAP_PTN FIFO_DIN_0..3, FIFO_DIN_1B

# XNU's makedefs/MakeInc.def WARNFLAGS_STD for C (-Weverything -Werror and its exceptions), as test-fuxi-args.py uses.
XNU_WARN = ('-Weverything -Werror -Wno-pedantic -Wno-bad-function-cast -Wno-bitwise-instead-of-logical -Wno-c++-compat '
            '-Wno-c++98-compat -Wno-conditional-uninitialized -Wno-covered-switch-default -Wno-disabled-macro-expansion '
            '-Wno-documentation-unknown-command -Wno-extra-semi-stmt -Wno-format-non-iso -Wno-language-extension-token '
            '-Wno-missing-variable-declarations -Wno-packed -Wno-padded -Wno-partial-availability -Wno-reserved-id-macro '
            '-Wno-shift-sign-overflow -Wno-switch-enum -Wno-unaligned-access -Wno-undef -Wno-unused-macros '
            '-Wno-used-but-marked-unused -Wno-variadic-macros -Wno-vla -Wno-zero-length-array -Wno-unknown-warning-option '
            '-Wno-anon-enum-enum-conversion -Wno-error=enum-enum-conversion -Wno-error=c99-designator '
            '-Wno-error=reorder-init-list -Wno-error=declaration-after-statement -Wno-sign-compare -Wno-sign-conversion '
            '-Wpointer-arith '
            # host-only: newer clang than Xcode 15's, stub headers
            '-Wno-unsafe-buffer-usage -Wno-reserved-identifier -Wno-reserved-macro-identifier -Wno-pre-c2x-compat '
            '-Wno-declaration-after-statement').split()


def code_only(text):
    """The C with comments, string and character literals blanked (newlines kept)."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith('/*', i):
            j = text.index('*/', i + 2) + 2
            out.append(re.sub(r'[^\n]', ' ', text[i:j]))
            i = j
        elif text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i))
            i = j
        elif text[i] in '"\'':
            q, j = text[i], i + 1
            while text[j] != q:
                j += 2 if text[j] == '\\' else 1
            out.append(q + ' ' * (j - i - 1) + q)
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def function(text, name):
    match = re.search(r'^(?:static )?[a-z_][a-z0-9_ ]*\n' + name + r'\(', text, re.M)
    assert match, name
    end = text.index('{', match.end())
    depth, end = 1, end + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return match.start(), end


def body(text, name):
    start, end = function(text, name)
    return text[start:end]


def static_checks(src):
    code = code_only(src)
    # 2a. One store through a volatile pointer, and it is the EXT_READL command word in fhr_ask().
    stores = re.findall(r'\*\s*\(\s*volatile\s+uint32_t\s*\*\s*\)\s*\(([^;]*?)\)\s*=(?!=)\s*([^;]*);', code)
    assert stores == [('chan + FHR_CMD', 'FHR_EXT_READL | ((uint32_t)reg << 4)')], stores
    assert code.count('volatile') == 2 + code.count('__asm__ volatile'), 'volatile only in fhr_rd, fhr_ask and asm'
    ask = code_only(body(src, 'fhr_ask'))
    assert ask.count('*(volatile uint32_t *)(chan + FHR_CMD) = FHR_EXT_READL | ((uint32_t)reg << 4);') == 1
    rd = code_only(body(src, 'fhr_rd'))
    assert re.search(r'uint32_t v = \*\(volatile uint32_t \*\)\(base \+ off\);', rd) and '=' not in rd.replace(
        'uint32_t v =', '').replace('==', ''), 'fhr_rd only loads'
    # Every other pointer dereference: none (no array or pointer store into a mapped window).
    derefs = [m.start() for m in re.finditer(r'\*\s*\(', code)]
    s_ask, e_ask = function(src, 'fhr_ask')
    s_rd, e_rd = function(src, 'fhr_rd')
    assert all(s_ask <= d < e_ask or s_rd <= d < e_rd for d in derefs), 'a cast dereference outside fhr_rd/fhr_ask'
    assert not re.search(r'\b(fhr_core|fhr_cnfg|fhr_obs|chan|base)\s*\[', code), 'MMIO indexed as an array'
    casts = [m.start() for m in re.finditer(r'\(\s*(?:const\s+|volatile\s+)*(?:struct\s+)?\w+\s*\*+\s*\)', code)]
    assert len(casts) == 2 and all(s_ask <= c < e_ask or s_rd <= c < e_rd for c in casts), 'a pointer cast elsewhere'
    for name in ('fhr_core', 'fhr_cnfg', 'fhr_obs'):
        for m in re.finditer(r'\b' + name + r'\b\s*=(?!=)', code):
            assert code[m.end():].lstrip().startswith('ml_io_map('), name + ' set from something else'
    # 2b. The opcode: one shift by 27, in FHR_EXT_READL = (1u << 27); FHR_CMD is the command register 0x00.
    assert re.findall(r'<<\s*27', code) == ['<< 27'] and '#define FHR_EXT_READL   (1u << 27)' in code
    defines = dict(re.findall(r'^#define (FHR_\w+)\s+(\S+)', code, re.M))
    want = {'FHR_CORE_PA': '0x0c400000u', 'FHR_CORE_SIZE': '0x3000u', 'FHR_CNFG_PA': '0x0c42d000u',
            'FHR_CNFG_SIZE': '0x1000u', 'FHR_OBS_PA': '0x0c440000u', 'FHR_OBS_SIZE': '0x8000u', 'FHR_MAP_OFF': '0x2000u',
            'FHR_MAX_APIDS': '1024u', 'FHR_CHAN_STRIDE': '0x20u', 'FHR_CMD': '0x00u', 'FHR_STATUS': '0x08u',
            'FHR_RDATA0': '0x18u', 'FHR_DONE': '0x01u', 'FHR_ERRORS': '0x0eu', 'FHR_POLLS': '1000u'}
    assert {k: defines.get(k) for k in want} == want, defines
    # 2c. Nothing named or addressed like a write.
    bad = re.findall(r'(?i)write|wdata|chnl|wr_base|zero_?w|mstr|\bfk_|\bsweet_spmi_(?!bus_(?:un)?lock\b)\w+|'
                     r'0x0?c50{5}|memcpy|memset|memmove|bcopy|bzero|ml_io_map\s*\(\s*0x', code)
    assert not bad, bad
    asm = re.findall(r'__asm__ volatile\("([^"]*)"', src)
    assert asm and set(asm) == {'dmb oshld', 'dmb oshst', 'dsb osh'}, asm
    maps = re.findall(r'ml_io_map\(([^)]*)\)', code)
    assert maps == ['vm_offset_t, vm_size_t', 'FHR_CORE_PA, FHR_CORE_SIZE', 'FHR_CNFG_PA, FHR_CNFG_SIZE',
                    'FHR_OBS_PA, FHR_OBS_SIZE'], maps
    # Only the call chain fhr_dump -> fhr_read -> fhr_read_locked -> fhr_ask reaches the store.
    for callee, caller in (('fhr_ask', 'fhr_read_locked'), ('fhr_read_locked', 'fhr_read'), ('fhr_read', 'fhr_dump')):
        calls = [m.start() for m in re.finditer(r'\b' + callee + r'\(', code)]
        s, e = function(src, caller)
        own = function(src, callee)[0]
        assert len(calls) == 2 and calls[0] == own + code[own:].index(callee + '(') and s <= calls[1] < e, (callee, caller)
    # 2d. The same arbiter, channel and EE as fuxi_keys.c (its values, its read command).
    keys = code_only(KEYS.read_text())
    kdef = dict(re.findall(r'^#define (FK_\w+)\s+(\S+)', keys, re.M))
    assert (kdef['FK_CORE_PA'], kdef['FK_CNFG_PA'], kdef['FK_OBS_PA'], kdef['FK_MAP_OFF']) == (
        want['FHR_CORE_PA'], want['FHR_CNFG_PA'], want['FHR_OBS_PA'], want['FHR_MAP_OFF']), kdef
    kread = code_only(body(KEYS.read_text(), 'fk_read_locked'))
    assert 'uint32_t off = 0x20u * apid;' in kread and 'uint32_t cmd = (1u << 27) | ((uint32_t)reg << 4);' in kread
    assert 'fk_rd(fk_obs, off + 0x08u)' in kread and 'fk_rd(fk_obs, off + 0x18u)' in kread
    assert 'fk_obs = ml_io_map(FK_OBS_PA, 0x8000);' in keys and 'fk_core = ml_io_map(FK_CORE_PA, 0x3000);' in keys
    # 2e. The gate: the boot-arg is parsed first, and nothing else starts the work.
    init = code_only(body(src, 'fuxi_hapread_init'))
    assert re.search(r'\{\s*int on = 0;\s*thread_t thread;\s*if \(!PE_parse_boot_argn\(" {11}", &on, sizeof\(on\)\) \|\| '
                     r'on != 1\) \{\s*return;\s*\}', init), init
    assert 'PE_parse_boot_argn("fuxihapread", &on, sizeof(on))' in src and code.count('PE_parse_boot_argn(') == 1
    assert code.count('kernel_thread_start(') == 1 and code.count('fhr_run()') == 1


def table_checks(src):
    code = code_only(src)
    periphs = re.findall(r'\[FHR_(\w+)\] = \{ " *", (0x[0-9a-f]+), (true|false) \}', code)
    names = re.findall(r'\[FHR_\w+\] = \{ "(\w+)", 0x', src)
    assert [(n, int(p, 16), r == 'true') for n, (_, p, r) in zip(names, periphs)] == [
        ('HAP_CFG', 0x7f0, True), ('HAP_PTN', 0x7f1, True), ('HAP_HBST', 0x7f2, True), ('SDAM46', 0x09d, False)], periphs
    assert {n: int(p, 16) for n, (_, p, _r) in zip(names, periphs)} == NOTE_PPIDS
    table = code[code.index('fhr_ranges[] = {'):code.index('};', code.index('fhr_ranges[] = {'))]
    ranges = re.findall(r'\{ FHR_(CFG|PTN|HBST|SDAM), 0x([0-9a-f]{2}), 0x([0-9a-f]{2}) \}', table)
    assert len(ranges) == table.count('{ FHR_'), 'a range the parser cannot read'
    got = [(NOTE_PPIDS['HAP_' + p if p != 'SDAM' else 'SDAM46'], r) for p, a, b in ranges
           for r in range(int(a, 16), int(b, 16) + 1)]
    assert got == NOTE_ORDER, got
    assert not FIFO_PORTS & set(got) and not any(p == 0x09d for p, _ in got)


def parse_note(path):
    """The H1 row of research_notes/loa-mic-rung.md: its PPIDs and its read list."""
    row = next(l for l in Path(path).read_text(encoding='utf-8').splitlines() if l.startswith('| **H1** |'))
    cell = row.split('|')[2]
    ppids = [int(x, 16) for x in re.findall(r'0x[0-9A-Fa-f]+', cell[:cell.index('SDAM46')])]
    assert ppids == [0x7f0, 0x7f1, 0x7f2], ppids
    reads = re.sub(r'\([^)]*\)|`', '', cell[cell.index('revision'):])
    parts = re.split(r'\b(HAP_CFG|HAP_PTN|HBST)\b', reads)
    def regs(s):
        out = []
        for a, b in re.findall(r'(0x[0-9A-Fa-f]{2})(?:-(0x[0-9A-Fa-f]{2}))?', s):
            out += range(int(a, 16), int(b or a, 16) + 1)
        return out
    common = regs(parts[0])
    found = {'HAP_CFG': common + regs(parts[2]), 'HAP_PTN': common + regs(parts[4]), 'HAP_HBST': regs(parts[6])}
    assert parts[1::2] == ['HAP_CFG', 'HAP_PTN', 'HBST'], parts
    assert found == NOTE_READS, found
    print('PASS: the note\'s H1 row (%s) lists the PPIDs and %d registers this test holds' % (path, len(NOTE_ORDER)))


MODEL = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
typedef size_t vm_size_t;
typedef int kern_return_t;
typedef int wait_result_t;
typedef void *thread_t;
typedef void (*thread_continue_t)(void *, wait_result_t);
#define KERN_SUCCESS 0
#define THREAD_CONTINUE_NULL ((thread_continue_t)0)
#define CORE_PA 0x0c400000u
#define CNFG_PA 0x0c42d000u
#define OBS_PA 0x0c440000u
static char out[32768];
static size_t outlen;
__attribute__((format(printf, 1, 2))) static int k_printf(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(out + outlen, sizeof(out) - outlen, fmt, ap);
	va_end(ap);
	assert(n >= 0 && outlen + (size_t)n < sizeof(out));
	outlen += (size_t)n;
	return n;
}
#define printf k_printf
static size_t k_strlcat(char *d, const char *s, size_t cap)
{
	size_t a = strlen(d), b = strlen(s);
	assert(a < cap);
	size_t c = b < cap - a - 1 ? b : cap - a - 1;
	memcpy(d + a, s, c);
	d[a + c] = 0;
	return a + b;
}
#define strlcat k_strlcat
static uint32_t a_version = 0x70020000u, a_count = 300, a_map[1024], a_owner[1024];
static uint8_t pmic[4096][256];
static int ch_state[1024];      /* 0 idle, 1 answers, 2 never answers, 3 answers "denied" */
static unsigned ch_wait[1024], ch_done[1024];
static uint32_t ch_data[1024];
static unsigned asks, delays, locked, locks, maps, threads, timeout_at = ~0u, late;
static uint16_t deny_ppid = 0xffff;
static vm_offset_t map_pa[8];
static vm_size_t map_size[8];
static uint16_t log_ppid[512];
static uint8_t log_reg[512];
static int arg_present, arg_value;
static vm_offset_t ml_io_map(vm_offset_t pa, vm_size_t size)
{
	assert(maps < 8);
	map_pa[maps] = pa;
	map_size[maps++] = size;
	return pa;
}
static void sweet_spmi_bus_lock(void) { assert(!locked); locked = 1; ++locks; }
static void sweet_spmi_bus_unlock(void) { assert(locked); locked = 0; }
static void delay(int us) { assert(us == 1 && locked); ++delays; assert(delays < 100000); }
static int PE_parse_boot_argn(const char *name, void *p, int len)
{
	assert(!strcmp(name, "fuxihapread") && len == (int)sizeof(int));
	if (!arg_present) return 0;
	*(int *)p = arg_value;
	return 1;
}
static kern_return_t kernel_thread_start(thread_continue_t fn, void *arg, thread_t *t)
{
	++threads;
	*t = &threads;
	fn(arg, 0);
	return KERN_SUCCESS;
}
static void thread_deallocate(thread_t t) { assert(t == &threads); }
static kern_return_t thread_terminate(thread_t t) { (void)t; return KERN_SUCCESS; }
static thread_t current_thread(void) { return NULL; }
static wait_result_t thread_block(thread_continue_t c) { assert(c == THREAD_CONTINUE_NULL); return 0; }
/* The arbiter model: the command word starts a transaction on that APID's observer channel. */
static void fhr_ask(vm_offset_t chan, uint8_t reg)
{
	assert(locked);
	assert(chan >= OBS_PA && chan < OBS_PA + 0x8000 && (chan - OBS_PA) % 0x20 == 0);
	unsigned apid = (unsigned)((chan - OBS_PA) / 0x20);
	assert(apid < a_count && a_map[apid] != 0 && ch_state[apid] == 0);
	uint16_t ppid = (uint16_t)((a_map[apid] >> 8) & 0xfff);
	assert(asks < 512);
	log_ppid[asks] = ppid;
	log_reg[asks] = reg;
	ch_state[apid] = asks == timeout_at ? 2 : ppid == deny_ppid ? 3 : 1;
	ch_wait[apid] = late;
	ch_done[apid] = 0;
	ch_data[apid] = 0xdead0000u | pmic[ppid][reg];
	++asks;
}
static uint32_t fhr_rd(vm_offset_t base, uint32_t off)
{
	vm_offset_t a = base + off;
	if (a >= CORE_PA && a < CORE_PA + 0x3000) {
		if (a == CORE_PA) return a_version;
		if (a == CORE_PA + 4) return 0xf800u | a_count;     /* bits above 10:0 are not the count */
		assert(a >= CORE_PA + 0x2000 && (a - CORE_PA - 0x2000) / 4 < a_count && a % 4 == 0);
		return a_map[(a - CORE_PA - 0x2000) / 4];
	}
	if (a >= CNFG_PA && a < CNFG_PA + 0x1000) {
		assert((a - CNFG_PA) / 4 < a_count && a % 4 == 0);
		return 0xf8u | a_owner[(a - CNFG_PA) / 4];           /* bits above 2:0 are not the owner */
	}
	assert(a >= OBS_PA && a < OBS_PA + 0x8000 && locked);
	unsigned apid = (unsigned)((a - OBS_PA) / 0x20), r = (unsigned)((a - OBS_PA) % 0x20);
	assert(ch_state[apid] != 0);
	if (r == 0x08) {
		if (ch_state[apid] == 2 || ch_wait[apid]) {
			if (ch_wait[apid]) --ch_wait[apid];
			return 0;
		}
		ch_done[apid] = 1;
		if (ch_state[apid] == 3) { ch_state[apid] = 0; return 0x5u; }   /* done, denied */
		return 0x1u;
	}
	assert(r == 0x18 && ch_done[apid] && ch_state[apid] == 1);
	ch_state[apid] = 0;
	return ch_data[apid];
}
'''

MAIN = r'''
static void map_one(unsigned apid, uint16_t ppid, unsigned owner, bool irq)
{
	a_map[apid] = (uint32_t)ppid << 8 | (irq ? 1u << 24 : 0) | 0x3u;
	a_owner[apid] = owner;
}
int main(int argc, char **argv)
{
	assert(argc == 2);
	const char *s = argv[1];
	for (unsigned p = 0; p < 4096; ++p)
		for (unsigned r = 0; r < 256; ++r)
			pmic[p][r] = (uint8_t)(r ^ 0x5a);
	map_one(5, 0x013, 0, true);            /* PON */
	map_one(40, 0x18d, 0, true);           /* GPIO6 */
	map_one(90, 0x09d, 2, false);          /* SDAM46, another EE's */
	map_one(150, 0x7f0, 2, true);          /* HAP_CFG decoy owned by ee2, before ee0's */
	map_one(151, 0x1f0, 0, false);         /* another SID's 0xf0 */
	map_one(200, 0x7f0, 0, true);
	map_one(201, 0x7f1, 0, false);
	map_one(202, 0x7f2, 2, false);
	uint8_t *cfg = pmic[0x7f0], *ptn = pmic[0x7f1], *hbst = pmic[0x7f2];
	cfg[0x01] = 0x04; ptn[0x01] = 0x04; cfg[0x46] = 0x80; cfg[0x4c] = 0x00; cfg[0x0c] = 0x00; cfg[0x48] = 0x18;
	cfg[0x0d] = 0x02; cfg[0x5c] = 0x04; cfg[0x5d] = 0x98; cfg[0x0b] = 0x03; cfg[0x0e] = 0x20; cfg[0x10] = 0x00;
	ptn[0x26] = 0x00; hbst[0x00] = 0x02; hbst[0x01] = 0x00;
	arg_present = 1;
	arg_value = 1;
	if (!strcmp(s, "noarg")) arg_present = 0;
	else if (!strcmp(s, "arg0")) arg_value = 0;
	else if (!strcmp(s, "arg2")) arg_value = 2;
	else if (!strcmp(s, "rev")) cfg[0x01] = 0x03;
	else if (!strcmp(s, "ptnrev")) ptn[0x01] = 0x03;
	else if (!strcmp(s, "owner")) a_map[200] = 0;
	else if (!strcmp(s, "ptnowner")) a_owner[201] = 1;
	else if (!strcmp(s, "en")) cfg[0x46] = 0x00;
	else if (!strcmp(s, "playing")) cfg[0x4c] = 0x81;
	else if (!strcmp(s, "fault")) cfg[0x0c] = 0x01;
	else if (!strcmp(s, "hbst1")) hbst[0x00] = 0x01;
	else if (!strcmp(s, "hbst100")) { hbst[0x00] = 0x00; hbst[0x01] = 0x01; }
	else if (!strcmp(s, "timeout")) timeout_at = 10;
	else if (!strcmp(s, "late")) late = 3;
	else if (!strcmp(s, "deny")) deny_ppid = 0x7f2;
	else if (!strcmp(s, "nocfg")) { a_map[150] = 0; a_map[200] = 0; }
	else if (!strcmp(s, "badver")) a_version = 0x50020000u;
	else if (!strcmp(s, "all")) { cfg[0x01] = 0x03; ptn[0x01] = 0x03; a_map[200] = 0; a_owner[201] = 1; cfg[0x46] = 0;
		cfg[0x4c] = 0x81; cfg[0x0c] = 0x07; hbst[0x00] = 0x01; }
	else assert(!strcmp(s, "good"));
	fuxi_hapread_init();
	assert(!locked);
	fputs(out, stdout);
	for (unsigned i = 0; i < asks; ++i) fprintf(stdout, "READ 0x%03x 0x%02x\n", log_ppid[i], log_reg[i]);
	for (unsigned i = 0; i < maps; ++i) fprintf(stdout, "MAP 0x%lx 0x%lx\n", (unsigned long)map_pa[i], (unsigned long)map_size[i]);
	fprintf(stdout, "STATS asks=%u delays=%u locks=%u threads=%u\n", asks, delays, locks, threads);
	return 0;
}
'''

REASONS = {
    'rev': 'revision 0x03/0x04, not 0x04/0x04',
    'ptnrev': 'revision 0x04/0x03, not 0x04/0x04',
    'owner': 'HAP_CFG owned by ee2, not ee0',
    'ptnowner': 'HAP_PTN owned by ee1, not ee0',
    'en': '0x46 bit 7 (HAPTICS_EN) is 0 (0x00)',
    'playing': '0x4c bit 7 (PLAY_EN) is set (0x81): playing',
    'fault': '0x0c (FAULT_STATUS) is 0x01',
    'hbst1': 'HBST revision 0x0001 (boost through SDAM)',
}


def run(binary, scenario):
    text = subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True).stdout
    lines = text.splitlines()
    said = [l for l in lines if not l.startswith(('READ ', 'MAP ', 'STATS '))]
    reads = [(int(p, 16), int(r, 16)) for p, r in re.findall(r'^READ (0x\w+) (0x\w+)$', text, re.M)]
    maps = re.findall(r'^MAP (0x\w+) (0x\w+)$', text, re.M)
    stats = dict(re.findall(r'(\w+)=(\d+)', next(l for l in lines if l.startswith('STATS '))))
    for line in said:
        assert line.startswith('FUXI HAPREAD: ') and len(line) <= 400, line
    verdict = [l for l in said if 'H2 would be allowed' in l]
    return said, reads, maps, {k: int(v) for k, v in stats.items()}, verdict


def dynamic_checks(src, tmp):
    start = src.index('#define FHR_CORE_PA')
    stop = src.rindex('#endif /* FUXI */')
    unit = src[start:stop]
    for name in ('fhr_rd', 'fhr_ask'):
        s, e = function(unit, name)
        unit = unit[:s] + unit[e:]
    source = tmp / 'hapread.c'
    source.write_text(MODEL + unit + MAIN)
    binary = tmp / 'hapread'
    subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all', str(source), '-o', str(binary)],
                   check=True)
    maps3 = [('0xc400000', '0x3000'), ('0xc42d000', '0x1000'), ('0xc440000', '0x8000')]

    said, reads, maps, st, verdict = run(binary, 'good')
    assert reads == NOTE_ORDER, reads
    assert maps == maps3 and st['threads'] == 1 and st['locks'] == st['asks'] == len(NOTE_ORDER) and st['delays'] == 0
    assert said[0] == ('FUXI HAPREAD: arbiter 0x70020000, 300 apids; reading as ee0 through observer channels, '
                       'one byte a command, no write'), said[0]
    assert said[1] == 'FUXI HAPREAD: HAP_CFG ppid 0x7f0: apid 200 owner ee0; 2 mapping(s) apid/owner: 150/ee2/irq 200/ee0/irq'
    assert said[2] == 'FUXI HAPREAD: HAP_PTN ppid 0x7f1: apid 201 owner ee0; 1 mapping(s) apid/owner: 201/ee0'
    assert said[3] == 'FUXI HAPREAD: HAP_HBST ppid 0x7f2: apid 202 owner ee2; 1 mapping(s) apid/owner: 202/ee2'
    assert said[4] == 'FUXI HAPREAD: SDAM46 ppid 0x09d: apid 90 owner ee2; 1 mapping(s) apid/owner: 90/ee2 (tables only, nothing read)'
    assert said[5] == 'FUXI HAPREAD: HAP_CFG 0x01: 04'
    assert said[6] == 'FUXI HAPREAD: HAP_CFG 0x04-0x05: 5e 5f'
    assert said[7] == 'FUXI HAPREAD: HAP_CFG 0x09-0x0e: 53 50 03 00 02 20'
    assert said[9] == 'FUXI HAPREAD: HAP_CFG 0x46-0x4e: 80 1d 18 13 10 11 00 17 14'
    assert said[11] == 'FUXI HAPREAD: HAP_CFG 0x5c-0x68: 04 98 04 05 3a 3b 38 39 3e 3f 3c 3d 32'
    assert said[16] == 'FUXI HAPREAD: HAP_HBST 0x00-0x01: 02 00'
    assert said[17] == ('FUXI HAPREAD: decoded: VMAX 1200 mV (50 mV steps), TLRA_OL 5880 us, HPWR_INTF 3, LRA impedance '
                        '8000 mOhm, INT_RT_STS 0x00, DIRECT_PLAY 0x00'), said[17]
    assert verdict == [said[18]] and said[18] == (
        'FUXI HAPREAD: H2 would be allowed: yes, because revision 0x04/0x04, HAP_CFG apid 200 and HAP_PTN apid 201 owned '
        'by ee0, 0x46 0x80 (HAPTICS_EN on), 0x4c 0x00 (not playing), 0x0c 0x00, HBST revision 0x0002'), said[18]
    assert said[19] == 'FUXI HAPREAD: done: 57 reads, 0 refused, none unanswered; no PMIC register was written'
    assert len(said) == 20
    good = said

    for scenario in ('noarg', 'arg0', 'arg2'):
        said, reads, maps, st, verdict = run(binary, scenario)
        assert not said and not reads and not maps and st == {'asks': 0, 'delays': 0, 'locks': 0, 'threads': 0}, scenario
    for scenario, reason in REASONS.items():
        said, reads, maps, st, verdict = run(binary, scenario)
        assert verdict == ['FUXI HAPREAD: H2 would be allowed: no, because ' + reason], (scenario, verdict)
        assert st['asks'] == len(NOTE_ORDER) and st['delays'] == 0
        assert reads == NOTE_ORDER, scenario
    said, reads, maps, st, verdict = run(binary, 'owner')
    assert 'FUXI HAPREAD: HAP_CFG ppid 0x7f0: apid 150 owner ee2; 1 mapping(s) apid/owner: 150/ee2/irq' in said
    said, reads, maps, st, verdict = run(binary, 'hbst100')
    assert verdict[0].endswith('HBST revision 0x0100'), verdict
    said, reads, maps, st, verdict = run(binary, 'late')
    assert reads == NOTE_ORDER and st['delays'] == 3 * len(NOTE_ORDER) and said == good
    said, reads, maps, st, verdict = run(binary, 'timeout')
    assert reads == NOTE_ORDER[:11] and st['delays'] == 1000 and st['asks'] == 11, (reads, st)
    assert 'FUXI HAPREAD: HAP_CFG 0x46: no answer after 1000 polls (status 0x0); giving up, nothing more is read' in said
    assert 'FUXI HAPREAD: HAP_CFG 0x46-0x4e: -- -- -- -- -- -- -- -- --' in said
    assert 'FUXI HAPREAD: HAP_HBST 0x00-0x01: -- --' in said
    assert verdict[0].startswith('FUXI HAPREAD: H2 would be allowed: no, because the dump is not complete '
                                 '(0 refused, a read went unanswered)'), verdict
    assert said[-1] == 'FUXI HAPREAD: done: 11 reads, 0 refused, stopped at an unanswered read; no PMIC register was written'
    said, reads, maps, st, verdict = run(binary, 'deny')
    assert reads == NOTE_ORDER and 'FUXI HAPREAD: HAP_HBST 0x00-0x01: !! !! (2 refused, status 0x5)' in said
    assert verdict == ['FUXI HAPREAD: H2 would be allowed: no, because the dump is not complete (2 refused)'], verdict
    said, reads, maps, st, verdict = run(binary, 'nocfg')
    assert reads == [x for x in NOTE_ORDER if x[0] != 0x7f0] and 'FUXI HAPREAD: HAP_CFG ppid 0x7f0: not in the APID map' in said
    assert 'FUXI HAPREAD: HAP_CFG 0x04-0x05: -- --' in said
    assert verdict[0].startswith('FUXI HAPREAD: H2 would be allowed: no, because the dump is not complete (0 refused); '
                                 'revision 0x00/0x04, not 0x04/0x04; HAP_CFG has no APID'), verdict
    said, reads, maps, st, verdict = run(binary, 'badver')
    assert said == ['FUXI HAPREAD: unsupported arbiter version=0x50020000 apids=300; nothing read'] and not reads
    said, reads, maps, st, verdict = run(binary, 'all')
    assert verdict == ['FUXI HAPREAD: H2 would be allowed: no, because revision 0x03/0x03, not 0x04/0x04; HAP_CFG '
                       'owned by ee2, not ee0; HAP_PTN owned by ee1, not ee0; 0x46 bit 7 (HAPTICS_EN) is 0 (0x00); '
                       '0x4c bit 7 (PLAY_EN) is set (0x81): playing; 0x0c (FAULT_STATUS) is 0x07; HBST revision '
                       '0x0001 (boost through SDAM)'], verdict
    return good


STUBS = {
    'sys/param.h': '#include <stddef.h>\n',
    'string.h': 'typedef __SIZE_TYPE__ size_t;\nsize_t strlcat(char *, const char *, size_t);\n',
    'sys/systm.h': ('typedef __SIZE_TYPE__ size_t;\nextern char version[];\nint printf(const char *, ...) __attribute__((format(printf, 1, 2)));\n'
                    'int snprintf(char *, size_t, const char *, ...) __attribute__((format(printf, 3, 4)));\n'),
    'kern/clock.h': 'extern void delay(int usec);\n',
    'kern/thread.h': ('#include <mach_types_stub.h>\nextern kern_return_t kernel_thread_start(thread_continue_t, void *, '
                      'thread_t *);\nextern void thread_deallocate(thread_t);\nextern thread_t current_thread(void);\n'),
    'kern/sched_prim.h': '#include <mach_types_stub.h>\nextern wait_result_t thread_block(thread_continue_t);\n',
    'machine/machine_routines.h': '#include <mach_types_stub.h>\n',
    'pexpert/pexpert.h': '#include <mach_types_stub.h>\nextern boolean_t PE_parse_boot_argn(const char *, void *, int);\n',
    'mach_types_stub.h': ('#pragma once\ntypedef unsigned long vm_offset_t;\ntypedef unsigned long vm_size_t;\n'
                          'typedef int kern_return_t;\ntypedef int wait_result_t;\ntypedef int boolean_t;\n'
                          'typedef struct thread *thread_t;\ntypedef void (*thread_continue_t)(void *, wait_result_t);\n'
                          '#define KERN_SUCCESS 0\n#define THREAD_CONTINUE_NULL ((thread_continue_t)0)\n'),
}


def xnu_syntax(tmp):
    inc = tmp / 'inc'
    for name, text in STUBS.items():
        (inc / name).parent.mkdir(parents=True, exist_ok=True)
        (inc / name).write_text(text)
    shutil.copyfile(ROOT / 'xnu/fuxi-h1/sweet_spmi_bus.h', inc / 'sweet_spmi_bus.h')
    shutil.copyfile(SRC, tmp / 'fuxi_hapread.c')
    subprocess.run(['clang', '-target', 'arm64-apple-macos13', '-std=gnu11', '-fsyntax-only', '-ffreestanding',
                    '-nostdlibinc', '-DFUXI', '-I', str(inc), *XNU_WARN, str(tmp / 'fuxi_hapread.c')], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--note', help='research_notes/loa-mic-rung.md: also check its H1 row against this list')
    args = ap.parse_args()
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-hapread.py'), '--check'], check=True)
    src = SRC.read_text()
    assert '\r' not in src
    static_checks(src)
    table_checks(src)
    if args.note:
        parse_note(args.note)
    with tempfile.TemporaryDirectory(prefix='fuxi-hapread-test-') as tmp:
        good = dynamic_checks(src, Path(tmp))
        xnu_syntax(Path(tmp))
    print('PASS: fuxi-hapread issues only EXT_READL on EE 0 observer channels (one store, no write opcode or RW channel), '
          'reads the note\'s %d registers in order and never SDAM46, says every stop condition, gives up on an '
          'unanswered read, does nothing without fuxihapread=1, and builds under XNU\'s warnings' % len(NOTE_ORDER))
    print('expected log of a clean dump (the model\'s bytes):')
    print('\n'.join('  ' + l for l in good))


if __name__ == '__main__':
    main()
