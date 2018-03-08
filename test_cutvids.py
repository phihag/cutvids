#!/usr/bin/env python
# coding: utf-8

from __future__ import unicode_literals

import unittest

import cutvids


class TestCutvids(unittest.TestCase):
    def test_parse_seconds(self):
        self.assertEqual(cutvids.parse_seconds('-'), None)
        self.assertEqual(cutvids.parse_seconds(None), None)
        self.assertEqual(cutvids.parse_seconds('123'), 123)
        self.assertEqual(cutvids.parse_seconds(123), 123)
        self.assertEqual(cutvids.parse_seconds(123.4), 123.4)
        self.assertEqual(cutvids.parse_seconds('123.4'), 123.4)
        self.assertEqual(cutvids.parse_seconds('1:1:40'), 3700)

    def test_parse_segment(self):
        self.assertEqual(cutvids.parse_segment({
            'start': '5',
            'end': '123',
        }), cutvids.Segment(5, 123))
        self.assertEqual(
            cutvids.parse_segment(['123.5', '12:34.5']),
            cutvids.Segment(123.5, 754.5))
