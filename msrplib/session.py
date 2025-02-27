# Copyright (C) 2008-2012 AG Projects. See LICENSE for details
#

import traceback

from time import time
from twisted.internet.error import ConnectionClosed, ConnectionDone
from twisted.python.failure import Failure
from gnutls.errors import GNUTLSError
from eventlib import api, coros, proc
from eventlib.twistedutil.protocol import ValueQueue

from msrplib import protocol, MSRPError
from msrplib.transport import make_report, make_response, MSRPTransactionError


ConnectionClosedErrors = (ConnectionClosed, GNUTLSError)


class MSRPSessionError(MSRPError):
    pass


class MSRPBadContentType(MSRPTransactionError):
    code = 415
    comment = 'Unsupported media type'


class LocalResponse(MSRPTransactionError):
    def __repr__(self):
        return '<LocalResponse %s %s>' % (self.code, self.comment)


Response200OK = LocalResponse("OK", 200)
Response408Timeout = LocalResponse("Timed out while waiting for transaction response", 408)


def contains_mime_type(mimetypelist, mimetype):
    """Return True if mimetypelist contains mimetype.
    mimietypelist either contains the complete mime types, such as 'text/plain',
    or simple patterns, like 'text/*', or simply '*'.
    """
    mimetype = mimetype.lower().partition(';')[0]
    for pattern in mimetypelist:
        pattern = pattern.lower()
        if pattern == '*':
            return True
        if pattern == mimetype:
            return True
        if pattern.endswith('/*') and mimetype.startswith(pattern[:-1]):
            return True
    return False


class OutgoingChunk(object):
    __slots__ = ('chunk', 'response_callback')

    def __init__(self,  chunk, response_callback=None):
        self.chunk = chunk
        self.response_callback = response_callback


