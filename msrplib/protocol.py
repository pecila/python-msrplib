# Copyright (C) 2008-2012 AG Projects. See LICENSE for details

import random
import re

from application.system import host as host_module
from collections import deque, namedtuple
from gnutls.interfaces.twisted import X509Credentials
from twisted.internet.protocol import connectionDone
from twisted.protocols.basic import LineReceiver

from msrplib import MSRPError


class ParsingError(MSRPError):
    pass


class HeaderParsingError(ParsingError):
    def __init__(self, header, reason):
        self.header = header
        ParsingError.__init__(self, 'Error parsing {} header: {}'.format(header, reason))


# Header value data types (for decoded values)
#

ByteRange = namedtuple('ByteRange', ['start', 'end', 'total'])
Status = namedtuple('Status', ['code', 'comment'])
ContentDisposition = namedtuple('ContentDisposition', ['disposition', 'parameters'])


# Header value types (describe how to encode/decode the value)
#

class SimpleHeaderType(object):
    data_type = object

    @staticmethod
    def decode(encoded):
        return encoded

    @staticmethod
    def encode(decoded):
        return decoded


class UTF8HeaderType(object):
    data_type = str

    @staticmethod
    def decode(encoded):
        return encoded.decode('utf-8')

    @staticmethod
    def encode(decoded):
        return decoded.encode('utf-8')


class URIHeaderType(object):
    data_type = deque

    @staticmethod
    def decode(encoded):
        return deque(URI.parse(uri) for uri in encoded.split(' '))

    @staticmethod
    def encode(decoded):
        return ' '.join(str(uri) for uri in decoded)


class IntegerHeaderType(object):
    data_type = int

    @staticmethod
    def decode(encoded):
        return int(encoded)

    @staticmethod
    def encode(decoded):
        return str(decoded)


class LimitedChoiceHeaderType(SimpleHeaderType):
    allowed_values = frozenset()

    @classmethod
    def decode(cls, encoded):
        if encoded not in cls.allowed_values:
            raise ValueError('Invalid value: {!r}'.format(encoded))
        return encoded


class SuccessReportHeaderType(LimitedChoiceHeaderType):
    allowed_values = frozenset({'yes', 'no'})


class FailureReportHeaderType(LimitedChoiceHeaderType):
    allowed_values = frozenset({'yes', 'no', 'partial'})


class ByteRangeHeaderType(object):
    data_type = ByteRange

    regex = re.compile(r'(?P<start>\d+)-(?P<end>\*|\d+)/(?P<total>\*|\d+)')

    @classmethod
    def decode(cls, encoded):
        match = cls.regex.match(encoded)
        if match is None:
            raise ValueError('Invalid byte range value: {!r}'.format(encoded))
        start, end, total = match.groups()
        start = int(start)
        end = int(end) if end != '*' else None
        total = int(total) if total != '*' else None
        return ByteRange(start, end, total)

    @staticmethod
    def encode(decoded):
        start, end, total = decoded
        return '{}-{}/{}'.format(start, '*' if end is None else end, '*' if total is None else total)


class StatusHeaderType(object):
    data_type = Status

    @staticmethod
    def decode(encoded):
        namespace, sep, rest = encoded.partition(' ')
        if namespace != '000' or sep != ' ':
            raise ValueError('Invalid status value: {!r}'.format(encoded))
        code, _, comment = rest.partition(' ')
        if not code.isdigit() or len(code) != 3:
            raise ValueError('Invalid status code: {!r}'.format(code))
        return Status(int(code), comment or None)

    @staticmethod
    def encode(decoded):
        code, comment = decoded
        if comment is None:
            return '000 {:03d}'.format(code)
        else:
            return '000 {:03d} {}'.format(code, comment)


