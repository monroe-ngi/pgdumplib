"""
The :py:class:`~pgdumplib.dump.Dump` class exposes methods to
:py:meth:`load <pgdumplib.dump.Dump.load>` an existing dump,
to :py:meth:`add an entry <pgdumplib.dump.Dump.add_entry>` to a dump,
to :py:meth:`add table data <pgdumplib.dump.Dump.add_data>` to a dump,
to :py:meth:`add blob data <pgdumplib.dump.Dump.add_blob>` to a dump,
and to :py:meth:`save <pgdumplib.dump.Dump.save>` a new dump.

There are :doc:`converters` that are available to format the data that is
returned by :py:meth:`~pgdumplib.dump.Dump.read_data`. The converter
is passed in during construction of a new :py:class:`~pgdumplib.dump.Dump`,
and is also available as an argument to :py:func:`pgdumplib.load`.

The default converter, :py:class:`~pgdumplib.converters.DataConverter` will
return all fields as strings, only replacing ``NULL`` with
:py:const:`None`. The :py:class:`~pgdumplib.converters.SmartDataConverter`
will attempt to convert all columns to native Python data types.

When loading or creating a dump, the table and blob data are stored in
gzip compressed data files in a temporary directory that is automatically
cleaned up when the :py:class:`~pgdumplib.dump.Dump` instance is released.

"""
import contextlib
import datetime
import gzip
import io
import logging
import os
import pathlib
import re
import struct
import tempfile
import typing
import zlib

import toposort

from pgdumplib import constants, converters, exceptions, models, version

LOGGER = logging.getLogger(__name__)

ENCODING_PATTERN = re.compile(r"^.*=\s+'(.*)'")

VERSION_INFO = '{} (pgdumplib {})'

Converters = (type[converters.DataConverter] | type[converters.NoOpConverter]
              | type[converters.SmartDataConverter])


class TableData:
    """Used to encapsulate table data using temporary file and allowing
    for an API that allows for the appending of data one row at a time.

    Do not create this class directly, instead invoke
    :py:meth:`~pgdumplib.dump.Dump.table_data_writer`.

    """
    def __init__(self, dump_id: int, tempdir: str, encoding: str):
        self.dump_id = dump_id
        self._encoding = encoding
        self._path = pathlib.Path(tempdir) / f'{dump_id}.gz'
        self._handle = gzip.open(self._path, 'wb')

    def append(self, *args) -> None:
        """Append a row to the table data, passing columns in as args

        Column order must match the order specified when
        :py:meth:`~pgdumplib.dump.Dump.table_data_writer` was invoked.

        All columns will be coerced to a string with special attention
        paid to ``None``, converting it to the null marker (``\\N``) and
        :py:class:`datetime.datetime` objects, which will have the proper
        pg_dump timestamp format applied to them.

        """
        row = '\t'.join([self._convert(c) for c in args])
        self._handle.write(f'{row}\n'.encode(self._encoding))

    def finish(self) -> None:
        """Invoked prior to saving a dump to close the temporary data
        handle and switch the class into read-only mode.

        For use by :py:class:`pgdumplib.dump.Dump` only.

        """
        if not self._handle.closed:
            self._handle.close()
        self._handle = gzip.open(self._path, 'rb')

    def read(self) -> bytes:
        """Read the data from disk for writing to the dump

        For use by :py:class:`pgdumplib.dump.Dump` only.

        """
        self._handle.seek(0)
        return self._handle.read()

    @property
    def size(self) -> int:
        """Return the current size of the data on disk"""
        self._handle.seek(0, io.SEEK_END)  # Seek to end to figure out size
        size = self._handle.tell()
        self._handle.seek(0)
        return size

    @staticmethod
    def _convert(column: typing.Any) -> str:
        """Convert the column to a string

        :param column: The column to convert

        """
        if isinstance(column, datetime.datetime):
            return column.strftime(constants.PGDUMP_STRFTIME_FMT)
        elif column is None:
            return '\\N'
        return str(column)


