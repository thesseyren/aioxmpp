import io
import ssl
import unittest
import unittest.mock

import aiohttp
import OpenSSL.SSL

import aioxmpp.structs as structs
import aioxmpp.xso as xso
import aioxmpp.websocket as ws

from aioxmpp.testutils import run_coroutine


class Cls(xso.XSO):
    TAG = ("uri:foo", "bar")

    DECLARE_NS = {}


class TestWebsocketXMLStreamWriter(unittest.TestCase):
    TEST_TO = structs.JID.fromstr("example.test")
    TEST_FROM = structs.JID.fromstr("foo@example.test")

    STREAM_HEADER = (b'<open '
                     b'xmlns="urn:ietf:params:xml:ns:xmpp-framing" '
                     b'to="'+str(TEST_TO).encode("utf-8")+b'" '
                     b'version="1.0"/>')
    STREAM_FOOTER = (b'<close '
                     b'xmlns="urn:ietf:params:xml:ns:xmpp-framing"/>')

    def setUp(self):
        self.buf = io.BytesIO()

    def tearDown(self):
        del self.buf

    def _make_gen(self, **kwargs):
        return ws.WebsocketXMLStreamWriter(self.buf, self.TEST_TO,
                                           sorted_attributes=True,
                                           **kwargs)

    def test_setup(self):
        gen = self._make_gen()
        gen.start()
        gen.close()

        self.assertEqual(
            self.STREAM_HEADER + self.STREAM_FOOTER,
            self.buf.getvalue())

    def test_from(self):
        gen = self._make_gen(from_=self.TEST_FROM)
        gen.start()
        gen.close()

        self.assertEqual(
            b'<open '
            b'xmlns="urn:ietf:params:xml:ns:xmpp-framing" '
            b'from="'+str(self.TEST_FROM).encode("utf-8")+b'" '
            b'to="'+str(self.TEST_TO).encode("utf-8")+b'" '
            b'version="1.0"/>' + self.STREAM_FOOTER,
            self.buf.getvalue())

    def test_not_to_have_root_namespace(self):
        gen = self._make_gen(nsmap={None: "jabber:client"})
        gen.start()
        gen.close()

        self.assertEqual(
            self.STREAM_HEADER + self.STREAM_FOOTER,
            self.buf.getvalue())

    def test_send_object_not_inherits_namespaces(self):
        obj = Cls()
        gen = self._make_gen(nsmap={"jc": "uri:foo"})
        gen.start()
        gen.send(obj)
        gen.close()

        self.assertEqual(
            self.STREAM_HEADER +
            b'<bar xmlns="uri:foo"/>' +
            self.STREAM_FOOTER,
            self.buf.getvalue())

    def test_close_is_idempotent(self):
        obj = Cls()
        gen = self._make_gen()
        gen.start()
        gen.send(obj)
        gen.close()
        gen.close()
        gen.close()

        self.assertEqual(
            self.STREAM_HEADER +
            b'<bar xmlns="uri:foo"/>' +
            self.STREAM_FOOTER,
            self.buf.getvalue())


class TestWebsocketXMPPXMLProcessor(unittest.TestCase):
    STREAM_HEADER_TAG = ("urn:ietf:params:xml:ns:xmpp-framing", "open")
    STREAM_FOOTER_TAG = ("urn:ietf:params:xml:ns:xmpp-framing", "close")

    STREAM_HEADER_ATTRS = {
        (None, "from"): "example.test",
        (None, "to"): "foo@example.test",
        (None, "id"): "foobarbaz",
        (None, "version"): "1.0"
    }

    def setUp(self):
        self.proc = ws.WebsocketXMPPXMLProcessor()

    def tearDown(self):
        del self.proc

    def test_stream_start_and_end(self):
        header_callback = unittest.mock.Mock()
        footer_callback = unittest.mock.Mock()
        self.proc.on_stream_header = header_callback
        self.proc.on_stream_footer = footer_callback

        self.proc.startDocument()
        self.proc.startElementNS(
            self.STREAM_HEADER_TAG,
            None,
            self.STREAM_HEADER_ATTRS)
        self.proc.endElementNS(
            self.STREAM_HEADER_TAG,
            None)

        header_callback.assert_called_once()
        footer_callback.assert_not_called()
        header_callback.reset_mock()
        footer_callback.reset_mock()

        self.proc.startElementNS(self.STREAM_FOOTER_TAG, None, {})
        self.proc.endElementNS(self.STREAM_FOOTER_TAG, None)

        header_callback.assert_not_called()
        footer_callback.assert_called_once()


