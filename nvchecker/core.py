# vim: se sw=2:
# MIT licensed
# Copyright (c) 2013-2018 lilydjwg <lilydjwg@gmail.com>, et al.

import os
import sys
import configparser
import asyncio
import logging
import structlog

from .lib import nicelogger
from .get_version import get_version
from .source import session
from . import slogconf

from . import __version__

logger = structlog.get_logger(logger_name=__name__)

def add_common_arguments(parser):
  parser.add_argument('-l', '--logging',
                      choices=('debug', 'info', 'warning', 'error'), default='info',
                      help='logging level (default: info)')
  parser.add_argument('--logger', choices=['pretty', 'json'],
                      default='pretty',
                      help='select which logger to use')
  parser.add_argument('-V', '--version', action='store_true',
                      help='show version and exit')
  parser.add_argument('file', metavar='FILE', nargs='?', type=open,
                      help='software version source file')

def process_common_arguments(args):
  '''return True if should stop'''
  if args.logger == 'pretty':
    slogconf.fix_logging()
    nicelogger.enable_pretty_logging(
      getattr(logging, args.logging.upper()))
    structlog.configure(
      processors=[
        slogconf.exc_info,
        slogconf.stdlib_renderer,
      ],
      logger_factory=structlog.PrintLoggerFactory(
        file=open(os.devnull, 'w')),
    )
  else:
    structlog.configure(
      processors=[
        slogconf.exc_info,
        structlog.processors.format_exc_info,
        slogconf.json_renderer,
      ],
      logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )

  if args.version:
    progname = os.path.basename(sys.argv[0])
    print('%s v%s' % (progname, __version__))
    return True

def safe_overwrite(fname, data, *, method='write', mode='w', encoding=None):
  # FIXME: directory has no read perm
  # FIXME: symlinks and hard links
  tmpname = fname + '.tmp'
  # if not using "with", write can fail without exception
  with open(tmpname, mode, encoding=encoding) as f:
    getattr(f, method)(data)
    # see also: https://thunk.org/tytso/blog/2009/03/15/dont-fear-the-fsync/
    f.flush()
    os.fsync(f.fileno())
  # if the above write failed (because disk is full etc), the old data should be kept
  os.rename(tmpname, fname)

def read_verfile(file):
  v = {}
  try:
    with open(file) as f:
      for l in f:
        name, ver = l.rstrip().split(None, 1)
        v[name] = ver
  except FileNotFoundError:
    pass
  return v

def write_verfile(file, versions):
  # sort using only alphanums, as done by the sort command, and needed by
  # comm command
  data = ['%s %s\n' % item
          for item in sorted(versions.items(), key=lambda i: (''.join(filter(str.isalnum, i[0])), i[1]))]
  safe_overwrite(file, data, method='writelines')

class Source:
  oldver = newver = None

  def __init__(self, file):
    self.config = config = configparser.ConfigParser(
      dict_type=dict, allow_no_value=True
    )
    self.name = file.name
    config.read_file(file)
    if '__config__' in config:
      c = config['__config__']

      if 'oldver' in c and 'newver' in c:
        d = os.path.dirname(file.name)
        self.oldver = os.path.expandvars(os.path.expanduser(
          os.path.join(d, c.get('oldver'))))
        self.newver = os.path.expandvars(os.path.expanduser(
          os.path.join(d, c.get('newver'))))

      self.max_concurrent = c.getint('max_concurrent', 20)
      session.nv_config = config["__config__"]

    else:
      self.max_concurrent = 20

  async def check(self):
    if self.oldver:
      self.oldvers = read_verfile(self.oldver)
    else:
      self.oldvers = {}
    self.curvers = self.oldvers.copy()

    token_q = asyncio.Queue(maxsize=self.max_concurrent)

    async def worker(name, conf):
      await token_q.get()
      try:
        ret = await get_version(name, conf)
        return name, ret
      except Exception as e:
        return name, e

    async def token_filler(n):
      for _ in range(n):
        await token_q.put(True)

    config = self.config
    futures = []
    for name in config.sections():
      if name == '__config__':
        continue

      conf = config[name]
      conf['oldver'] = self.oldvers.get(name, None)
      fu = asyncio.ensure_future(worker(name, conf))
      futures.append(fu)

    filler_fu = asyncio.ensure_future(token_filler(len(futures)))

    for fu in asyncio.as_completed(futures):
      name, result = await fu
      if isinstance(result, Exception):
        logger.error('unexpected error happened', name=name, exc_info=result)
      elif result is not None:
        self.print_version_update(name, result)

    await filler_fu

    if self.newver:
      write_verfile(self.newver, self.curvers)

  def print_version_update(self, name, version):
    oldver = self.oldvers.get(name, None)
    if not oldver or oldver != version:
      logger.info('updated', name=name, version=version, old_version=oldver)
      self.curvers[name] = version
      self.on_update(name, version, oldver)
    else:
      logger.debug('up-to-date', name=name, version=version)

  def on_update(self, name, version, oldver):
    pass

  def __repr__(self):
    return '<Source from %r>' % self.name