class ContentDispositionHeaderType(object):
    data_type = ContentDisposition

    regex = re.compile(r'(\w+)=("[^"]+"|[^";]+)')

    @classmethod
    def decode(cls, encoded):
        disposition, _, parameters = encoded.partition(';')
        if not disposition:
            raise ValueError('Invalid content disposition: {!r}'.format(encoded))
        return ContentDisposition(disposition, {name: value.strip('"') for name, value in cls.regex.findall(parameters)})

    @staticmethod
    def encode(decoded):
        disposition, parameters = decoded
        return '; '.join([disposition] + ['{}="{}"'.format(name, value) for name, value in parameters.items()])


class ParameterListHeaderType(object):
    data_type = dict

    regex = re.compile(r'(\w+)=("[^"]+"|[^",]+)')

    @classmethod
    def decode(cls, encoded):
        return {name: value.strip('"') for name, value in cls.regex.findall(encoded)}

    @staticmethod
    def encode(decoded):
        return ', '.join('{}="{}"'.format(name, value) for name, value in decoded.items())


class DigestHeaderType(ParameterListHeaderType):
    @classmethod
    def decode(cls, encoded):
        algorithm, sep, parameters = encoded.partition(' ')
        if algorithm != 'Digest' or sep != ' ':
            raise ValueError('Invalid Digest header value')
        return super(DigestHeaderType, cls).decode(parameters)

    @staticmethod
    def encode(decoded):
        return 'Digest ' + super(DigestHeaderType, DigestHeaderType).encode(decoded)


# Header classes
#

class MSRPHeaderMeta(type):
    __classmap__ = {}

    name = None

    def __init__(cls, name, bases, dictionary):
        type.__init__(cls, name, bases, dictionary)
        if cls.name is not None:
            cls.__classmap__[cls.name] = cls

    def __call__(cls, *args, **kw):
        if cls.name is not None:
            return super(MSRPHeaderMeta, cls).__call__(*args, **kw)  # specialized class, instantiated directly.
        else:
            return cls._instantiate_specialized_class(*args, **kw)   # non-specialized class, instantiated as a more specialized class if available.

    def _instantiate_specialized_class(cls, name, value):
        if name in cls.__classmap__:
            return super(MSRPHeaderMeta, cls.__classmap__[name]).__call__(value)
        else:
            return super(MSRPHeaderMeta, cls).__call__(name, value)


class MSRPHeader(object, metaclass=MSRPHeaderMeta):
    name = None
    type = SimpleHeaderType

    def __init__(self, name, value):
        self.name = name
        if isinstance(value, str):
            self.encoded = value
        else:
            self.decoded = value

    def __eq__(self, other):
        if isinstance(other, MSRPHeader):
            return self.name == other.name and self.decoded == other.decoded
        return NotImplemented

    def __ne__(self, other):
        return not self == other

    @property
    def encoded(self):
        if self._encoded is None:
            self._encoded = self.type.encode(self._decoded)
        return self._encoded

    @encoded.setter
    def encoded(self, encoded):
        self._encoded = encoded
        self._decoded = None

    @property
    def decoded(self):
        if self._decoded is None:
            try:
                self._decoded = self.type.decode(self._encoded)
            except Exception as e:
                raise HeaderParsingError(self.name, str(e))
        return self._decoded

    @decoded.setter
    def decoded(self, decoded):
        if not isinstance(decoded, self.type.data_type):
            try:
                # noinspection PyArgumentList
                decoded = self.type.data_type(decoded)
            except Exception:
                raise TypeError('value must be an instance of {}'.format(self.type.data_type.__name__))
        self._decoded = decoded
        self._encoded = None


class MSRPNamedHeader(MSRPHeader):
    def __init__(self, value):
        MSRPHeader.__init__(self, self.name, value)


class ToPathHeader(MSRPNamedHeader):
    name = 'To-Path'
    type = URIHeaderType


class FromPathHeader(MSRPNamedHeader):
    name = 'From-Path'
    type = URIHeaderType


class MessageIDHeader(MSRPNamedHeader):
    name = 'Message-ID'
    type = SimpleHeaderType


class SuccessReportHeader(MSRPNamedHeader):
    name = 'Success-Report'
    type = SuccessReportHeaderType


