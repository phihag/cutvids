#!/usr/bin/env python3
# coding: utf-8

from __future__ import unicode_literals, print_function

import argparse
import collections
import errno
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile


VideoTask = collections.namedtuple(
    'VideoTask',
    ('input_files', 'output_file', 'description', 'segments', 'boost_volume',
     'privacy', 'upload'))


Segment = collections.namedtuple(
    'Segment', ('start', 'end'))


class FileNotFoundError(BaseException):
    pass


def parse_seconds(token):
    if token == '-':
        return None
    m = re.match(
        r'(?:(?:(?P<hours>[0-9]+):)?(?P<minutes>[0-9]+):)?(?P<secs>[0-9]+)$',
        token)
    res = int(m.group('secs'))
    if m.group('minutes'):
        res += 60 * int(m.group('minutes'))
    if m.group('hours'):
        res += 60 * 60 * int(m.group('hours'))
    return res


def parse_tokens(line):
    while line:
        line = line.strip()
        if line.startswith('#'):
            continue
        if line[:1] == '"':
            token, _, line = line[1:].partition('"')
            yield token
        elif line[:1] == "'":
            token, _, line = line[1:].partition("'")
            yield token
        else:
            token, _, line = line.partition(' ')
            assert '"' not in token
            yield token


def parse_video_tasks(fn):
    with io.open(fn, encoding='utf-8') as inf:
        for line in inf:
            if not line.strip() or line.startswith('#'):
                continue
            tokens = list(parse_tokens(line))
            assert 2 <= len(tokens) <= 5
            input_files = tokens[0].split('+')
            output_file = tokens[1]
            if not re.search(r'\.(?:mp4|webm|ogv)$', output_file):
                output_file += '.mp4'
            start = None if len(tokens) < 3 else parse_seconds(tokens[2])
            end = None if len(tokens) < 4 else parse_seconds(tokens[3])

            if len(tokens) >= 5:
                extra_data = json.loads(tokens[4])
            else:
                extra_data = {}

            privacy = extra_data.get('privacy')
            description = extra_data.get('description')
            segments_in = extra_data.get('segments')
            upload = extra_data.get('upload', True)
            if segments_in:
                assert not start
                assert not end
                segments = [Segment(
                    parse_seconds(s['start']), parse_seconds(s['end']))
                    for s in segments_in]
            else:
                segments = [Segment(start, end)]
            yield VideoTask(
                input_files, output_file, description, segments,
                extra_data.get('boost_volume'), privacy, upload)


