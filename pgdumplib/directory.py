# coding=utf-8
"""
Directory
=========
Implements the Dump class for Directory based

"""
import pathlib

from pgdumplib import models, reader


def load(filepath):
    """Load a pg_dump file created with -Fd

    :raises: `ValueError`
    :rtype: pgdumplib.models.Dump

    """
    path = pathlib.Path(filepath)
    if not path.exists():
        raise ValueError('Path {!r} does not exist'.format(path))

    toc = path / 'toc.dat'
    if not toc.exists():
        raise ValueError('Missing ToC @ {!r}'.format(path))

    with open(toc, 'rb') as handle:
        rdr = reader.Reader(handle)
        return models.Dump(str(path), rdr.read_toc())