class FailureReportHeader(MSRPNamedHeader):
    name = 'Failure-Report'
    type = FailureReportHeaderType


class ByteRangeHeader(MSRPNamedHeader):
    name = 'Byte-Range'
    type = ByteRangeHeaderType

    @property
    def start(self):
        return self.decoded.start

    @property
    def end(self):
        return self.decoded.end

    @property
    def total(self):
        return self.decoded.total


class StatusHeader(MSRPNamedHeader):
    name = 'Status'
    type = StatusHeaderType

    @property
    def code(self):
        return self.decoded.code

    @property
    def comment(self):
        return self.decoded.comment


class ExpiresHeader(MSRPNamedHeader):
    name = 'Expires'
    type = IntegerHeaderType


class MinExpiresHeader(MSRPNamedHeader):
    name = 'Min-Expires'
    type = IntegerHeaderType


class MaxExpiresHeader(MSRPNamedHeader):
    name = 'Max-Expires'
    type = IntegerHeaderType


class UsePathHeader(MSRPNamedHeader):
    name = 'Use-Path'
    type = URIHeaderType


class WWWAuthenticateHeader(MSRPNamedHeader):
    name = 'WWW-Authenticate'
    type = DigestHeaderType


class AuthorizationHeader(MSRPNamedHeader):
    name = 'Authorization'
    type = DigestHeaderType


class AuthenticationInfoHeader(MSRPNamedHeader):
    name = 'Authentication-Info'
    type = ParameterListHeaderType


class ContentTypeHeader(MSRPNamedHeader):
    name = 'Content-Type'
    type = SimpleHeaderType


class ContentIDHeader(MSRPNamedHeader):
    name = 'Content-ID'
    type = SimpleHeaderType


class ContentDescriptionHeader(MSRPNamedHeader):
    name = 'Content-Description'
    type = SimpleHeaderType


class ContentDispositionHeader(MSRPNamedHeader):
    name = 'Content-Disposition'
    type = ContentDispositionHeaderType


class UseNicknameHeader(MSRPNamedHeader):
    name = 'Use-Nickname'
    type = UTF8HeaderType


class HeaderOrderMapping(dict):
    __levels__ = {
        0: ['To-Path'],
        1: ['From-Path'],
        2: ['Status', 'Message-ID', 'Byte-Range', 'Success-Report', 'Failure-Report'] +
           ['Authorization', 'Authentication-Info', 'WWW-Authenticate', 'Expires', 'Min-Expires', 'Max-Expires', 'Use-Path', 'Use-Nickname'],
        3: ['Content-ID', 'Content-Description', 'Content-Disposition'],
        4: ['Content-Type']
    }

    def __init__(self):
        super(HeaderOrderMapping, self).__init__({name: level for level, name_list in list(self.__levels__.items()) for name in name_list})

    def __missing__(self, key):
        return 3 if key.startswith('Content-') else 2

    sort_key = dict.__getitem__


class HeaderOrdering(object):
    name_map = HeaderOrderMapping()
    sort_key = name_map.sort_key


class MissingHeader(object):
    decoded = None


class HeaderMapping(dict):
    def __init__(self, *args, **kw):
        super(HeaderMapping, self).__init__(*args, **kw)
        self.__modified__ = True

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__, super(HeaderMapping, self).__repr__())

    def __setitem__(self, key, value):
        super(HeaderMapping, self).__setitem__(key, value)
        self.__modified__ = True

    def __delitem__(self, key):
        super(HeaderMapping, self).__delitem__(key)
        self.__modified__ = True

    def __copy__(self):
        return self.__class__(self)

    def clear(self):
        super(HeaderMapping, self).clear()
        self.__modified__ = True

    def copy(self):
        return self.__class__(self)

    def pop(self, *args):
        result = super(HeaderMapping, self).pop(*args)
        self.__modified__ = True
        return result

    def popitem(self):
        result = super(HeaderMapping, self).popitem()
        self.__modified__ = True
        return result

    def setdefault(self, *args):
        result = super(HeaderMapping, self).setdefault(*args)
        self.__modified__ = True
        return result

    def update(self, *args, **kw):
        super(HeaderMapping, self).update(*args, **kw)
        self.__modified__ = True