def cutvid_commands(vt, indir, outdir):
    input_files = [find_file(indir, f) for f in vt.input_files]
    output_fn = os.path.join(outdir, vt.output_file)
    tmpfiles = []

    ext = os.path.splitext(vt.output_file)[1]

    def _concat_cmd(in_fns, out_fn):
        concat_fn = out_fn + '.concat_list.txt'
        concat_str = ('\n'.join(
            "file '%s'" % infn for infn in in_fns)) + '\n\n'
        tmpfiles.append(concat_fn)
        with io.open(concat_fn, 'w', encoding='utf-8') as concat_f:
            concat_f.write(concat_str)

        return [
            'ffmpeg', '-y',
            '-safe', '0',  # because we use absolute paths at the moment
            '-f', 'concat', '-i', concat_fn,
            '-c', 'copy',
            out_fn,
        ]

    try:
        if len(vt.segments) > 1:
            if len(input_files) > 1:
                inf = output_fn + '.whole%s' % ext
                tmpfiles.append(inf)
                yield _concat_cmd(input_files, inf)
            else:
                inf = input_files[0]

            segment_files = []
            for segment_num, s in enumerate(vt.segments):
                segment_fn = output_fn + '.segment%d%s' % (segment_num, ext)
                tmpfiles.append(segment_fn)
                segment_files.append(segment_fn)
                ffmpeg_opts = []
                if s.start is not None:
                    ffmpeg_opts += ['-ss', '%d' % s.start]
                    if s.end is not None:
                        assert s.end > s.start
                        ffmpeg_opts += ['-t', '%d' % (s.end - s.start)]
                elif s.end is not None:
                    ffmpeg_opts += ['-t', '%d' % s.end]

                tmpfiles.append(segment_fn)
                yield ([
                    'ffmpeg', '-i', input_files[0], '-y'] +
                    ffmpeg_opts +
                    ['-c', 'copy',
                     segment_fn])

            yield _concat_cmd(segment_files, output_fn + '.part%s' % ext)
            if vt.boost_volume:
                yield [
                    'mv', '--', output_fn + '.part%s' % ext,
                    output_fn + '.filter_input%s' % ext,
                ]
                tmpfiles.append(output_fn + '.filter_input%s' % ext)
                yield ([
                    'ffmpeg', '-i', output_fn + '.filter_input%s' % ext,
                    '-y'] +
                    ffmpeg_opts +
                    ['-c:v', 'copy', '-af', 'volume=%s' % vt.boost_volume,
                     output_fn + '.part%s' % ext])
            yield [
                'mv', '--', output_fn + '.part%s' % ext, output_fn,
            ]
            return

        # Only 1 segment, use simpler calls
        assert not vt.boost_volume, 'boost_volume not supported here'
        start = vt.segments[0].start
        end = vt.segments[0].end
        if len(input_files) == 1 and start and end:
            yield [
                'ffmpeg',
                '-noaccurate_seek',
                '-i', input_files[0], '-y',
                '-ss', '%d' % start,
                '-c', 'copy',
                '-t', '%d' % (end - start),
                '-avoid_negative_ts', 'make_zero',
                output_fn + '.part%s' % ext]
            yield [
                'mv', '--', output_fn + '.part%s' % ext, output_fn,
            ]
            return

        if start:
            tmpfile = output_fn + '.first_part%s' % ext
            tmpfiles.append(tmpfile)
            yield [
                'ffmpeg', '-i', input_files[0], '-y',
                '-ss', '%d' % start,
                '-c', 'copy',
                tmpfile,
            ]
            input_files[0] = tmpfile
        if end:
            tmpfile = output_fn + '.end_part%s' % ext
            tmpfiles.append(tmpfile)
            yield [
                'ffmpeg', '-i', input_files[-1], '-y',
                '-t', '%d' % end,
                '-c', 'copy',
                tmpfile,
            ]
            input_files[-1] = tmpfile
        if len(input_files) == 1:
            yield [
                'cp', '--', input_files[0], output_fn + '.part%s' % ext,
            ]
        else:
            tmph, tmpfile = tempfile.mkstemp(
                prefix=vt.output_file, suffix='.concat_list.txt', dir=outdir)
            tmpfiles.append(tmpfile)
            concat_str = ('\n'.join(
                "file '%s'" % inf for inf in input_files)) + '\n\n'
            tmpf = os.fdopen(tmph, mode='r+b')
            tmpf.write(concat_str.encode('utf-8'))
            tmpf.close()
            yield [
                'ffmpeg', '-y',
                '-safe', '0',
                '-f', 'concat', '-i', tmpfile,
                '-c', 'copy',
                output_fn + '.part%s' % ext,
            ]
    finally:
        for fn in tmpfiles:
            try:
                os.remove(fn)
            except OSError as ose:
                if ose.errno != errno.ENOENT:
                    raise

    yield [
        'mv', '--', output_fn + '.part%s' % ext, output_fn,
    ]


def is_uploaded(cwd, vt):
    return os.path.exists(os.path.join(cwd, vt.output_file))


def is_cut(outdir, vt):
    return os.path.exists(os.path.join(outdir, vt.output_file))


def find_upload_bin():
    for candidate in ('youtube_upload', 'youtube-upload', 'yt-upload'):
        try:
            output_bytes = subprocess.check_output(['which', candidate])
            return output_bytes.decode().strip()
        except subprocess.CalledProcessError:
            continue
    raise SystemError('Cannot find youtube_upload!')


