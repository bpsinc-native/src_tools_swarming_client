#!/usr/bin/env python
# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import binascii
import random
import hashlib
import logging
import os
import StringIO
import sys
import threading
import time
import unittest
import zlib

BASE_PATH = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_PATH)
sys.path.insert(0, ROOT_DIR)

import auto_stub
import isolateserver


class TestCase(auto_stub.TestCase):
  def setUp(self):
    super(TestCase, self).setUp()
    self.mock(isolateserver.net, 'url_open', self._url_open)
    self.mock(isolateserver.net, 'sleep_before_retry', lambda *_: None)
    self._lock = threading.Lock()
    self._requests = []

  def tearDown(self):
    try:
      self.assertEqual([], self._requests)
    finally:
      super(TestCase, self).tearDown()

  def _url_open(self, url, **kwargs):
    logging.warn('url_open(%s, %s)', url[:500], str(kwargs)[:500])
    with self._lock:
      if not self._requests:
        return None
      # Ignore 'stream' argument, it's not important for these tests.
      kwargs.pop('stream', None)
      for i, n in enumerate(self._requests):
        if n[0] == url:
          _, expected_kwargs, result = self._requests.pop(i)
          self.assertEqual(expected_kwargs, kwargs)
          if result is not None:
            return isolateserver.net.HttpResponse.get_fake_response(result, url)
          return None
    self.fail('Unknown request %s' % url)


class IsolateServerArchiveTest(TestCase):
  def setUp(self):
    super(IsolateServerArchiveTest, self).setUp()
    self.mock(isolateserver, 'randomness', lambda: 'not_really_random')
    self.mock(sys, 'stdout', StringIO.StringIO())

  def test_present(self):
    files = [
      os.path.join(BASE_PATH, 'isolateserver', f)
      for f in ('small_file.txt', 'empty_file.txt')
    ]
    sha1encoded = ''.join(
        binascii.unhexlify(isolateserver.sha1_file(f)) for f in files)
    path = 'http://random/'
    self._requests = [
      (path + 'content/get_token', {}, 'foo bar'),
      (
        path + 'content/contains/default-gzip?token=foo%20bar',
        {'data': sha1encoded, 'content_type': 'application/octet-stream'},
        '\1\1',
      ),
    ]
    result = isolateserver.main(['archive', '--isolate-server', path] + files)
    self.assertEqual(0, result)

  def test_missing(self):
    files = [
      os.path.join(BASE_PATH, 'isolateserver', f)
      for f in ('small_file.txt', 'empty_file.txt')
    ]
    sha1s = map(isolateserver.sha1_file, files)
    sha1encoded = ''.join(map(binascii.unhexlify, sha1s))
    compressed = [
        zlib.compress(
            open(f, 'rb').read(),
            isolateserver.compression_level(f))
        for f in files
    ]
    path = 'http://random/'
    self._requests = [
      (path + 'content/get_token', {}, 'foo bar'),
      (
        path + 'content/contains/default-gzip?token=foo%20bar',
        {'data': sha1encoded, 'content_type': 'application/octet-stream'},
        '\0\0',
      ),
      (
        path + 'content/store/default-gzip/%s?token=foo%%20bar' % sha1s[0],
        {'data': compressed[0], 'content_type': 'application/octet-stream'},
        'ok',
      ),
      (
        path + 'content/store/default-gzip/%s?token=foo%%20bar' % sha1s[1],
        {'data': compressed[1], 'content_type': 'application/octet-stream'},
        'ok',
      ),
    ]
    result = isolateserver.main(['archive', '--isolate-server', path] + files)
    self.assertEqual(0, result)

  def test_large(self):
    content = ''
    compressed = ''
    while (
        len(compressed) <= isolateserver.MIN_SIZE_FOR_DIRECT_BLOBSTORE):
      # The goal here is to generate a file, once compressed, is at least
      # MIN_SIZE_FOR_DIRECT_BLOBSTORE.
      content += ''.join(chr(random.randint(0, 255)) for _ in xrange(20*1024))
      compressed = zlib.compress(
          content, isolateserver.compression_level('foo.txt'))

    s = hashlib.sha1(content).hexdigest()
    infiles = {
      'foo.txt': {
        's': len(content),
        'h': s,
      },
    }
    path = 'http://random/'
    sha1encoded = binascii.unhexlify(s)
    content_type, body = isolateserver.encode_multipart_formdata(
                [('token', 'foo bar')], [('content', s, compressed)])

    self._requests = [
      (path + 'content/get_token', {}, 'foo bar'),
      (
        path + 'content/contains/default-gzip?token=foo%20bar',
        {'data': sha1encoded, 'content_type': 'application/octet-stream'},
        '\0',
      ),
      (
        path + 'content/generate_blobstore_url/default-gzip/%s' % s,
        {'data': [('token', 'foo bar')]},
        'an_url/',
      ),
      (
        'an_url/',
        {'data': body, 'content_type': content_type, 'retry_50x': False},
        'ok',
      ),
    ]

    self.mock(isolateserver, 'read_and_compress', lambda x, y: compressed)
    result = isolateserver.upload_sha1_tree(
          base_url=path,
          indir=os.getcwd(),
          infiles=infiles,
          namespace='default-gzip')

    self.assertEqual(0, result)

  def test_batch_files_for_check(self):
    items = {
      'foo': {'s': 12},
      'bar': {},
      'blow': {'s': 0},
      'bizz': {'s': 1222},
      'buzz': {'s': 1223},
    }
    expected = [
      [
        ('buzz', {'s': 1223}),
        ('bizz', {'s': 1222}),
        ('foo', {'s': 12}),
        ('blow', {'s': 0}),
      ],
    ]
    batches = list(isolateserver.batch_files_for_check(items))
    self.assertEqual(batches, expected)

  def test_get_files_to_upload(self):
    items = {
      'foo': {'s': 12},
      'bar': {},
      'blow': {'s': 0},
      'bizz': {'s': 1222},
      'buzz': {'s': 1223},
    }
    missing = {
      'bizz': {'s': 1222},
      'buzz': {'s': 1223},
    }

    def mock_check(url, items):
      self.assertEqual('fakeurl', url)
      return [item for item in items if item[0] in missing]
    self.mock(isolateserver, 'check_files_exist_on_server', mock_check)

    # 'get_files_to_upload' doesn't guarantee order of its results, so convert
    # list of pairs to unordered dict and compare dicts.
    result = dict(isolateserver.get_files_to_upload('fakeurl', items))
    self.assertEqual(result, missing)

  def test_upload_blobstore_simple(self):
    content = 'blob_content'
    s = hashlib.sha1(content).hexdigest()
    path = 'http://example.com:80/'
    data = [('token', 'foo bar')]
    content_type, body = isolateserver.encode_multipart_formdata(
        data[:], [('content', s, 'blob_content')])
    self._requests = [
      (
        path + 'gen_url?foo#bar',
        {'data': data[:]},
        'an_url/',
      ),
      (
        'an_url/',
        {'data': body, 'content_type': content_type, 'retry_50x': False},
        'ok42',
      ),
    ]
    result = isolateserver.upload_hash_content_to_blobstore(
        path + 'gen_url?foo#bar', data[:], s, content)
    self.assertEqual('ok42', result)

  def test_upload_blobstore_retry_500(self):
    content = 'blob_content'
    s = hashlib.sha1(content).hexdigest()
    path = 'http://example.com:80/'
    data = [('token', 'foo bar')]
    content_type, body = isolateserver.encode_multipart_formdata(
        data[:], [('content', s, 'blob_content')])
    self._requests = [
      (
        path + 'gen_url?foo#bar',
        {'data': data[:]},
        'an_url/',
      ),
      (
        'an_url/',
        {'data': body, 'content_type': content_type, 'retry_50x': False},
        # Let's say an HTTP 500 was returned.
        None,
      ),
      # In that case, a new url must be generated since the last one may have
      # been "consumed".
      (
        path + 'gen_url?foo#bar',
        {'data': data[:]},
        'an_url/',
      ),
      (
        'an_url/',
        {'data': body, 'content_type': content_type, 'retry_50x': False},
        'ok42',
      ),
    ]
    result = isolateserver.upload_hash_content_to_blobstore(
        path + 'gen_url?foo#bar', data[:], s, content)
    self.assertEqual('ok42', result)


