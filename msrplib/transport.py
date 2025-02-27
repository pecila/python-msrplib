# Copyright (C) 2008-2012 AG Projects. See LICENSE for details

import random

from application import log
from eventlib.twistedutil.protocol import GreenTransportBase
from twisted.internet.error import ConnectionDone

from msrplib import protocol, MSRPError
from msrplib.trafficlog import Logger


log = log.get_logger('msrplib')


class ChunkParseError(MSRPError):
    """Failed to parse incoming chunk"""


class MSRPTransactionError(MSRPError):
    def __init__(self, comment=None, code=None):
        if comment is not None:
            self.comment = comment
        if code is not None:
            self.code = code
        if not hasattr(self, 'code'):
            raise TypeError("must provide 'code'")

    def __str__(self):
        if hasattr(self, 'comment'):
            return '%s %s' % (self.code, self.comment)
        else:
            return str(self.code)


class MSRPBadRequest(MSRPTransactionError):
    code = 400
    comment = 'Bad Request'

    def __str__(self):
        return 'Remote party sent bogus data'


class MSRPNoSuchSessionError(MSRPTransactionError):
    code = 481
    comment = 'No such session'


data_start, data_end, data_write, data_final_write = list(range(4))


def make_report(chunk, code, comment):
    if chunk.success_report == 'yes' or (chunk.failure_report in ('yes', 'partial') and code != 200):
        report = protocol.MSRPData(transaction_id='%x' % random.getrandbits(64), method='REPORT')
        report.add_header(protocol.ToPathHeader(chunk.from_path))
        report.add_header(protocol.FromPathHeader([chunk.to_path[0]]))
        report.add_header(protocol.StatusHeader(protocol.Status(code, comment)))
        report.add_header(protocol.MessageIDHeader(chunk.message_id))
        if chunk.byte_range is None:
            start = 1
            total = chunk.size
        else:
            start, end, total = chunk.byte_range
        report.add_header(protocol.ByteRangeHeader(protocol.ByteRange(start, start+chunk.size-1, total)))
        return report
    else:
        return None


def make_response(chunk, code, comment):
    """Construct a response to a request as described in RFC4975 Section 7.2.
    If the response is not needed, return None.
    If a required header missing, raise ChunkParseError.
    """
    if chunk.failure_report == 'no':
        return
    if chunk.failure_report == 'partial' and code == 200:
        return
    if chunk.to_path is None:
        raise ChunkParseError('missing To-Path header: %r' % chunk)
    if chunk.from_path is None:
        raise ChunkParseError('missing From-Path header: %r' % chunk)
    if chunk.method == 'SEND':
        to_path = [chunk.from_path[0]]
    else:
        to_path = chunk.from_path
    from_path = [chunk.to_path[0]]
    response = protocol.MSRPData(chunk.transaction_id, code=code, comment=comment)
    response.add_header(protocol.ToPathHeader(to_path))
    response.add_header(protocol.FromPathHeader(from_path))
    return response