def calc_upload_cmd(upload_config, title, vt, tmp_fn):
    binpath = find_upload_bin()
    if binpath.endswith('youtube_upload'):  # old version?
        res = [
            binpath,
            '--category', upload_config['category'],
            '--email', upload_config['email'],
            '--password', upload_config['password'],
            '-t', title,
        ]
        if vt.privacy:
            if vt.privacy == 'public':
                res += ['--public']
            elif vt.privacy == 'private':
                res += ['--private']
            elif vt.privacy == 'unlisted':
                res += ['--unlisted']
            else:
                raise Exception('Unsupported privacy %r' % vt.privacy)
        if vt.description is not None:
            res += ['--description', vt.description]
        res += ['--', tmp_fn]
    else:  # https://github.com/tokland/youtube-upload
        res = [
            binpath,
            '--category', upload_config['category'],
            '-t', title,
        ]
        if vt.description is not None:
            res += ['--description', vt.description]
        if vt.privacy:
            res += ['--privacy', vt.privacy]
        res += ['--', tmp_fn]

    return res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'index_file', metavar='FILE',
        nargs='?', default='./renames',
        help='Description of videos, one per line. '
             'Format: Source-videos(separated with +) "destination video name"'
             ' [offset in first video, - for none] [end offset in last video]')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print commands instead of executing them')
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Print out more information')
    parser.add_argument(
        '-u', '--upload', action='store_true',
        help='Upload videos to YouTube after cutting them')
    parser.add_argument(
        '--indir', metavar='DIR',
        help='Directory to search source videos in')
    parser.add_argument(
        '--upload-config', metavar='FILE', default='~/.config/cutvids.conf',
        help='JSON configuration file for the upload. '
             'A dictionary with the keys email, password and category.')
    parser.add_argument(
        '--show-tasks', action='store_true',
        help='Show the video tasks and exit')
    parser.add_argument(
        '--find-upload-bin', action='store_true',
        help='Find youtube-upload and exit')
    args = parser.parse_args()

    if args.find_upload_bin:
        try:
            bin_path = find_upload_bin()
            print('youtube_upload is at %s' % bin_path)
            return 0
        except SystemError as se:
            sys.stderr.write(se.args[0] + '\n')
            return 1

    cwd = os.getcwd()
    indir = cwd if args.indir is None else args.indir
    uploading_dir = os.path.join(cwd, 'uploading')
    if not os.path.exists(uploading_dir):
        os.mkdir(uploading_dir)
    tasks = list(parse_video_tasks(args.index_file))

    if args.show_tasks:
        for t in tasks:
            print(t)
        return 0

    for vt in tasks:
        if is_uploaded(cwd, vt) or is_cut(uploading_dir, vt):
            if args.verbose:
                sys.stdout.write('%s: Conversion done.\n' % vt.output_file)
            continue
        sys.stdout.write('%s: Converting' % vt.output_file)
        sys.stdout.flush()
        for c in cutvid_commands(vt, indir, uploading_dir):
            if args.verbose:
                sys.stdout.write('\n  ' + ' '.join(shlex.quote(a) for a in c))
                sys.stdout.flush()
            if not args.dry_run:
                p = subprocess.Popen(
                    c, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
                stdout, stderr = p.communicate()
                if p.returncode != 0:
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    sys.stdout.buffer.write(stderr)
                    sys.stdout.buffer.flush()
                    raise OSError('ffmpeg failed, see output above')
        sys.stdout.write('\n')
        sys.stdout.flush()

    if not args.upload:
        return

    upload_config = {}
    if args.upload_config:
        config_fn = os.path.expanduser(args.upload_config)
        with io.open(config_fn, 'r', encoding='utf-8') as cfgf:
            upload_config.update(json.load(cfgf))

    for vt in tasks:
        if not vt.upload:
            continue
        if is_uploaded(cwd, vt):
            if args.verbose:
                sys.stdout.write('%s: Already uploaded.\n' % vt.output_file)
                sys.stdout.flush()
            continue
        sys.stdout.write('%s: Uploading' % vt.output_file)
        sys.stdout.flush()
        tmp_fn = os.path.join(uploading_dir, vt.output_file)
        title = os.path.splitext(vt.output_file)[0]
        upload_cmd = calc_upload_cmd(upload_config, title, vt, tmp_fn)
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
    sys.exit(main())