class MSRPData(object):
    __immutable__ = frozenset({'method', 'code', 'comment', 'headers'})  # Immutable attributes (cannot be overwritten)

    def __init__(self, transaction_id, method=None, code=None, comment=None, headers=None, data='', contflag='$'):
        if method is None and code is None:
            raise ValueError('either method or code must be specified')
        elif method is not None and code is not None:
            raise ValueError('method and code cannot be both specified')
        elif code is None and comment is not None:
            raise ValueError('comment should only be specified when code is specified')
        self.transaction_id = transaction_id
        self.method = method
        self.code = code
        self.comment = comment
        self.headers = HeaderMapping(headers or {})
        self.data = data
        self.contflag = contflag
        self.chunk_header = None  # the chunk header (if the data was received from the network)
        self.chunk_footer = None  # the chunk footer (if the data was received from the network)
        if method is not None:
            self.first_line = 'MSRP {} {}'.format(transaction_id, method)
        elif comment is None:
            self.first_line = 'MSRP {} {:03d}'.format(transaction_id, code)
        else:
            self.first_line = 'MSRP {} {:03d} {}'.format(transaction_id, code, comment)
        self.__modified__ = True

    def __setattr__(self, name, value):
        if name in self.__dict__:
            if name in self.__immutable__:
                raise AttributeError('Cannot overwrite attribute')
            elif name == 'transaction_id':
                self.first_line = self.first_line.replace(self.transaction_id, value)
                self.__modified__ = True
        super(MSRPData, self).__setattr__(name, value)

    def __delattr__(self, name):
        if name in self.__immutable__:
            raise AttributeError('Cannot delete attribute')
        super(MSRPData, self).__delattr__(name)

    def __str__(self):  # TODO: make __str__ == encode()?
        return self.first_line

    def __repr__(self):
        description = self.first_line
        for name in sorted(self.headers, key=HeaderOrdering.sort_key):
            description += ' {}={!r}'.format(name, self.headers[name].encoded)
        description += ' len={}'.format(self.size)
        return '<{} at {:#x} {} {}>'.format(self.__class__.__name__, id(self), description, self.contflag)

    def __eq__(self, other):
        if isinstance(other, MSRPData):
            return self.first_line == other.first_line and self.headers == other.headers and self.data == other.data and self.contflag == other.contflag
        return NotImplemented

    def __ne__(self, other):
        return not self == other

    def copy(self):
        return self.__class__(self.transaction_id, self.method, self.code, self.comment, self.headers.copy(), self.data, self.contflag)

    def add_header(self, header):
        self.headers[header.name] = header

    def verify_headers(self):
        if 'To-Path' not in self.headers:
            raise HeaderParsingError('To-Path', 'header is missing')
        if 'From-Path' not in self.headers:
            raise HeaderParsingError('From-Path', 'header is missing')
        for header in self.headers.values():
            _ = header.decoded

    @property
    def from_path(self):
        return self.headers.get('From-Path', MissingHeader).decoded

    @property
    def to_path(self):
        return self.headers.get('To-Path', MissingHeader).decoded

    @property
    def content_type(self):
        return self.headers.get('Content-Type', MissingHeader).decoded

    @property
    def message_id(self):
        return self.headers.get('Message-ID', MissingHeader).decoded

    @property
    def byte_range(self):
        return self.headers.get('Byte-Range', MissingHeader).decoded

    @property
    def status(self):
        return self.headers.get('Status', MissingHeader).decoded

    @property
    def failure_report(self):
        return self.headers.get('Failure-Report', MissingHeader).decoded or 'yes'

    @property
    def success_report(self):
        return self.headers.get('Success-Report', MissingHeader).decoded or 'no'

    @property
    def size(self):
        return len(self.data)

    @property
    def encoded_header(self):
        if self.__modified__ or self.headers.__modified__:
            lines = [self.first_line] + ['{}: {}'.format(name, self.headers[name].encoded) for name in sorted(self.headers, key=HeaderOrdering.sort_key)]
            if 'Content-Type' in self.headers:
                lines.append('\r\n')
            self.__dict__['encoded_header'] = '\r\n'.join(lines)
            self.__modified__ = self.headers.__modified__ = False
        return self.__dict__['encoded_header']

    @property
    def encoded_footer(self):
        return '\r\n-------{}{}\r\n'.format(self.transaction_id, self.contflag)

    def encode(self):
        return self.encoded_header + self.data + self.encoded_footer


