import asyncio

import aiohttp
import OpenSSL.SSL

import aioxmpp.xml as xml
import aioxmpp.ssl_transport as ssl_transport

from aioxmpp.utils import namespaces


namespaces.framing = "urn:ietf:params:xml:ns:xmpp-framing"


class WebsocketElements:
    OPEN = (namespaces.framing, "open")
    CLOSE = (namespaces.framing, "close")


class WebsocketXMLStreamWriter(xml.BaseXMLStreamWriter):

    def start(self):
        self._writer.startElementNS(WebsocketElements.OPEN, None, self._attrs)
        self._writer.endElementNS(WebsocketElements.OPEN, None)
        self._writer.flush()

    def close(self):
        if self._closed:
            return
        self._closed = True

        self._writer.startElementNS(WebsocketElements.CLOSE, None)
        self._writer.endElementNS(WebsocketElements.CLOSE, None)
        self._writer.endDocument()
        self._writer.flush()
        del self._writer


class WebsocketXMPPXMLProcessor(xml.XMPPXMLProcessor):

    def __init__(self):
        super().__init__()
        self._stream_header_tag = WebsocketElements.OPEN

    def startElementNS(self, name, qname, attributes):
        if name != WebsocketElements.CLOSE:
            super().startElementNS(name, qname, attributes)

    def endElementNS(self, name, qname):
        if name != WebsocketElements.OPEN:
            super().endElementNS(name, qname)


class AIOOpenSSLHTTPConnector(aiohttp.TCPConnector):

    def __init__(self, *args, ssl, **kwargs):
        # We always provide a ssl context factory but aiohttp doesn't support
        # to have a callable for ssl context.
        super().__init__(*args, ssl=None, **kwargs)
        self._ssl = ssl

    def _get_ssl_context(self, req: "ClientRequest"):
        if not req.is_ssl():
            return None
        elif req.ssl is None:
            return self._ssl
        else:
            return super()._get_ssl_context(req)

    async def _wrap_create_connection(
        self,
        *args,
        req: "ClientRequest",
        timeout: "ClientTimeout",
        client_error=aiohttp.ClientConnectorError,
        **kwargs,
    ):
        ssl = kwargs.get('ssl')
        if not callable(ssl):
            return await super()._wrap_create_connection(
                *args, req=req, timeout=timeout, client_error=client_error,
                **kwargs)

        try:
            ceil_timeout = aiohttp.helpers.ceil_timeout # aiohttp>=3.7.0
        except AttributeError:
            ceil_timeout = aiohttp.helpers.CeilTimeout # aiohttp<3.7.0

        try:
            with ceil_timeout(timeout.sock_connect):
                return await ssl_transport.create_starttls_connection(
                    self._loop, *args,
                    sock=kwargs.get("sock"),
                    ssl_context_factory=ssl,
                    local_addr=kwargs.get("local_addr"),
                    server_hostname=kwargs.get("server_hostname"),
                )
        except OpenSSL.SSL.Error as exc:
            raise aiohttp.ClientSSLError(req.connection_key, OSError()) from exc
        except OSError as exc:
            raise client_error(req.connection_key, exc) from exc


class WebsocketTransport(asyncio.Transport):

    def __init__(self, loop, stream, logger, timeout, ssl_context_factory, decode=None):
        self.loop = loop
        self.stream = stream
        self.logger = logger
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._ssl_context = ssl_context_factory
        self._decode = decode

        self.connection = None
        self._buffer = b''
        self._reader_task = None
        self._called_lost = False
        self._attempt_reconnect = True

    async def create_connection(self, *args, **kwargs):
        self.logger.debug('Setting up new websocket connection.')

        self.session = aiohttp.ClientSession(
            connector=AIOOpenSSLHTTPConnector(ssl=self._ssl_context),
            timeout=self._timeout)
        self.connection = await self.session.ws_connect(*args, **kwargs)

        self._reader_task = self.loop.create_task(self.reader())
        self.stream.connection_made(self)
        self._called_lost = False
        self._attempt_reconnect = True

        self.logger.debug('Websocket connection established.')
        return self.connection

    async def reader(self):
        self.logger.debug('Websocket reader is now running.')

        try:
            async for msg in self.connection:
                self.logger.debug('RECV: {0}'.format(msg))
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self.stream.data_received(msg.data)

                if msg.type == aiohttp.WSMsgType.CLOSED:
                    if self._attempt_reconnect:
                        err = ConnectionError(
                            'websocket stream closed'
                        )
                    else:
                        err = None

                    if not self._called_lost:
                        self._called_lost = True
                        self.stream.connection_lost(err)
                        self._close_session()

                    break

                if msg.type == aiohttp.WSMsgType.ERROR:
                    if not self._called_lost:
                        self._called_lost = True
                        self.stream.connection_lost(
                            ConnectionError(
                                'websocket stream received an error: '
                                '{0}'.format(self.connection.exception()))
                        )
                    break
        finally:
            self.logger.debug('Websocket reader stopped.')

    async def send(self, data):
        send = self.connection.send_bytes
        if self._decode:
            data = data.decode(self._decode)
            send = self.connection.send_str

        self.logger.debug('SEND: {0}'.format(data))
        await send(data)

    def write(self, data):
        self._buffer += data

    def flush(self):
        if self._buffer:
            asyncio.ensure_future(self.send(self._buffer))
        self._buffer = b''

    def can_write_eof(self):
        return False

    def write_eof(self):
        raise NotImplementedError("Cannot write_eof() on ws transport.")

    def _stop_reader(self):
        if self._reader_task is not None and not self._reader_task.cancelled():
            self._reader_task.cancel()

    def _close_session(self, *args):
        try:
            self.loop.create_task(self.session.close())
        except AttributeError:
            pass

    def _close(self):
        if not self.connection:
            raise RuntimeError('Cannot close a non-existing connection.')

        self.logger.debug('Closing websocket connection.')

        task = self.loop.create_task(self.connection.close())
        task.add_done_callback(self._close_session)

        self._stop_reader()

    def close(self):
        self._attempt_reconnect = False
        self._close()

    def abort(self):
        self.logger.debug('Received abort signal.')
        self._close()

    def get_extra_info(self, *args, **kwargs):
        return self.connection.get_extra_info(*args, **kwargs)
