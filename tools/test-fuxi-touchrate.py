#!/usr/bin/env python3
"""Run fuxi_keys.c's scripted strokes (fk_flick, fk_swipe, fk_switcher) on a host clock, both ways.

Without fuxiflickus the strokes must be the old ones exactly (an 8 ms relative sleep after each whole-pixel point);
with it, points every N us on an absolute clock with no timer leeway, in 1/16 px. Also checks, from the source, that
the touch report-rate command stays behind fuxitouchrate=1 (research_notes/cam-ung-vsync.md). Host only: no device,
no network, nothing from upstream.
"""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-h1'


def function(source, name):
    match = re.search(r'^static (?:void|int|bool|unsigned)\n' + re.escape(name) + r'\([^;]*?\)\n\{', source, re.M)
    assert match, name
    depth, end = 1, match.end()
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end] + '\n'


MODEL = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define THREAD_UNINT 0
#define THREAD_CONTINUE_NULL ((void *)0)
#define TIMEOUT_URGENCY_SYS_CRITICAL 0x02
static uint64_t now_ns = 1000000, last_deadline;
static unsigned boot_us, perf, sleeps8, other_sleeps, waits, bad_waits, stall_at;
static int fk_script_chan;
static int PE_parse_boot_argn(const char *name, void *out, int size) {
 assert(!strcmp(name, "fuxiflickus") && size == 4);
 if (!boot_us) return 0;
 memcpy(out, &boot_us, 4);return 1;
}
static uint64_t mach_absolute_time(void) { return now_ns; }
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *abs) { *abs = ns; }
static int assert_wait_deadline_with_leeway(void *event, int interruptible, int urgency, uint64_t deadline, uint64_t leeway) {
 assert(event == &fk_script_chan && interruptible == THREAD_UNINT);
 if (urgency != TIMEOUT_URGENCY_SYS_CRITICAL || leeway) bad_waits++;
 waits++;last_deadline = deadline;return 0;
}
static int thread_block(void *next) {
 (void)next;if (last_deadline > now_ns) now_ns = last_deadline;now_ns += 37000;
 if (waits == stall_at) now_ns += 20000000;   /* preempted for 20 ms */
 return 0;
}
static void fuxi_perf_input(void) { perf++; }
static void fk_script_sleep(unsigned ms) { if (ms == 8) sleeps8++;else other_sleeps++;now_ns += ms * 1000000ull + 450000; }
struct point { uint64_t t; int down, x, y, sub; };
static struct point pts[4096];
static unsigned npts;
static void sweet_hid_touch(int finger, int down, int x, int y) {
 assert(finger == 0 && npts < 4096);pts[npts++] = (struct point){now_ns, down, x, y, 1};
}
static void sweet_hid_touch_sub(int finger, int down, int x, int y) {
 assert(finger == 0 && npts < 4096);pts[npts++] = (struct point){now_ns, down, x, y, 16};
}
static void reset(unsigned us) { npts = perf = sleeps8 = other_sleeps = waits = bad_waits = 0;boot_us = us; }
'''

TEST = r'''
static unsigned repeats(void) {   /* consecutive down points at the same position, which SweetHID drops */
 unsigned n = 0;
 for (unsigned i = 1; i < npts; i++) n += pts[i].down && pts[i - 1].down && pts[i].x == pts[i - 1].x && pts[i].y == pts[i - 1].y;
 return n;
}
int main(void) {
 /* No argument: the old strokes, unchanged: steps+1 whole-pixel points, an 8 ms sleep after each, then the lift. */
 fk_step_cached = ~0u;reset(0);fk_flick(950, 1300, 130, 1300, 1200);
 assert(npts == 152 && sleeps8 == 151 && !waits && perf == 151 && pts[0].sub == 1 && pts[0].x == 950);
 assert(pts[150].x == 130 && !pts[151].down && pts[151].x == 130);
 unsigned old_repeats = repeats();assert(old_repeats == 6);   /* the eased end in whole pixels */
 fk_step_cached = ~0u;reset(0);fk_swipe(540, 1800, 560, 700, 120);
 assert(npts == 17 && sleeps8 == 16 && pts[0].sub == 1 && pts[15].y == 700 && !pts[16].down);
 fk_step_cached = ~0u;reset(0);fk_switcher(400);
 assert(sleeps8 == 35 + 50 && other_sleeps == 1 && pts[npts - 1].down == 0 && pts[npts - 1].x == 740);
 /* Out of range: ignored, the old strokes. */
 fk_step_cached = ~0u;reset(1999);fk_flick(950, 1300, 130, 1300, 100);assert(sleeps8 == 13 && !waits);
 fk_step_cached = ~0u;reset(20001);fk_flick(950, 1300, 130, 1300, 100);assert(sleeps8 == 13 && !waits);
 /* fuxiflickus=4000: 301 points of 1/16 px on an absolute 4 ms clock (the 37 us each point costs does not add up). */
 fk_step_cached = ~0u;reset(4000);uint64_t t0 = now_ns;fk_flick(950, 1300, 130, 1300, 1200);
 assert(npts == 302 && waits == 301 && !bad_waits && !sleeps8 && perf == 301);
 for (unsigned i = 0; i < 301; i++) {
  assert(pts[i].down && pts[i].sub == 16 && pts[i].y == 1300 * 16);
  assert(pts[i].t == t0 + (i ? i * 4000000ull + 37000 : 0));
  if (i) assert(pts[i].x <= pts[i - 1].x);   /* eased, monotonic */
 }
 assert(pts[0].x == 950 * 16 && pts[300].x == 130 * 16 && !pts[301].down && pts[301].x == 130 * 16);
 unsigned fine_repeats = repeats();
 printf("1200 ms flick: whole-pixel 8 ms points repeat %u times, 1/16 px 4 ms points %u times\n", old_repeats, fine_repeats);
 assert(fine_repeats < 2 * old_repeats);   /* twice the points, under twice the repeats */
 /* fuxiflickus=8267: the panel's TE period, about 145 points over 1200 ms; the 100 ms flick 12 points. */
 fk_step_cached = ~0u;reset(8267);fk_flick(950, 1300, 130, 1300, 1200);assert(npts == 147 && waits == 146 && !bad_waits);
 fk_step_cached = ~0u;reset(8267);fk_flick(950, 1300, 130, 1300, 100);assert(npts == 14 && pts[12].x == 130 * 16);
 /* A stroke that fell behind (20 ms at its third point) moves its clock instead of sending a burst. */
 fk_step_cached = ~0u;reset(4000);stall_at = 3;fk_swipe(540, 1800, 560, 700, 40);stall_at = 0;
 assert(npts == 12 && pts[10].y == 700 * 16 && pts[0].x == 540 * 16 && pts[10].x == 560 * 16);
 for (unsigned i = 1; i < 11; i++) assert(pts[i].t - pts[i - 1].t >= 4000000 - 37000);
 assert(pts[3].t - pts[2].t > 20000000 && pts[4].t - pts[3].t < 4100000);
 /* The switcher: the same eased stroke, the hold at its end, the lift, then 1.5 s. */
 fk_step_cached = ~0u;reset(4000);fk_switcher(400);
 assert(other_sleeps == 1 && !sleeps8 && waits == 68 + 100 + 1 && !bad_waits);
 assert(pts[0].x == 680 * 16 && pts[0].y == 2375 * 16 && pts[npts - 2].x == 740 * 16 && pts[npts - 2].y == 1945 * 16);
 assert(!pts[npts - 1].down);
 puts("PASS: scripted strokes unchanged without fuxiflickus; with it, an absolute leeway-free clock in 1/16 px");
 return 0;
}
'''


def main():
    keys = (SRC / 'fuxi_keys.c').read_text()
    touch = (SRC / 'fuxi_touch.c').read_text()
    hid = (SRC / 'SweetHID.cpp').read_text()
    cached = re.search(r'^static unsigned fk_step_cached = ~0u;$', keys, re.M)
    assert cached
    body = cached[0] + '\n' + ''.join(function(keys, name) for name in (
        'fk_step_us', 'fk_step_wait', 'fk_stroke_fine', 'fk_swipe', 'fk_flick', 'fk_switcher'))
    with tempfile.TemporaryDirectory(prefix='fuxi-touchrate-') as tmp:
        source, binary = Path(tmp) / 'strokes.c', Path(tmp) / 'strokes'
        source.write_text(MODEL + body + TEST)
        subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    # Default boots write nothing new to the IC and change no HID number: the gates, read from the source.
    assert 'PE_parse_boot_argn("fuxiflickus"' in keys and keys.count('fk_stroke_fine(') == 4  # definition, three users
    assert 'sweet_hid_touch_sub(' in keys and keys.count('fk_step_us()') == 4
    assert 'if (ft.rate != 1) {' in touch and 'ft.fine = ft.fine == 1;' in touch
    assert re.search(r'^#define SWEET_TOUCH_SUB 16$', hid, re.M) and re.search(r'^#define FT_SUB       16 ', touch, re.M)
    assert 'changed by the event system' in hid and 'SWEET_TOUCH_RATE_LOGS 16' in hid
    print('PASS: fuxitouchrate/fuxitouchfine/fuxiflickus are opt-in; property values are logged')


if __name__ == '__main__':
    main()
