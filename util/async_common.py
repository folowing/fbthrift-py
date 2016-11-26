"""
Common base for asyncio and Trollius (the Python 2 asyncio backport).
Ideally this would be all that's necessary but we can't use the async/await
syntax on Python 2 so we had to abstract coroutines away.

Look for them in TAsyncioServer and TTrolliusServer respectively.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import six
import logging
import struct
import warnings

import thrift
from collections import defaultdict
from io import BytesIO
from thrift.protocol.THeaderProtocol import THeaderProtocolFactory
from thrift.server.TServer import TConnectionContext
from thrift.transport.TTransport import (
    TTransportBase,
    TTransportException,
)
from thrift.transport.THeaderTransport import THeaderTransport
from thrift.Thrift import (
    TApplicationException,
    TMessageType,
)

if six.PY3 and not thrift.trollius:
    import asyncio
else:
    import trollius as asyncio

# We support the deprecated FRAMED transport for old fb303
# clients that were otherwise failing miserably.
THEADER_CLIENT_TYPES = {
    THeaderTransport.HEADERS_CLIENT_TYPE,
    THeaderTransport.FRAMED_DEPRECATED,
}
_default_thpfactory = THeaderProtocolFactory(client_types=THEADER_CLIENT_TYPES)
THeaderProtocol = _default_thpfactory.getProtocol

logger = logging.getLogger(__name__)


if six.PY2:
    class PermissionError(IOError):
        pass

    class ResourceWarning(RuntimeWarning):
        pass


class TReadOnlyBuffer(TTransportBase):
    """Leaner version of TMemoryBuffer that is resettable."""

    def __init__(self, value=b""):
        self._open = True
        self._value = value
        self.reset()

    def isOpen(self):
        return self._open

    def close(self):
        self._io.close()
        self._open = False

    def read(self, sz):
        return self._io.read(sz)

    def write(self, buf):
        raise PermissionError("This is a read-only buffer")

    def reset(self):
        self._io = BytesIO(self._value)


class TWriteOnlyBuffer(TTransportBase):
    """Leaner version of TMemoryBuffer that is resettable."""

    def __init__(self):
        self._open = True
        self.reset()

    def isOpen(self):
        return self._open

    def close(self):
        self._io.close()
        self._open = False

    def read(self, sz):
        raise EOFError("This is a write-only buffer")

    def write(self, buf):
        self._io.write(buf)

    def getvalue(self):
        return self._io.getvalue()

    def reset(self):
        self._io = BytesIO()


class TReadWriteBuffer(TTransportBase):
    """TMemoryBuffer equivalent with separate buffers to read and write."""

    def __init__(self, value=b""):
        self._read_io = TReadOnlyBuffer(value=value)
        self._write_io = TWriteOnlyBuffer()
        self.read = self._read_io.read
        self.write = self._write_io.write
        self.getvalue = self._write_io.getvalue
        self.reset()

    def isOpen(self):
        return self._read_io._open and self._write_io._open

    def close(self):
        self._read_io.close()
        self._write_io.close()

    def reset(self):
        self._read_io.reset()
        self._write_io.reset()

    # Note: read()/write()/getvalue() methods are bound in __init__().


class WrappedTransport(TWriteOnlyBuffer):
    """Wraps an asyncio.Transport in a Thrift Transport interface."""

    MAX_QUEUE_SIZE = 1024

    def __init__(self, trans, proto, loop):
        super(WrappedTransport, self).__init__()
        self._trans = trans
        self._proto = proto
        self._loop = loop
        self._queue = asyncio.Queue(
            maxsize=self.MAX_QUEUE_SIZE,
            loop=self._loop,
        )
        self._consumer = self._loop.create_task(self._send())
        self._producers = []

    @asyncio.coroutine
    def _send(self):
        raise NotImplementedError

    def send_message(self, msg):
        self._producers.append(
            self._loop.create_task(self._queue.put(msg)),
        )

    def flush(self):
        msg = self.getvalue()
        tmi = TReadOnlyBuffer(msg)
        iprot = THeaderProtocol(tmi)
        fname, mtype, seqid = iprot.readMessageBegin()
        fname = fname.decode()
        self._proto.schedule_timeout(fname, seqid)
        self.send_message(msg)
        self.reset()

    def _clean_producers(self):
        self._producers = [
            p for p in self._producers if not p.done() and not p.cancelled()
        ]

    def close(self):
        try:
            self._consumer.cancel()
            for producer in self._producers:
                if not producer.done() and not producer.cancelled():
                    producer.cancel()
            super(WrappedTransport, self).close()
        finally:
            self._trans.close()

    def __del__(self):
        if not self._consumer.done() or not self._consumer.cancelled():
            logger.debug(
                'WrappedTransport did not finish properly'
                ' as the consumer asyncio.Task is still pending.'
                ' Make sure to call .close() on this object.'
            )
        if self.isOpen():
            warnings.warn(
                'WrappedTransport is being garbage collected'
                ' while still open.'
                ' Make sure to call .close() on this object.',
                ResourceWarning
            )


class FramedProtocol(asyncio.Protocol):
    """Unpacks Thrift frames and reads them asynchronously."""

    MAX_LENGTH = THeaderTransport.MAX_FRAME_SIZE

    def __init__(self, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.recvd = b""

    @asyncio.coroutine
    def message_received(self, frame):
        raise NotImplementedError

    def data_received(self, data):
        """Implements asyncio.Protocol.data_received."""
        self.recvd = self.recvd + data
        while len(self.recvd) >= 4:
            length, = struct.unpack("!I", self.recvd[:4])
            if length > self.MAX_LENGTH:
                logger.error(
                    "Frame size %d too large for THeaderProtocol",
                    length,
                )
                self.transport.close()
                return
            elif length == 0:
                logger.error("Empty frame")
                self.transport.close()
                return

            if len(self.recvd) < length + 4:
                return

            frame = self.recvd[0:4 + length]
            self.recvd = self.recvd[4 + length:]
            self.loop.create_task(self.message_received(frame))

    def eof_received(self):
        """Implements asyncio.Protocol.eof_received."""
        return self.connection_lost(exc=None)

    # Don't forget to implement connection_made/connection_lost in your
    # subclass.  There's also pause_writing/resume_writing but it seems we're
    # fine without it.


class ThriftHeaderClientProtocolBase(FramedProtocol):
    """asyncio THeader protocol wrapper for client use.

    This is abstract, missing implementation of an async TTransport
    wrapper and the `message_received` coroutine function.
    """
    DEFAULT_TIMEOUT = 60.0
    THEADER_PROTOCOL_FACTORY = THeaderProtocolFactory
    _exception_serializer = None

    def __init__(
        self,
        client_class,
        loop=None,
        timeouts=None,
        client_type=None,
    ):
        super(ThriftHeaderClientProtocolBase, self).__init__(loop=loop)
        self.client_class = client_class
        if timeouts is None:
            timeouts = {}
        default_timeout = timeouts.get('') or self.DEFAULT_TIMEOUT
        self.timeouts = defaultdict(lambda: default_timeout)
        self.timeouts.update(timeouts)
        self.client_type = client_type

        self.client = None
        self.pending_tasks = {}
        self.transport = None  # TTransport wrapping an asyncio.Transport

    # message_received still left to be implemented in a subclass

    def wrapAsyncioTransport(self, asyncio_transport):
        raise NotImplementedError

    def connection_made(self, transport):
        """Implements asyncio.Protocol.connection_made."""
        assert self.transport is None, (
            "Thrift transport already instantiated here."
        )
        assert self.client is None, "Client already instantiated here."
        self.transport = self.wrapAsyncioTransport(transport)
        thrift_protocol = self.THEADER_PROTOCOL_FACTORY(
            client_type=self.client_type,
        ).getProtocol(self.transport)
        self.client = self.client_class(thrift_protocol, self.loop)

    def connection_lost(self, exc):
        """Implements asyncio.Protocol.connection_lost."""
        for fut in self.client._futures.values():
            te = TTransportException(
                type=TTransportException.END_OF_FILE,
                message="Connection closed")
            if not fut.done():
                fut.set_exception(te)

    def _handle_message_received(self, iprot, fname, mtype, seqid):
        method = getattr(self.client, "recv_" + fname.decode(), None)
        if method is None:
            logger.error("Method %r is not supported", fname)
            self.close()
            return

        try:
            method(iprot, mtype, seqid)
        except (
            asyncio.futures.InvalidStateError,
            asyncio.CancelledError,
        ) as e:
            logger.warning("Method %r cancelled: %s", fname, str(e))

    def update_pending_tasks(self, seqid, task):
        no_longer_pending = [
            _seqid for _seqid, _task in self.pending_tasks.items()
            if _task.done() or _task.cancelled()
        ]
        for _seqid in no_longer_pending:
            del self.pending_tasks[_seqid]
        assert seqid not in self.pending_tasks, (
            "seqid already pending for timeout"
        )
        self.pending_tasks[seqid] = task

    def schedule_timeout(self, fname, seqid):
        timeout = self.timeouts[fname]
        if not timeout:
            return

        exc = TApplicationException(
            TApplicationException.TIMEOUT, "Call to {} timed out".format(fname)
        )
        serialized_exc = self.serialize_texception(fname, seqid, exc)
        timeout_task = asyncio.Task(
            self.message_received(serialized_exc, delay=timeout),
            loop=self.loop,
        )
        self.update_pending_tasks(seqid, timeout_task)

    def close(self):
        for task in self.pending_tasks.values():
            if not task.done() and not task.cancelled():
                task.cancel()
        if not self.transport:
            return

        try:
            # Closing the wrapped sender transport will cascade closing
            # of the underlying tranports, too.
            self.transport.close()
        except Exception:
            pass

    @classmethod
    def serialize_texception(cls, fname, seqid, exception):
        """This saves us a bit of processing time for timeout handling by
        reusing the Thrift structs involved in exception serialization.

        NOTE: this is not thread-safe nor it is meant to be.
        """
        # the serializer is a singleton
        if cls._exception_serializer is None:
            buffer = TWriteOnlyBuffer()
            transport = THeaderTransport(buffer)
            cls._exception_serializer = THeaderProtocol(transport)
        else:
            transport = cls._exception_serializer.trans
            buffer = transport.getTransport()
            buffer.reset()

        serializer = cls._exception_serializer
        serializer.writeMessageBegin(fname, TMessageType.EXCEPTION, seqid)
        exception.write(serializer)
        serializer.writeMessageEnd()
        serializer.trans.flush()
        return buffer.getvalue()


class AsyncioRpcConnectionContext(TConnectionContext):

    def __init__(self, client_socket):
        self._client_socket = client_socket

    def getPeerName(self):
        return self._client_socket.getpeername()
