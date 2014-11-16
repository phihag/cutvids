#!/usr/bin/env python3
# coding: utf-8

from __future__ import unicode_literals, print_function

import argparse
import collections
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile


VideoTask = collections.namedtuple(
    'VideoTask', ('input_files', 'output_file', 'start', 'end'))


class FileNotFoundError(BaseException):
    pass


def parse_seconds(token):
    if token == '-':
        return None
    m = re.match(r'(?:(?P<minutes>[0-9]+):)?(?P<seconds>[0-9]+)$', token)
    res = int(m.group('seconds'))
    if m.group('minutes'):
        res += 60 * int(m.group('minutes'))
    return res


def parse_tokens(line):
    while line:
        line = line.strip()
        if not line:
            break
        if line[:1] == '"':
            token, _, line = line[1:].partition('"')
            yield token
        else:
            token, _, line = line.partition(' ')
            assert '"' not in token
            yield token


def parse_video_tasks(fn):
    with io.open(fn, encoding='utf-8') as inf:
        for line in inf:
            if not line.strip() or line.startswith('#'):
                break
            tokens = list(parse_tokens(line))
            assert 2 <= len(tokens) <= 4
            input_files = tokens[0].split('+')
            start = None if len(tokens) < 3 else parse_seconds(tokens[2])
            end = None if len(tokens) < 4 else parse_seconds(tokens[3])
            yield VideoTask(input_files, tokens[1], start, end)


def cutvid_commands(vt, indir, outdir):
    input_files = [find_file(indir, f) for f in vt.input_files]
    output_fn = os.path.join(outdir, vt.output_file)
    tmpfiles = []
    try:
        if len(input_files) == 1 and vt.start and vt.end:
            yield [
                'ffmpeg', '-i', input_files[0], '-y', '-c', 'copy',
                '-ss', '%d' % vt.start, '-t', '%d' % (vt.end - vt.start),
                output_fn + '.part']
            yield [
                'mv', '--', output_fn + '.part', output_fn,
            ]
            return
    
        if vt.start:
            tmph, tmpfile = tempfile.mkstemp(
                prefix=os.path.basename(input_files[0]) + '.',
                suffix='.first_part.mp4', dir=outdir)
            tmpfiles.append(tmpfile)
            os.close(tmph)
            yield [
                'ffmpeg', '-i', input_files[0], '-y', '-c', 'copy',
                '-ss', '%d' % vt.start,
                tmpfile,
            ]
            input_files[0] = tmpfile
        if vt.end:
            tmph, tmpfile = tempfile.mkstemp(
                prefix=os.path.basename(input_files[-1]) + '.',
                suffix='.end_part.mp4', dir=outdir)
            tmpfiles.append(tmpfile)
            os.close(tmph)
            yield [
                'ffmpeg', '-i', input_files[-1], '-y',
                '-t', '%d' % vt.end,
                '-c', 'copy', '-f', 'mp4',
                tmpfile,
            ]
            input_files[-1] = tmpfile
        if len(input_files) == 1:
            yield [
                'cp', '--', input_files[0], output_fn + '.part',
            ]
        else:
            tmph, tmpfile = tempfile.mkstemp(
                prefix=vt.output_file, suffix='.concat_list.txt', dir=outdir)
            tmpfiles.append(tmpfile)
            concat_str = ('\n'.join(
                "file '%s'" % inf for inf in input_files)) + '\n\n'
            tmpf = os.fdopen(tmph, mode='r+b')
            tmpf.write(concat_str.encode('ascii'))
            tmpf.close()
            yield [
                'ffmpeg', '-y',
                '-f', 'concat', '-i', tmpfile,
                '-c', 'copy', '-f', 'mp4',
                output_fn + '.part',
            ]
    finally:
        for fn in tmpfiles:
            os.remove(fn)

    yield [
        'mv', '--', output_fn + '.part', output_fn,
    ]


def is_uploaded(cwd, vt):
    return os.path.exists(os.path.join(cwd, vt.output_file))


def is_cut(outdir, vt):
    return os.path.exists(os.path.join(outdir, vt.output_file))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('index_file', metavar='FILE')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print commands instead of executing them')
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Print out more information')
    parser.add_argument(
        '-u', '--upload', action='store_true',
        help='Upload videos after cutting them')
    parser.add_argument(
        '--upload-config', metavar='FILE',
        help='Configuration file for the upload.')
    args = parser.parse_args()

    cwd = os.getcwd()
    uploading_dir = os.path.join(cwd, 'uploading')
    if not os.path.exists(uploading_dir):
        os.mkdir(uploading_dir)
    tasks = list(parse_video_tasks(args.index_file))
    for vt in tasks:
        if is_uploaded(cwd, vt) or is_cut(uploading_dir, vt):
            if args.verbose:
                sys.stdout.write('%s: Conversion done.\n' % vt.output_file)
            continue
        sys.stdout.write('%s: Converting\n' % vt.output_file)
        sys.stdout.flush()
        for c in cutvid_commands(vt, cwd, uploading_dir):
            if args.verbose:
                sys.stdout.write('  ' + ' '.join(shlex.quote(a) for a in c))
                sys.stdout.flush()
            if not args.dry_run:
                subprocess.check_output(
                    c, stderr=subprocess.DEVNULL, stdin=subprocess.PIPE)
            sys.stdout.write('\n')
            sys.stdout.flush()

    if not args.upload:
        return

    upload_config = {}
    if args.upload_config:
        with io.open(args.upload_config, 'r', encoding='utf-8') as cfgf:
            upload_config.update(json.load(cfgf))

    for vt in tasks:
        if is_uploaded(cwd, vt):
            if args.verbose:
                sys.stdout.write('%s: Already uploaded.\n' % vt.output_file)
                sys.stdout.flush()
            continue
        sys.stdout.write('%s: Uploading' % vt.output_file)
        sys.stdout.flush()
        tmp_fn = os.path.join(uploading_dir, vt.output_file)
        title = os.path.splitext(vt.output_file)[0]
        upload_cmd = [
            'youtube_upload',
            '--category', upload_config['category'],
            '--email', upload_config['email'],
            '--password', upload_config['password'],
            '-t', title,
            '--', tmp_fn,
        ]
        subprocess.check_call(upload_cmd)
        sys.stdout.write('\n')
        sys.stdout.flush()

        os.rename(tmp_fn, os.path.join(cwd, vt.output_file))


def find_file(root_dir, basename):
    found = None
    for path, _, files in os.walk(root_dir):
        if basename in files:
            if found:
                raise FileNotFoundError(
                    'Found two files with basename %r: %s and %s' % (
                        basename, found, os.path.join(path, basename)))
            found = os.path.join(path, basename)
    if not found:
        raise ValueError('Could not find input file %r' % basename)
    return found


if __name__ == '__main__':
    main()