class MSRPSession(object):
    RESPONSE_TIMEOUT = 30
    SHUTDOWN_TIMEOUT = 1
    KEEPALIVE_INTERVAL = 60

    def __init__(self, msrptransport, accept_types=['*'], on_incoming_cb=None, automatic_reports=True):
        self.msrp = msrptransport
        self.accept_types = accept_types
        self.automatic_reports = automatic_reports
        if not callable(on_incoming_cb):
            raise TypeError('on_incoming_cb must be callable: %r' % on_incoming_cb)
        self._on_incoming_cb = on_incoming_cb
        self.expected_responses = {}
        self.outgoing = coros.queue()
        self.reader_job = proc.spawn(self._reader)
        self.writer_job = proc.spawn(self._writer)
        self.state = 'CONNECTED' # -> 'FLUSHING' -> 'CLOSING' -> 'DONE'
        # in FLUSHING writer sends only while there's something in the outgoing queue
        # then it exits and sets state to 'CLOSING' which makes reader only pay attention
        # to responses and success reports. (XXX it could now discard incoming data chunks
        # with direct write() since writer is dead)
        self.reader_job.link(self.writer_job)
        self.last_expected_response = 0
        self.keepalive_proc = proc.spawn(self._keepalive)

    def _get_logger(self):
        return self.msrp.logger

    def _set_logger(self, logger):
        self.msrp.logger = logger

    logger = property(_get_logger, _set_logger)

    def set_state(self, state):
        self.logger.debug('%s (was %s)', state, self.state)
        self.state = state

    @property
    def connected(self):
        return self.state=='CONNECTED'

    def shutdown(self, wait=True):
        """Send the messages already in queue then close the connection"""
        self.set_state('FLUSHING')
        self.keepalive_proc.kill()
        self.keepalive_proc = None
        self.outgoing.send(None)
        if wait:
            self.writer_job.wait(None, None)
            self.reader_job.wait(None, None)

    def _keepalive(self):
        while True:
            api.sleep(self.KEEPALIVE_INTERVAL)
            if not self.connected:
                return
            try:
                chunk = self.msrp.make_send_request()
                chunk.add_header(protocol.MSRPHeader('Keep-Alive', 'yes'))
                self.deliver_chunk(chunk)
            except MSRPTransactionError as e:
                if e.code == 408:
                    self.msrp.loseConnection(wait=False)
                    self.set_state('CLOSING')
                    return

    def _handle_incoming_response(self, chunk):
        try:
            response_cb, timer = self.expected_responses.pop(chunk.transaction_id)
        except KeyError:
            pass
        else:
            if timer is not None:
                timer.cancel()
            response_cb(chunk)

    def _check_incoming_SEND(self, chunk):
        error = self.msrp.check_incoming_SEND_chunk(chunk)
        if error is not None:
            return error
        if chunk.data:
            if chunk.content_type is None:
                return MSRPBadContentType('Content-Type header missing')
            if not contains_mime_type(self.accept_types, chunk.content_type):
                return MSRPBadContentType

    def _handle_incoming_SEND(self, chunk):
        error = self._check_incoming_SEND(chunk)
        if error is None:
            code, comment = 200, 'OK'
        else:
            code, comment = error.code, error.comment
        response = make_response(chunk, code, comment)
        if response is not None:
            self.outgoing.send(OutgoingChunk(response))
        if code == 200:
            self._on_incoming_cb(chunk)
            if self.automatic_reports:
                report = make_report(chunk, 200, 'OK')
                if report is not None:
                    self.outgoing.send(OutgoingChunk(report))

    def _handle_incoming_REPORT(self, chunk):
        self._on_incoming_cb(chunk)

    def _handle_incoming_NICKNAME(self, chunk):
        if 'Use-Nickname' not in chunk.headers or 'Success-Report' in chunk.headers or 'Failure-Report' in chunk.headers:
            response = make_response(chunk, 400, 'Bad request')
            self.outgoing.send(OutgoingChunk(response))
            return
        self._on_incoming_cb(chunk)

    def _reader(self):
        """Wait forever for new chunks. Notify the user about the good ones through self._on_incoming_cb.

        If a response to a previously sent chunk is received, pop the corresponding
        response_cb from self.expected_responses and send the response there.
        """
        error = Failure(ConnectionDone())
        try:
            self.writer_job.link(self.reader_job)
            try:
                while self.state in ['CONNECTED', 'FLUSHING']:
                    chunk = self.msrp.read_chunk()
                    if chunk.method is None: # response
                        self._handle_incoming_response(chunk)
                    else:
                        method = getattr(self, '_handle_incoming_%s' % chunk.method, None)
                        if method is not None:
                            method(chunk)
                        else:
                            response = make_response(chunk, 501, 'Method unknown')
                            self.outgoing.send(OutgoingChunk(response))
            except proc.LinkedExited: # writer has exited
                pass
            finally:
                self.writer_job.unlink(self.reader_job)
                self.writer_job.kill()
            self.logger.debug('reader: expecting responses only')
            delay = time() - self.last_expected_response
            if delay>=0 and self.expected_responses:
                # continue read the responses until the last timeout expires
                with api.timeout(delay, None):
                    while self.expected_responses:
                        chunk = self.msrp.read_chunk()
                        if chunk.method is None:
                            self._handle_incoming_response(chunk)
                        else:
                            self.logger.debug('dropping incoming %r', chunk)
                # read whatever left in the queue
                with api.timeout(0, None):
                    while self.msrp._queue:
                        chunk = self.msrp.read_chunk()
                        if chunk.method is None:
                            self._handle_incoming_response(chunk)
                        else:
                            self.logger.debug('dropping incoming %r', chunk)
            self.logger.debug('reader: done')
        except ConnectionClosedErrors as e:
            self.logger.debug('reader: exiting because of %r', e)
            error = Failure(e)
        except Exception:
            self.logger.exception('reader: captured unhandled exception:')
            error = Failure()
            raise
        finally:
            self._on_incoming_cb(error=error)
            self.msrp.loseConnection(wait=False)
            self.set_state('DONE')

    def _writer(self):
        try:
            while self.state=='CONNECTED' or (self.state=='FLUSHING' and self.outgoing):
                item = self.outgoing.wait()
                if item is None:
                    break
                self._write_chunk(item.chunk, item.response_callback)
        except ConnectionClosedErrors + (proc.LinkedExited, proc.ProcExit) as e:
            self.logger.debug('writer: exiting because of %r' % e)
        except:
            self.logger.exception('writer: captured unhandled exception:')
            raise
        finally:
            self.msrp.loseConnection(wait=False)
            self.set_state('CLOSING')

    def _write_chunk(self, chunk, response_cb=None):
        assert chunk.transaction_id not in self.expected_responses, "MSRP transaction %r is already in progress" % chunk.transaction_id
        self.msrp.write_chunk(chunk)
        if response_cb is not None:
            timer = api.get_hub().schedule_call_global(self.RESPONSE_TIMEOUT, self._response_timeout, chunk.transaction_id, Response408Timeout)
            self.expected_responses[chunk.transaction_id] = (response_cb, timer)
            self.last_expected_response = time() + self.RESPONSE_TIMEOUT

    def _response_timeout(self, id, timeout_error):
        response_cb, timer = self.expected_responses.pop(id, (None, None))
        if response_cb is not None:
            response_cb(timeout_error)
            if timer is not None:
                timer.cancel()

    def send_chunk(self, chunk, response_cb=None):
        """Send `chunk'. Report the result via `response_cb'.

        When `response_cb' argument is present, it will be used to report
        the transaction response to the caller. When a response is received
        or generated locally, `response_cb' is called with one argument. The function
        must do something quickly and must not block, because otherwise it would
        the reader greenlet.

        If no response was received after RESPONSE_TIMEOUT seconds,
        * 408 response is generated if Failure-Report was 'yes' or absent
        * 200 response is generated if Failure-Report was 'partial' or 'no'

        Note that it's rather wasteful to provide `response_cb' argument other than None
        for chunks with Failure-Report='no' since it will always fire 30 seconds later
        with 200 result (unless the other party is broken and ignores Failure-Report header)

        If sending is impossible raise MSRPSessionError.
        """
        assert chunk.transaction_id not in self.expected_responses, "MSRP transaction %r is already in progress" % chunk.transaction_id
        if response_cb is not None and not callable(response_cb):
            raise TypeError('response_cb must be callable: %r' % (response_cb, ))
        if self.state != 'CONNECTED':
            raise MSRPSessionError('Cannot send chunk because MSRPSession is %s' % self.state)
        if self.msrp._disconnected_event.ready():
            raise MSRPSessionError(str(self.msrp._disconnected_event.wait()))
        self.outgoing.send(OutgoingChunk(chunk, response_cb))

    def send_report(self, chunk, code, reason):
        if chunk.method != 'SEND':
            raise ValueError('reports may only be sent for SEND chunks')
        report = make_report(chunk, code, reason)
        if report is not None:
            self.send_chunk(report)

    def deliver_chunk(self, chunk, event=None):
        """Send chunk, wait for the transaction response (if Failure-Report header is not 'no').
        Return the transaction response if it's a success, raise MSRPTransactionError if it's not.

        If chunk's Failure-Report is 'no', return None immediately.
        """
        if chunk.failure_report!='no' and event is None:
            event = coros.event()
        self.send_chunk(chunk, event.send)
        if event is not None:
            response = event.wait()
            if isinstance(response, Exception):
                raise response
            elif 200 <= response.code <= 299:
                return response
            raise MSRPTransactionError(comment=response.comment, code=response.code)

    def make_message(self, msg, content_type, message_id=None):
        chunk = self.msrp.make_send_request(data=msg, message_id=message_id)
        chunk.add_header(protocol.ContentTypeHeader(content_type))
        return chunk

    def send_message(self, msg, content_type):
        chunk = self.make_message(msg, content_type)
        self.send_chunk(chunk)
        return chunk

    def deliver_message(self, msg, content_type):
        chunk = self.make_message(msg, content_type)
        self.deliver_chunk(chunk)
        return chunk


class GreenMSRPSession(MSRPSession):

    def __init__(self, msrptransport, accept_types=['*']):
        MSRPSession.__init__(self, msrptransport, accept_types, on_incoming_cb=self._incoming_cb)
        self.incoming = ValueQueue()

    def receive_chunk(self):
        return self.incoming.wait()

    def _incoming_cb(self, value=None, error=None):
        if error is not None:
            self.incoming.send_exception(error.type, error.value, error.tb)
        else:
            self.incoming.send(value)

# TODO:
# 413 - requires special action both in reader and in writer
# continuation: #
# All MSRP endpoints MUST be able to receive the multipart/mixed [15] and multipart/alternative [15] media-types.