# noinspection PyProtectedMember
class MSRPProtocol(LineReceiver):
    # TODO: _ in the method name is not legal, but sipsimple defined the FILE_OFFSET method
    first_line_re = re.compile(r'^MSRP ([A-Za-z0-9][A-Za-z0-9.+%=-]{3,31}) (?:([A-Z_]+)|(\d{3})(?: (.+))?)$')

    MAX_LENGTH = 16384
    MAX_LINES = 64

    def __init__(self, msrp_transport):
        self.msrp_transport = msrp_transport
        self.term_buf = ''
        self.term_re = None
        self.term_substrings = []
        self.data = None
        self.line_count = 0

    def _reset(self):
        self.data = None
        self.line_count = 0

    def connectionMade(self):
        self.msrp_transport._got_transport(self.transport)

    def lineReceived(self, line):
        line = line.decode('utf-8')
        if self.data:
            if len(line) == 0:
                terminator = '\r\n-------' + self.data.transaction_id
                continue_flags = [c+'\r\n' for c in '$#+']
                self.term_buf = ''
                self.term_re = re.compile("^(.*)%s([$#+])\r\n(.*)$" % re.escape(terminator), re.DOTALL)
                self.term_substrings = [terminator[:i] for i in range(1, len(terminator)+1)] + [terminator+cont[:i] for cont in continue_flags for i in range(1, len(cont))]
                self.term_substrings.reverse()

                self.data.chunk_header += self.delimiter.decode('utf-8')
                self.msrp_transport._data_start(self.data)
                self.setRawMode()
            else:
                match = self.term_re.match(line)
                if match:
                    continuation = match.group(1)
                    self.data.chunk_footer = line + self.delimiter.decode('utf-8')
                    self.msrp_transport._data_start(self.data)
                    self.msrp_transport._data_end(continuation)
                    self._reset()
                else:
                    self.data.chunk_header += line + self.delimiter.decode('utf-8')
                    self.line_count += 1
                    if self.line_count > self.MAX_LINES:
                        self.msrp_transport.logger.received_illegal_data(self.data.chunk_header, self.msrp_transport)
                        self._reset()
                        return
                    try:
                        name, value = line.split(': ', 1)
                    except ValueError:
                        return  # let this pass silently, we'll just not read this line. TODO: review this (ignore or drop connection?)
                    else:
                        self.data.add_header(MSRPHeader(name, value))
        else:
            # this is a new message. TODO: drop connection if first line cannot be parsed?
            match = self.first_line_re.match(line)
            if match:
                transaction_id, method, code, comment = match.groups()
                code = int(code) if code is not None else None
                self.data = MSRPData(transaction_id, method, code, comment)
                self.data.chunk_header = line + self.delimiter.decode('utf-8')
                self.term_re = re.compile(r'^-------{}([$#+])$'.format(re.escape(transaction_id)))
            else:
                self.msrp_transport.logger.received_illegal_data(line + self.delimiter.decode('utf-8'), self.msrp_transport)

    def lineLengthExceeded(self, line):
        self._reset()

    def rawDataReceived(self, data):
        data = self.term_buf + data.decode('utf-8')
        match = self.term_re.match(data)
        if match:  # we got the last data for this message
            contents, continuation, extra = match.groups()
            if contents:
                self.msrp_transport._data_write(contents, final=True)
            self.data.chunk_footer = '\r\n-------{}{}\r\n'.format(self.data.transaction_id, continuation)
            self.msrp_transport._data_end(continuation)
            self._reset()
            self.setLineMode(extra)
        else:
            for term in self.term_substrings:
                if data.endswith(term):
                    self.term_buf = data[-len(term):]
                    data = data[:-len(term)]
                    break
            else:
                self.term_buf = ''
            self.msrp_transport._data_write(data, final=False)

    def connectionLost(self, reason=connectionDone):
        self.msrp_transport.connection_lost(reason)