class TestAIOOpenSSLHTTPConnector(unittest.TestCase):

    def setUp(self):
        self.context_factory = lambda transport: object()
        self.loop = unittest.mock.Mock()
        self.connector = ws.AIOOpenSSLHTTPConnector(ssl=self.context_factory,
                                                    loop=self.loop)

    def tearDown(self):
        del self.context_factory
        del self.connector

    def _make_request(self, is_ssl, ssl):
        # aiohttp.ClientRequest
        req = unittest.mock.Mock()
        req.is_ssl = unittest.mock.Mock(return_value=is_ssl)
        type(req).ssl = unittest.mock.PropertyMock(return_value=ssl)
        return req

    def _make_timeout(self, sock_connect=60):
        t = unittest.mock.Mock()
        p = unittest.mock.PropertyMock(return_value=sock_connect)
        type(t).sock_connect = p
        return t

    def test_get_ssl_context(self):
        # request url starts with http:// and no need to ssl
        req = self._make_request(is_ssl=False, ssl=None)
        received = self.connector._get_ssl_context(req)
        self.assertIs(received, None)

        # need to ssl but user didn't pass anything to ssl= arg on client
        req = self._make_request(is_ssl=True, ssl=None)
        received = self.connector._get_ssl_context(req)
        self.assertIs(received, self.context_factory)

        # need to ssl and user passing something on ssl= arg
        req = self._make_request(is_ssl=True, ssl=False)
        received = self.connector._get_ssl_context(req)
        self.assertIsInstance(received, ssl.SSLContext)

        # same as above
        sslcontext = ssl.create_default_context()
        req = self._make_request(is_ssl=True, ssl=sslcontext)
        received = self.connector._get_ssl_context(req)
        self.assertIs(received, sslcontext)

    @unittest.mock.patch("aiohttp.TCPConnector._wrap_create_connection")
    def test_create_connection_without_context_factory(self, mock):
        req = self._make_request(is_ssl=True, ssl=None)
        timeout = self._make_timeout()

        run_coroutine(self.connector._wrap_create_connection(
            "protocol", "host", "port",
            req=req,
            timeout=timeout,
            client_error=aiohttp.ClientConnectorError,
            ssl=None))
        mock.assert_called_once_with(
            "protocol", "host", "port",
            req=req,
            timeout=timeout,
            client_error=aiohttp.ClientConnectorError,
            ssl=None)

    @unittest.mock.patch("aioxmpp.ssl_transport.create_starttls_connection")
    def test_create_connection_with_context_factory(self, mock):
        req = self._make_request(is_ssl=True, ssl=None)
        timeout = self._make_timeout()

        run_coroutine(self.connector._wrap_create_connection(
            "protocol", "host", "port",
            req=req,
            timeout=timeout,
            client_error=aiohttp.ClientConnectorError,
            ssl=self.context_factory,
            sock="sock",
            local_addr="local_addr",
            server_hostname="server_hostname"))
        mock.assert_called_once_with(
            self.loop, "protocol", "host", "port",
            sock="sock",
            ssl_context_factory=self.context_factory,
            local_addr="local_addr",
            server_hostname="server_hostname")

    def test_create_connection_errors(self):
        sslmodule = "aioxmpp.ssl_transport.create_starttls_connection"
        req = self._make_request(is_ssl=True, ssl=None)
        timeout = self._make_timeout()

        with unittest.mock.patch(sslmodule, side_effect=OpenSSL.SSL.Error):
            with self.assertRaises(aiohttp.ClientSSLError):
                run_coroutine(self.connector._wrap_create_connection(
                    req=req,
                    timeout=timeout,
                    ssl=self.context_factory))

        with unittest.mock.patch(sslmodule, side_effect=OSError):
            with self.assertRaises(aiohttp.ClientConnectorError):
                run_coroutine(self.connector._wrap_create_connection(
                    req=req,
                    timeout=timeout,
                    client_error=aiohttp.ClientConnectorError,
                    ssl=self.context_factory))