class MSRPTransport(GreenTransportBase):
    protocol_class = protocol.MSRPProtocol

    def __init__(self, local_uri, logger, use_sessmatch=False):
        GreenTransportBase.__init__(self)
        if local_uri is not None and not isinstance(local_uri, protocol.URI):
            raise TypeError('Not MSRP URI instance: %r' % (local_uri, ))
        # The following members define To-Path and From-Path headers as following:
        # * Outgoing request:
        #   From-Path: local_uri
        #   To-Path: local_path + remote_path + [remote_uri]
        # * Incoming request:
        #   From-Path: remote_path + remote_uri
        #   To-Path: remote_path + local_path + [local_uri] # XXX
        self.local_uri = local_uri
        if logger is None:
            logger = Logger()
        self.logger = logger
        self.local_path = []
        self.remote_uri = None
        self.remote_path = []
        self.use_sessmatch = use_sessmatch

    def next_host(self):
        if self.local_path:
            return self.local_path[0]
        return self.full_remote_path[0]

    def set_local_path(self, lst):
        self.local_path = lst

    @property
    def full_local_path(self):
        # suitable to put into INVITE
        return self.local_path + [self.local_uri]

    @property
    def full_remote_path(self):
        return self.remote_path + [self.remote_uri]

    def make_request(self, method):
        transaction_id = '%x' % random.getrandbits(64)
        chunk = protocol.MSRPData(transaction_id=transaction_id, method=method)
        chunk.add_header(protocol.ToPathHeader(self.local_path + self.remote_path + [self.remote_uri]))
        chunk.add_header(protocol.FromPathHeader([self.local_uri]))
        return chunk

    def make_send_request(self, message_id=None, data='', start=1, end=None, length=None):
        chunk = self.make_request('SEND')
        if end is None:
            end = start - 1 + len(data)
        if length is None:
            length = start - 1 + len(data)
        if end == length != '*':
            contflag = '$'
        else:
            contflag = '+'
        chunk.add_header(protocol.ByteRangeHeader(protocol.ByteRange(start, end if length <= 2048 else None, length)))
        if message_id is None:
            message_id = '%x' % random.getrandbits(64)
        chunk.add_header(protocol.MessageIDHeader(message_id))
        chunk.data = data
        chunk.contflag = contflag
        return chunk

    def _data_start(self, data):
        self._queue.send((data_start, data))

    def _data_end(self, continuation):
        self._queue.send((data_end, continuation))

    def _data_write(self, contents, final):
        if final:
            self._queue.send((data_final_write, contents))
        else:
            self._queue.send((data_write, contents))

    def write_chunk(self, chunk, wait=True):
        self.write(chunk.encode(), wait=wait)
        self.logger.sent_chunk(chunk, transport=self)

    def read_chunk(self, max_size=1024*1024*4):
        """Wait for a new chunk and return it.
        If there was an error, close the connection and raise ChunkParseError.

        In case of unintelligible input, lose the connection and return None.
        When the connection is closed, raise the reason of the closure (e.g. ConnectionDone).
        """

        assert max_size > 0

        func, msrpdata = self._wait()
        if func != data_start:
            self.logger.debug('Bad data: %r %r', func, msrpdata)
            self.loseConnection()
            raise ChunkParseError
        data = msrpdata.data
        func, param = self._wait()
        while func == data_write:
            data += param
            if len(data) > max_size:
                self.logger.debug('Chunk is too big (max_size=%d bytes)', max_size)
                self.loseConnection()
                raise ChunkParseError
            func, param = self._wait()
        if func == data_final_write:
            data += param
            func, param = self._wait()
        if func != data_end:
            self.logger.debug('Bad data: %r %s', func, repr(param)[:100])
            self.loseConnection()
            raise ChunkParseError
        if param not in "$+#":
            self.logger.debug('Bad data: %r %s', func, repr(param)[:100])
            self.loseConnection()
            raise ChunkParseError
        msrpdata.data = data
        msrpdata.contflag = param
        self.logger.received_chunk(msrpdata, transport=self)
        return msrpdata

    def _set_full_remote_path(self, full_remote_path):
        # as received in response to INVITE
        if not all(isinstance(x, protocol.URI) for x in full_remote_path):
            raise TypeError('Not all elements are MSRP URI: %r' % full_remote_path)
        self.remote_uri = full_remote_path[-1]
        self.remote_path = full_remote_path[:-1]

    def bind(self, full_remote_path):
        self._set_full_remote_path(full_remote_path)
        chunk = self.make_send_request()
        self.write_chunk(chunk)
        # With some ACM implementations both parties may think they are active,
        # so they will both send an empty SEND request. -Saul
        while True:
            chunk = self.read_chunk()
            if chunk.code is None:
                # This was not a response, it was a request
                if chunk.method == 'SEND' and not chunk.data:
                    self.write_response(chunk, 200, 'OK')
                else:
                    self.loseConnection(wait=False)
                    raise MSRPNoSuchSessionError('Chunk received while binding session: %s' % chunk)
            elif chunk.code != 200:
                self.loseConnection(wait=False)
                raise MSRPNoSuchSessionError('Cannot bind session: %s' % chunk)
            else:
                break

    def write_response(self, chunk, code, comment, wait=True):
        """Generate and write the response, lose the connection in case of error"""
        try:
            response = make_response(chunk, code, comment)
        except ChunkParseError as ex:
            log.error('Failed to generate a response: %s' % ex)
            self.loseConnection(wait=False)
            raise
        except Exception:
            log.exception('Failed to generate a response')
            self.loseConnection(wait=False)
            raise
        else:
            if response is not None:
                self.write_chunk(response, wait=wait)

    def accept_binding(self, full_remote_path):
        self._set_full_remote_path(full_remote_path)
        chunk = self.read_chunk()
        error = self.check_incoming_SEND_chunk(chunk)
        if error is None:
            code, comment = 200, 'OK'
        else:
            code, comment = error.code, error.comment
        self.write_response(chunk, code, comment)
        if 'Content-Type' in chunk.headers or chunk.size > 0:
            # deliver chunk to read_chunk
            data = chunk.data
            chunk.data = ''
            self._data_start(chunk)
            self._data_write(data, final=True)
            self._data_end(chunk.contflag)

    def check_incoming_SEND_chunk(self, chunk):
        """Check the 'To-Path' and 'From-Path' of the incoming SEND chunk.
        Return None is the paths are valid for this connection.
        If an error is detected and MSRPError is created and returned.
        """
        assert chunk.method == 'SEND', repr(chunk)
        if chunk.to_path is None:
            return MSRPBadRequest('To-Path header missing')
        if chunk.from_path is None:
            return MSRPBadRequest('From-Path header missing')
        to_path = list(chunk.to_path)
        from_path = list(chunk.from_path)
        expected_to = [self.local_uri]
        expected_from = self.local_path + self.remote_path + [self.remote_uri]
        # Match only session ID when use_sessmatch is set (http://tools.ietf.org/html/draft-ietf-simple-msrp-sessmatch-10)
        if self.use_sessmatch:
            if to_path[0].session_id != expected_to[0].session_id:
                log.error('To-Path: expected session_id %s, got %s' % (expected_to[0].session_id, to_path[0].session_id))
                return MSRPNoSuchSessionError('Invalid To-Path')
            if from_path[0].session_id != expected_from[0].session_id:
                log.error('From-Path: expected session_id %s, got %s' % (expected_from[0].session_id, from_path[0].session_id))
                return MSRPNoSuchSessionError('Invalid From-Path')
        else:
            if to_path != expected_to:
                log.error('To-Path: expected %r, got %r' % (expected_to, to_path))
                return MSRPNoSuchSessionError('Invalid To-Path')
            if from_path != expected_from:
                log.error('From-Path: expected %r, got %r' % (expected_from, from_path))
                return MSRPNoSuchSessionError('Invalid From-Path')

    def connection_lost(self, reason):
        message = 'Closed connection to {0.host}:{0.port}'.format(self.getPeer())
        if not isinstance(reason.value, ConnectionDone):
            message += ' ({})'.format(reason.getErrorMessage())
        self.logger.info(message)
        self._connectionLost(reason)