class IsolateServerDownloadTest(TestCase):
  def test_download_two_files(self):
    # Test downloading two files.
    actual = {}
    def out(key, generator):
      actual[key] = ''.join(generator)
    self.mock(isolateserver, 'file_write', out)
    server = 'http://example.com'
    self._requests = [
      (
        server + '/content/retrieve/default-gzip/sha-1',
        {'read_timeout': 60, 'retry_404': True},
        zlib.compress('Coucou'),
      ),
      (
        server + '/content/retrieve/default-gzip/sha-2',
        {'read_timeout': 60, 'retry_404': True},
        zlib.compress('Bye Bye'),
      ),
    ]
    cmd = [
      'download',
      '--isolate-server', server,
      '--target', ROOT_DIR,
      '--file', 'sha-1', 'path/to/a',
      '--file', 'sha-2', 'path/to/b',
    ]
    self.assertEqual(0, isolateserver.main(cmd))
    expected = {
      os.path.join(ROOT_DIR, 'path/to/a'): 'Coucou',
      os.path.join(ROOT_DIR, 'path/to/b'): 'Bye Bye',
    }
    self.assertEqual(expected, actual)


def upload_file(item, _dest, _size):
  if type(item) == type(Exception) and issubclass(item, Exception):
    raise item()
  elif isinstance(item, int):
    time.sleep(int(item) / 100)


class RemoteOperationTest(auto_stub.TestCase):
  def test_remote_no_errors(self):
    files_to_handle = 50
    remote = isolateserver.RemoteOperation(upload_file)

    for i in range(files_to_handle):
      remote.add_item(
          isolateserver.RemoteOperation.MED,
          'obj%d' % i,
          'dest%d' % i,
          isolateserver.UNKNOWN_FILE_SIZE)

    items = sorted(remote.join())
    expected = sorted('obj%d' % i for i in range(files_to_handle))
    self.assertEqual(expected, items)

  def test_remote_with_errors(self):
    remote = isolateserver.RemoteOperation(upload_file)

    def RaiseIOError(*_):
      raise IOError()
    remote._do_item = RaiseIOError
    remote.add_item(isolateserver.RemoteOperation.MED, 'ignored', '',
                    isolateserver.UNKNOWN_FILE_SIZE)
    self.assertRaises(IOError, remote.join)
    self.assertEqual([], remote.join())


if __name__ == '__main__':
  logging.basicConfig(
      level=(logging.DEBUG if '-v' in sys.argv else logging.ERROR))
  unittest.main()