class ConnectInfo(object):
    host = None
    use_tls = True
    port = 2855

    def __init__(self, host=None, use_tls=None, port=None, credentials=None):
        if host is not None:
            self.host = host
        if use_tls is not None:
            self.use_tls = use_tls
        if port is not None:
            self.port = port
        self.credentials = credentials
        if self.use_tls and self.credentials is None:
            self.credentials = X509Credentials(None, None)

    @property
    def scheme(self):
        if self.use_tls:
            return 'msrps'
        else:
            return 'msrp'


# use TLS_URI and TCP_URI ?
class URI(ConnectInfo):
    _uri_re = re.compile(r'^(?P<scheme>.*?)://(((?P<user>.*?)@)?(?P<host>.*?)(:(?P<port>[0-9]+?))?)(/(?P<session_id>.*?))?;(?P<transport>.*?)(;(?P<parameters>.*))?$')

    def __init__(self, host=None, use_tls=None, user=None, port=None, session_id=None, transport="tcp", parameters=None, credentials=None):
        ConnectInfo.__init__(self, host or host_module.default_ip, use_tls=use_tls, port=port, credentials=credentials)
        if session_id is None:
            session_id = '%x' % random.getrandbits(80)
        if parameters is None:
            parameters = {}
        self.user = user
        self.transport = transport
        self.session_id = session_id
        self.parameters = parameters

    # noinspection PyTypeChecker
    @classmethod
    def parse(cls, value):
        match = cls._uri_re.match(value)
        if match is None:
            raise ParsingError('Cannot parse URI')

        uri_params = match.groupdict()
        scheme = uri_params.pop('scheme')

        if scheme not in ('msrp', 'msrps'):
            raise ParsingError('Invalid URI scheme: %r' % scheme)
        if uri_params['transport'] != 'tcp':
            raise ParsingError("Invalid URI transport: %r (only 'tcp' is accepted)" % uri_params['transport'])

        uri_params['use_tls'] = scheme == 'msrps'
        if uri_params['port'] is not None:
            uri_params['port'] = int(uri_params['port'])
        if uri_params['parameters'] is not None:
            try:
                uri_params['parameters'] = dict(param.split('=') for param in uri_params['parameters'].split(';'))
            except ValueError:
                raise ParsingError('Cannot parse URI parameters')

        return cls(**uri_params)

    def __repr__(self):
        arguments = 'host', 'use_tls', 'user', 'port', 'session_id', 'transport', 'parameters', 'credentials'
        return '{}({})'.format(self.__class__.__name__, ', '.join('{}={!r}'.format(name, getattr(self, name)) for name in arguments))

    def __str__(self):
        user_part = '{}@'.format(self.user) if self.user else ''
        port_part = ':{}'.format(self.port) if self.port else ''
        session_part = '/{}'.format(self.session_id) if self.session_id else ''
        parameter_parts = [';{}={}'.format(name, value) for name, value in self.parameters.items()] if self.parameters else []
        return ''.join([self.scheme, '://', user_part, self.host, port_part, session_part, ';', self.transport] + parameter_parts)

    def __eq__(self, other):
        if self is other:
            return True
        if isinstance(other, URI):
            # MSRP URI comparison according to section 6.1 of RFC 4975
            self_items = self.use_tls, self.host.lower(), self.port, self.session_id, self.transport.lower()
            other_items = other.use_tls, other.host.lower(), other.port, other.session_id, other.transport.lower()
            return self_items == other_items
        return NotImplemented

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.use_tls, self.host.lower(), self.port, self.session_id, self.transport.lower()))
