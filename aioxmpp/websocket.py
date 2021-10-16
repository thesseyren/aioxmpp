import asyncio
import logging

import aiohttp

from .utils import namespaces
from .protocol import DebugWrapper, XMLStream
from .xml import (
    BaseXMLStreamWriter,
    XMPPXMLProcessor,
    NonRootXMLParser,
)


namespaces.framing = "urn:ietf:params:xml:ns:xmpp-framing"


class WebsocketElements:
    OPEN = (namespaces.framing, "open")
    CLOSE = (namespaces.framing, "close")


class WebsocketXMLStreamWriter(BaseXMLStreamWriter):

    def start(self):
        for prefix, uri in self._nsmap_to_use.items():
            self._writer.startPrefixMapping(prefix, uri)

        self._writer.startElementNS(WebsocketElements.OPEN, None, self._attrs)
        self._writer.endElementNS(WebsocketElements.OPEN, None)

        self._writer.flush()

    def close(self):
        if self._closed:
            return
        self._closed = True

        self._writer.startElementNS(WebsocketElements.CLOSE, None)
        self._writer.endElementNS(WebsocketElements.CLOSE, None)

        for prefix in self._nsmap_to_use:
            self._writer.endPrefixMapping(prefix)
        self._writer.endDocument()
        self._writer.flush()
        del self._writer


class WebsocketXMPPXMLProcessor(XMPPXMLProcessor):

    def __init__(self):
        super().__init__()
        self._stream_header_tag = WebsocketElements.OPEN

    def startElementNS(self, name, qname, attributes):
        if name != WebsocketElements.CLOSE:
            super().startElementNS(name, qname, attributes)

    def endElementNS(self, name, qname):
        if name != WebsocketElements.OPEN:
            super().endElementNS(name, qname)


class WebsocketXMLStream(XMLStream):

    def _reset_state(self):
        self._kill_state()

        self._processor = WebsocketXMPPXMLProcessor()
        self._processor.stanza_parser = self.stanza_parser
        self._processor.on_stream_header = self._rx_stream_header
        self._processor.on_stream_footer = self._rx_stream_footer
        self._processor.on_exception = self._rx_exception
        self._parser = NonRootXMLParser()
        self._parser.setContentHandler(self._processor)
        self._debug_wrapper = None

        if self._logger.getEffectiveLevel() <= logging.DEBUG:
            dest = DebugWrapper(self._transport, self._logger)
            self._debug_wrapper = dest
        else:
            dest = self._transport

        self._writer = WebsocketXMLStreamWriter(
            dest,
            self._to,
            sorted_attributes=self._sorted_attributes)


class WebsocketTransport(asyncio.Transport):

    def __init__(self, loop, stream, logger, decode=None, ws_connector=None):
        self.loop = loop
        self.stream = stream
        self.logger = logger
        self.ws_connector = ws_connector
        self._decode = decode

        self.connection = None
        self._buffer = b''
        self._reader_task = None
        self._called_lost = False
        self._attempt_reconnect = True

    async def create_connection(self, *args, **kwargs):
        self.logger.debug('Setting up new websocket connection.')

        self.session = aiohttp.ClientSession(
            connector=self.ws_connector,
            connector_owner=self.ws_connector is None,
        )
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