class Dump:
    """Create a new instance of the :py:class:`~pgdumplib.dump.Dump` class

    Once created, the instance of :py:class:`~pgdumplib.dump.Dump` can
    be used to read existing dumps or to create new ones.

    :param str dbname: The database name for the dump (Default: ``pgdumplib``)
    :param str encoding: The data encoding (Default: ``UTF8``)
    :param converter: The data converter class to use
        (Default: :py:class:`pgdumplib.converters.DataConverter`)

    """
    def __init__(self,
                 dbname: str = 'pgdumplib',
                 encoding: str = 'UTF8',
                 converter: Converters | None = None,
                 appear_as: str = '12.0'):
        self.compression_algorithm = constants.COMPRESSION_NONE
        self.dbname = dbname
        self.dump_version = VERSION_INFO.format(appear_as, version)
        self.encoding = encoding
        self.entries = [
            models.Entry(dump_id=1,
                         tag=constants.ENCODING,
                         desc=constants.ENCODING,
                         defn=f"SET client_encoding = '{self.encoding}';\n"),
            models.Entry(dump_id=2,
                         tag='STDSTRINGS',
                         desc='STDSTRINGS',
                         defn="SET standard_conforming_strings = 'on';\n"),
            models.Entry(dump_id=3,
                         tag='SEARCHPATH',
                         desc='SEARCHPATH',
                         defn='SELECT pg_catalog.set_config('
                         "'search_path', '', false);\n")
        ]
        self.server_version = self.dump_version
        self.timestamp = datetime.datetime.now(tz=datetime.UTC)

        converter = converter or converters.DataConverter
        self._converter: converters.DataConverter = converter()
        self._format: str = 'Custom'
        self._handle: typing.BinaryIO | None = None
        self._intsize: int = 4
        self._offsize: int = 8
        self._temp_dir = tempfile.TemporaryDirectory()
        k_version = self._get_k_version(
            tuple(int(v) for v in appear_as.split('.')))
        self._vmaj: int = k_version[0]
        self._vmin: int = k_version[1]
        self._vrev: int = k_version[2]
        self._writers: dict[int, TableData] = {}

    def __repr__(self) -> str:
        return f'<Dump format={self._format!r} ' \
               f'timestamp={self.timestamp.isoformat()!r} ' \
               f'entry_count={len(self.entries)!r}>'

    def add_entry(self,
                  desc: str,
                  namespace: str | None = None,
                  tag: str | None = None,
                  owner: str | None = None,
                  defn: str | None = None,
                  drop_stmt: str | None = None,
                  copy_stmt: str | None = None,
                  dependencies: list[int] | None = None,
                  tablespace: str | None = None,
                  tableam: str | None = None,
                  dump_id: int | None = None) -> models.Entry:
        """Add an entry to the dump

        The ``namespace`` and ``tag`` are required.

        A :py:exc:`ValueError` will be raised if `desc` is not value that
        is known in :py:module:`pgdumplib.constants`.

        The section is

        When adding data, use :py:meth:`~Dump.table_data_writer` instead of
        invoking :py:meth:`~Dump.add_entry` directly.

        If ``dependencies`` are specified, they will be validated and if a
        ``dump_id`` is specified and no entry is found with that ``dump_id``,
        a :py:exc:`ValueError` will be raised.

        Other omitted values will be set to the default values will be set to
        the defaults specified in the :py:class:`pgdumplib.dump.Entry`
        class.

        The ``dump_id`` will be auto-calculated based upon the existing entries
        if it is not specified.

        .. note:: The creation of ad-hoc blobs is not supported.

        :param str desc: The entry description
        :param str namespace: The namespace of the entry
        :param str tag: The name/table/relation/etc of the entry
        :param str owner: The owner of the object in Postgres
        :param str defn: The DDL definition for the entry
        :param drop_stmt: A drop statement used to drop the entry before
        :param copy_stmt: A copy statement used when there is a corresponding
            data section.
        :param list dependencies: A list of dump_ids of objects that the entry
            is dependent upon.
        :param str tablespace: The tablespace to use
        :param str tableam: The table access method
        :param int dump_id: The dump id, will be auto-calculated if left empty
        :raises: :py:exc:`ValueError`
        :rtype: pgdumplib.dump.Entry

        """
        if desc not in constants.SECTION_MAPPING:
            raise ValueError(f'Invalid desc: {desc}')

        if dump_id is not None and dump_id < 1:
            raise ValueError('dump_id must be greater than 1')

        dump_ids = [e.dump_id for e in self.entries]

        if dump_id and dump_id in dump_ids:
            raise ValueError('dump_id {!r} is already assigned', dump_id)

        for dependency in dependencies or []:
            if dependency not in dump_ids:
                raise ValueError(
                    f'Dependency dump_id {dependency!r} not found')
        self.entries.append(
            models.Entry(dump_id or self._next_dump_id(), False, '', '', tag
                         or '', desc, defn or '', drop_stmt or '', copy_stmt
                         or '', namespace or '', tablespace or '', tableam
                         or '', owner or '', False, dependencies or []))
        return self.entries[-1]

    def blobs(self) -> typing.Generator[tuple[int, bytes], None, None]:
        """Iterator that returns each blob in the dump

        :rtype: tuple(int, bytes)

        """
        def read_oid(fd: typing.BinaryIO) -> int | None:
            """Small helper function to deduplicate code"""
            try:
                return struct.unpack('I', fd.read(4))[0]
            except struct.error:
                return None

        for entry in self._data_entries:
            if entry.desc == constants.BLOBS:
                with self._tempfile(entry.dump_id, 'rb') as handle:
                    oid: int | None = read_oid(handle)
                    while oid:
                        length: int = struct.unpack('I', handle.read(4))[0]
                        yield oid, handle.read(length)
                        oid = read_oid(handle)

    def get_entry(self, dump_id: int) -> models.Entry | None:
        """Return the entry for the given `dump_id`

        :param int dump_id: The dump ID of the entry to return.

        """
        for entry in self.entries:
            if entry.dump_id == dump_id:
                return entry
        return None

    def load(self, path: os.PathLike) -> typing.Self:
        """Load the Dumpfile, including extracting all data into a temporary
        directory

        :param os.PathLike path: The path of the dump to load
        :raises: :py:exc:`RuntimeError`
        :raises: :py:exc:`ValueError`

        """
        if not pathlib.Path(path).exists():
            raise ValueError(f'Path {path!r} does not exist')

        LOGGER.debug('Loading dump file from %s', path)

        self.entries = []  # Wipe out pre-existing entries
        self._handle = open(path, 'rb')
        self._read_header()
        if not constants.MIN_VER <= self.version <= constants.MAX_VER:
            raise ValueError(
                'Unsupported backup version: {}.{}.{}'.format(*self.version))

        if self.version >= (1, 15, 0):
            self.compression_algorithm = constants.COMPRESSION_ALGORITHMS[self._read_byte()]

            if self.compression_algorithm not in constants.SUPPORTED_COMPRESSION_ALGORITHMS:
                raise ValueError(
                    'Unsupported compression algorithm: {}'.format(*self.compression_algorithm))
        else:
            self.compression_algorithm = (
                constants.COMPRESSION_GZIP if self._read_int() != 0 else constants.COMPRESSION_NONE
            )

        self.timestamp = self._read_timestamp()
        self.dbname = self._read_bytes().decode(self.encoding)
        self.server_version = self._read_bytes().decode(self.encoding)
        self.dump_version = self._read_bytes().decode(self.encoding)

        self._read_entries()
        self._set_encoding()

        # Cache table data and blobs
        last_pos = self._handle.tell()

        for entry in self._data_entries:
            if entry.data_state == constants.K_OFFSET_NO_DATA:
                continue

            elif entry.data_state == constants.K_OFFSET_POS_SET:
                self._handle.seek(entry.offset, io.SEEK_SET)
                block_type, dump_id = self._read_block_header()
                if not dump_id or dump_id != entry.dump_id:
                    raise RuntimeError(f'Dump IDs do not match ({dump_id} != {entry.dump_id})')
                self._cache_block_data(block_type, dump_id)

            elif entry.data_state == constants.K_OFFSET_POS_NOT_SET:
                self._handle.seek(last_pos)

                while True:
                    pos = self._handle.tell()
                    try:
                        block_type, dump_id = self._read_block_header()
                    except EOFError:
                        return self

                    if entry.dump_id == dump_id:
                        break

                    # Cache position for any data blocks we find
                    data_entry = next((e for e in self._data_entries if e.dump_id == dump_id), None)

                    if data_entry and data_entry.data_state == constants.K_OFFSET_POS_NOT_SET:
                        data_entry.offset = pos
                        data_entry.data_state = constants.K_OFFSET_POS_SET

                    # Skip this block
                    if block_type == constants.BLK_DATA:
                        self._read_data()
                    elif block_type == constants.BLK_BLOBS:
                        self._read_blobs()
                    else:
                        raise RuntimeError(f'Unknown block type: {block_type}')

                self._cache_block_data(block_type, dump_id)

                # Read the end marker
                end_marker = self._read_int()
                if end_marker != 0:
                    raise RuntimeError(f'Unexpected end marker: {end_marker}')

                cur_pos = self._handle.tell()
                if cur_pos > last_pos:
                    last_pos = cur_pos

        return self

    def _cache_block_data(self, block_type, dump_id):
        if block_type == constants.BLK_DATA:
            self._cache_table_data(dump_id)
        elif block_type == constants.BLK_BLOBS:
            self._cache_blobs(dump_id)
        else:
            raise RuntimeError(f'Unexpected block type {block_type}')

    def lookup_entry(self, desc: str, namespace: str, tag: str) \
            -> models.Entry | None:
        """Return the entry for the given namespace and tag

        :param str desc: The desc / object type of the entry
        :param str namespace: The namespace of the entry
        :param str tag: The tag/relation/table name
        :param str section: The dump section the entry is for
        :raises: :py:exc:`ValueError`
        :rtype: pgdumplib.dump.Entry or None

        """
        if desc not in constants.SECTION_MAPPING:
            raise ValueError(f'Invalid desc: {desc}')
        for entry in [e for e in self.entries if e.desc == desc]:
            if entry.namespace == namespace and entry.tag == tag:
                return entry
        return None

    def save(self, path: os.PathLike) -> None:
        """Save the Dump file to the specified path

        :param os.PathLike path: The path to save the dump to

        """
        if getattr(self, '_handle', None) and not self._handle.closed:
            self._handle.close()
        self.compression_algorithm = constants.COMPRESSION_NONE
        self._handle = open(path, 'wb')
        self._save()
        self._handle.close()

    def table_data(self, namespace: str, table: str) \
            -> typing.Generator[str | tuple[typing.Any, ...], None, None]:
        """Iterator that returns data for the given namespace and table

        :param str namespace: The namespace/schema for the table
        :param str table: The table name
        :raises: :py:exc:`pgdumplib.exceptions.EntityNotFoundError`

        """
        for entry in self._data_entries:
            if entry.namespace == namespace and entry.tag == table:
                for row in self._read_table_data(entry.dump_id):
                    yield self._converter.convert(row)
                return
        raise exceptions.EntityNotFoundError(namespace=namespace, table=table)

    @contextlib.contextmanager
    def table_data_writer(self,
                          entry: models.Entry,
                          columns: typing.Sequence) \
            -> typing.Generator[TableData, None, None]:
        """A context manager that is used to return a
        :py:class:`~pgdumplib.dump.TableData` instance, which can be used
        to add table data to the dump.

        When invoked for a given entry containing the table definition,

        :param Entry entry: The entry for the table to add data for
        :param columns: The ordered list of table columns
        :type columns: list or tuple
        :rtype: TableData

        """
        if entry.dump_id not in self._writers.keys():
            dump_id = self._next_dump_id()
            self.entries.append(
                models.Entry(dump_id=dump_id,
                             had_dumper=True,
                             tag=entry.tag,
                             desc=constants.TABLE_DATA,
                             copy_stmt='COPY {}.{} ({}) FROM stdin;'.format(
                                 entry.namespace, entry.tag,
                                 ', '.join(columns)),
                             namespace=entry.namespace,
                             owner=entry.owner,
                             dependencies=[entry.dump_id],
                             data_state=constants.K_OFFSET_POS_NOT_SET))
            self._writers[entry.dump_id] = TableData(dump_id,
                                                     self._temp_dir.name,
                                                     self.encoding)
        yield self._writers[entry.dump_id]
        return None

    @property
    def version(self) -> tuple[int, int, int]:
        """Return the version as a tuple to make version comparisons easier.

        :rtype: tuple

        """
        return self._vmaj, self._vmin, self._vrev

    def _cache_blobs(self, dump_id: int) -> None:
        """Create a temp cache file for blob data

        :param int dump_id: The dump ID for the filename

        """
        count = 0
        with self._tempfile(dump_id, 'wb') as handle:
            for oid, blob in self._read_blobs():
                handle.write(struct.pack('I', oid))
                handle.write(struct.pack('I', len(blob)))
                handle.write(blob)
                count += 1

    def _cache_table_data(self, dump_id: int) -> None:
        """Create a temp cache file for the table data

        :param int dump_id: The dump ID for the filename

        """
        with self._tempfile(dump_id, 'wb') as handle:
            handle.write(self._read_data())

    @property
    def _data_entries(self) -> list[models.Entry]:
        """Return the list of entries that are in the data section

        :rtype: list

        """
        return [e for e in self.entries if e.section == constants.SECTION_DATA]

    @staticmethod
    def _get_k_version(appear_as: tuple[int, int]) \
            -> tuple[int, int, int]:
        for (min_ver, max_ver), value in constants.K_VERSION_MAP.items():
            if min_ver <= appear_as <= max_ver:
                return value
        raise RuntimeError(f'Unsupported PostgreSQL version: {appear_as}')

    def _next_dump_id(self) -> int:
        """Get the next ``dump_id`` that is available for adding an entry

        :rtype: int

        """
        return max(e.dump_id for e in self.entries) + 1

    def _read_blobs(self) -> typing.Generator[tuple[int, bytes], None, None]:
        """Read blobs, returning a tuple of the blob ID and the blob data

        :rtype: (int, bytes)
        :raises: :exc:`RuntimeError`

        """
        oid = self._read_int()
        while oid is not None and oid > 0:
            data = self._read_data()
            yield oid, data
            oid = self._read_int()
            if oid == 0:
                oid = self._read_int()

    def _read_block_header(self) -> tuple[bytes, int | None]:
        """Read the block header in

        :rtype: bytes, int

        """
        return self._handle.read(1), self._read_int()

    def _read_byte(self) -> int | None:
        """Read in an individual byte

        :rtype: int

        """
        try:
            return struct.unpack('B', self._handle.read(1))[0]
        except struct.error:
            return None

    def _read_bytes(self) -> bytes:
        """Read in a byte stream

        :rtype: bytes

        """
        length = self._read_int()
        if length and length > 0:
            value = self._handle.read(length)
            return value
        return b''

    def _read_data(self) -> bytes:
        """Read a data block, returning the bytes.

        :rtype: bytes

        """
        if self.compression_algorithm != constants.COMPRESSION_NONE:
            return self._read_data_compressed()
        return self._read_data_uncompressed()

    def _read_data_compressed(self) -> bytes:
        """Read a compressed data block

        :rtype: bytes

        """
        buffer = io.BytesIO()
        chunk = b''
        decompress = zlib.decompressobj()
        while True:
            chunk_size = self._read_int()
            if not chunk_size:  # pragma: nocover
                break
            chunk += self._handle.read(chunk_size)
            buffer.write(decompress.decompress(chunk))
            chunk = decompress.unconsumed_tail
            if chunk_size < constants.ZLIB_IN_SIZE:
                break
        return buffer.getvalue()

    def _read_data_uncompressed(self) -> bytes:
        """Read an uncompressed data block

        :rtype: bytes

        """
        buffer = io.BytesIO()
        while True:
            block_length = self._read_int()
            if not block_length or block_length <= 0:
                break
            buffer.write(self._handle.read(block_length))
        return buffer.getvalue()

    def _read_dependencies(self) -> list:
        """Read in the dependencies for an entry.

        :rtype: list

        """
        values = set({})
        while True:
            value = self._read_bytes()
            if not value:
                break
            values.add(int(value))
        return sorted(values)

    def _read_entries(self) -> None:
        """Read in all of the entries"""
        for _i in range(0, self._read_int() or 0):
            self._read_entry()

    def _read_entry(self) -> None:
        """Read in an individual entry and append it to the entries stack"""
        dump_id = self._read_int()
        had_dumper = bool(self._read_int())
        table_oid = self._read_bytes().decode(self.encoding)
        oid = self._read_bytes().decode(self.encoding)
        tag = self._read_bytes().decode(self.encoding)
        desc = self._read_bytes().decode(self.encoding)
        self._read_int()  # Section is mapped, no need to assign
        defn = self._read_bytes().decode(self.encoding)
        drop_stmt = self._read_bytes().decode(self.encoding)
        copy_stmt = self._read_bytes().decode(self.encoding)
        namespace = self._read_bytes().decode(self.encoding)
        tablespace = self._read_bytes().decode(self.encoding)
        if self.version >= (1, 14, 0):
            tableam = self._read_bytes().decode(self.encoding)
        else:
            tableam = ''
        owner = self._read_bytes().decode(self.encoding)
        with_oids = self._read_bytes() == b'true'
        dependencies = self._read_dependencies()
        data_state, offset = self._read_offset()
        self.entries.append(
            models.Entry(dump_id=dump_id,
                         had_dumper=had_dumper,
                         table_oid=table_oid,
                         oid=oid,
                         tag=tag,
                         desc=desc,
                         defn=defn,
                         drop_stmt=drop_stmt,
                         copy_stmt=copy_stmt,
                         namespace=namespace,
                         tablespace=tablespace,
                         tableam=tableam,
                         owner=owner,
                         with_oids=with_oids,
                         dependencies=dependencies,
                         data_state=data_state or 0,
                         offset=offset or 0))

    def _read_header(self) -> None:
        """Read in the dump header

        :raises: ValueError

        """
        if self._handle.read(5) != constants.MAGIC:
            raise ValueError('Invalid archive header')
        self._vmaj = struct.unpack('B', self._handle.read(1))[0]
        self._vmin = struct.unpack('B', self._handle.read(1))[0]
        self._vrev = struct.unpack('B', self._handle.read(1))[0]
        self._intsize = struct.unpack('B', self._handle.read(1))[0]
        self._offsize = struct.unpack('B', self._handle.read(1))[0]
        self._format = constants.FORMATS[struct.unpack(
            'B', self._handle.read(1))[0]]
        LOGGER.debug('Archive version %i.%i.%i', self._vmaj, self._vmin,
                     self._vrev)

    def _read_int(self) -> int | None:
        """Read in a signed integer

        :rtype: int or None

        """
        sign = self._read_byte()
        if sign is None:
            return None
        bs, bv, value = 0, 0, 0
        for _offset in range(0, self._intsize):
            bv = (self._read_byte() or 0) & 0xFF
            if bv != 0:
                value += (bv << bs)
            bs += 8
        return -value if sign else value

    def _read_offset(self) -> tuple[int, int]:
        """Read in the value for the length of the data stored in the file

        :rtype: int, int

        """
        data_state = self._read_byte() or 0
        value = 0
        for offset in range(0, self._offsize):
            bv = self._read_byte() or 0
            value |= bv << (offset * 8)
        return data_state, value

    def _read_table_data(self, dump_id: int) \
            -> typing.Generator[str, None, None]:
        """Iterate through the data returning on row at a time

        :rtype: str

        """
        try:
            with self._tempfile(dump_id, 'rb') as handle:
                for line in handle:
                    out = (line or b'').decode(self.encoding).strip()
                    if out.startswith('\\.') or not out:
                        break
                    yield out
        except exceptions.NoDataError:
            pass

    def _read_timestamp(self) -> datetime.datetime:
        """Read in the timestamp from handle.

        :rtype: datetime.datetime

        """
        second, minute, hour, day, month, year = (self._read_int(),
                                                  self._read_int(),
                                                  self._read_int(),
                                                  self._read_int(),
                                                  (self._read_int() or 0) + 1,
                                                  (self._read_int() or 0) +
                                                  1900)
        self._read_int()  # DST flag
        return datetime.datetime(year,
                                 month,
                                 day,
                                 hour,
                                 minute,
                                 second,
                                 0,
                                 tzinfo=datetime.UTC)

    def _save(self) -> None:
        """Save the dump file to disk"""
        self._write_toc()
        self._write_entries()
        if self._write_data():
            self._write_toc()  # Overwrite ToC and entries
            self._write_entries()

    def _set_encoding(self) -> None:
        """If the encoding is found in the dump entries, set the encoding
        to `self.encoding`.

        """
        for entry in self.entries:
            if entry.desc == constants.ENCODING:
                match = ENCODING_PATTERN.match(entry.defn)
                if match:
                    self.encoding = match.group(1)
                    return

    @contextlib.contextmanager
    def _tempfile(self, dump_id: int, mode: str) \
            -> typing.Generator[typing.IO[bytes], None, None]:
        """Open the temp file for the specified dump_id in the specified mode

        :param int dump_id: The dump_id for the temp file
        :param str mode: The mode (rb, wb)

        """
        path = pathlib.Path(self._temp_dir.name) / f'{dump_id}.gz'
        if not path.exists() and mode.startswith('r'):
            raise exceptions.NoDataError()
        with gzip.open(path, mode) as handle:
            try:
                yield handle
            except Exception:
                raise

    def _write_blobs(self, dump_id: int) -> int:
        """Write the blobs for the entry.

        :param int dump_id: The entry dump ID for the blobs
        :rtype: int

        """
        with self._tempfile(dump_id, 'rb') as handle:
            self._handle.write(constants.BLK_BLOBS)
            self._write_int(dump_id)
            while True:
                try:
                    oid = struct.unpack('I', handle.read(4))[0]
                except struct.error:
                    break
                length = struct.unpack('I', handle.read(4))[0]
                self._write_int(oid)
                self._write_int(length)
                self._handle.write(handle.read(length))
                self._write_int(0)
            self._write_int(0)
        return length

    def _write_byte(self, value: int) -> None:
        """Write a byte to the handle

        :param int value: The byte value

        """
        self._handle.write(struct.pack('B', value))

    def _write_data(self) -> set:
        """Write the data blocks, returning a set of IDs that were written"""
        saved = set({})
        for offset, entry in enumerate(self.entries):
            if entry.section != constants.SECTION_DATA:
                continue
            self.entries[offset].offset = self._handle.tell()
            size = 0
            if entry.desc == constants.TABLE_DATA:
                size = self._write_table_data(entry.dump_id)
                saved.add(entry.dump_id)
            elif entry.desc == constants.BLOBS:
                size = self._write_blobs(entry.dump_id)
                saved.add(entry.dump_id)
            if size:
                self.entries[offset].data_state = constants.K_OFFSET_POS_SET
        return saved

    def _write_entries(self):
        self._write_int(len(self.entries))
        saved = set({})

        # Always add these entries first
        for entry in self.entries[0:3]:
            self._write_entry(entry)
            saved.add(entry.dump_id)

        saved = self._write_section(constants.SECTION_PRE_DATA, [
            constants.GROUP, constants.ROLE, constants.USER, constants.SCHEMA,
            constants.EXTENSION, constants.AGGREGATE, constants.OPERATOR,
            constants.OPERATOR_CLASS, constants.CAST, constants.COLLATION,
            constants.CONVERSION, constants.PROCEDURAL_LANGUAGE,
            constants.FOREIGN_DATA_WRAPPER, constants.FOREIGN_SERVER,
            constants.SERVER, constants.DOMAIN, constants.TYPE,
            constants.SHELL_TYPE
        ], saved)

        saved = self._write_section(constants.SECTION_DATA, [], saved)

        saved = self._write_section(constants.SECTION_POST_DATA, [
            constants.CHECK_CONSTRAINT, constants.CONSTRAINT, constants.INDEX
        ], saved)

        saved = self._write_section(constants.SECTION_NONE, [], saved)
        LOGGER.debug('Wrote %i of %i entries', len(saved), len(self.entries))

    def _write_entry(self, entry: models.Entry) -> None:
        """Write the entry

        :param pgdumplib.dump.Entry entry: The entry to write

        """
        LOGGER.debug('Writing %r', entry)
        self._write_int(entry.dump_id)
        self._write_int(int(entry.had_dumper))
        self._write_str(entry.table_oid or '0')
        self._write_str(entry.oid or '0')
        self._write_str(entry.tag)
        self._write_str(entry.desc)
        self._write_int(constants.SECTIONS.index(entry.section) + 1)
        self._write_str(entry.defn)
        self._write_str(entry.drop_stmt)
        self._write_str(entry.copy_stmt)
        self._write_str(entry.namespace)
        self._write_str(entry.tablespace)
        if self.version >= (1, 14, 0):
            LOGGER.debug('Adding tableam')
            self._write_str(entry.tableam)
        self._write_str(entry.owner)
        self._write_str('true' if entry.with_oids else 'false')
        for dependency in entry.dependencies or []:
            self._write_str(str(dependency))
        self._write_int(-1)
        self._write_offset(entry.offset, entry.data_state)

    def _write_header(self) -> None:
        """Write the file header"""
        LOGGER.debug('Writing archive version %i.%i.%i', self._vmaj,
                     self._vmin, self._vrev)
        self._handle.write(constants.MAGIC)
        self._write_byte(self._vmaj)
        self._write_byte(self._vmin)
        self._write_byte(self._vrev)
        self._write_byte(self._intsize)
        self._write_byte(self._offsize)
        self._write_byte(constants.FORMATS.index(self._format))

    def _write_int(self, value: int) -> None:
        """Write an integer value

        :param int value:

        """
        self._write_byte(1 if value < 0 else 0)
        if value < 0:
            value = -value
        for _offset in range(0, self._intsize):
            self._write_byte(value & 0xFF)
            value >>= 8

    def _write_offset(self, value: int, data_state: int) -> None:
        """Write the offset value.

        :param int value: The value to write
        :param int data_state: The data state flag

        """
        self._write_byte(data_state)
        for _offset in range(0, self._offsize):
            self._write_byte(value & 0xFF)
            value >>= 8

    def _write_section(self, section: str, obj_types: list, saved: set) -> set:
        for obj_type in obj_types:
            for entry in [e for e in self.entries if e.desc == obj_type]:
                self._write_entry(entry)
                saved.add(entry.dump_id)
        for dump_id in toposort.toposort_flatten(
            {
                e.dump_id: set(e.dependencies)
                for e in self.entries if e.section == section
            }, True):
            if dump_id not in saved:
                self._write_entry(self.get_entry(dump_id))
                saved.add(dump_id)
        return saved

    def _write_str(self, value: str) -> None:
        """Write a string

        :param str value: The string to write

        """
        out = value.encode(self.encoding) if value else b''
        self._write_int(len(out))
        if out:
            LOGGER.debug('Writing %r', out)
            self._handle.write(out)

    def _write_table_data(self, dump_id: int) -> int:
        """Write the blobs for the entry, returning the # of bytes written

        :param int dump_id: The entry dump ID for the blobs
        :rtype: int

        """
        self._handle.write(constants.BLK_DATA)
        self._write_int(dump_id)

        writer = [w for w in self._writers.values() if w.dump_id == dump_id]
        if writer:  # Data was added ad-hoc
            writer[0].finish()
            self._write_int(writer[0].size)
            self._handle.write(writer[0].read())
            self._write_int(0)  # End of data indicator
            return writer[0].size

        # Data was cached on load
        with self._tempfile(dump_id, 'rb') as handle:
            handle.seek(0, io.SEEK_END)  # Seek to end to figure out size
            size = handle.tell()
            self._write_int(size)
            if size:
                handle.seek(0)  # Rewind to read data
                self._handle.write(handle.read())
        self._write_int(0)  # End of data indicator
        return size

    def _write_timestamp(self, value: datetime.datetime) -> None:
        """Write a datetime.datetime value

        :param datetime.datetime value: The value to write

        """
        self._write_int(value.second)
        self._write_int(value.minute)
        self._write_int(value.hour)
        self._write_int(value.day)
        self._write_int(value.month - 1)
        self._write_int(value.year - 1900)
        self._write_int(1 if value.dst() else 0)

    def _write_toc(self) -> None:
        """Write the ToC for the file"""
        self._handle.seek(0)
        self._write_header()

        if self.version >= (1, 15, 0):
            self._write_byte(constants.COMPRESSION_ALGORITHMS.index(self.compression_algorithm))
        else:
            self._write_int(int(self.compression_algorithm != constants.COMPRESSION_NONE))

        self._write_timestamp(self.timestamp)
        self._write_str(self.dbname)
        self._write_str(self.server_version)
        self._write_str(self.dump_version)
