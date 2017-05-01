########################################################################
# File name: service.py
# This file is part of: aioxmpp
#
# LICENSE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
########################################################################
import asyncio
import copy
import functools
import logging
import os
import tempfile
import urllib.parse

import aioxmpp.callbacks
import aioxmpp.disco as disco
import aioxmpp.service
import aioxmpp.xml
import aioxmpp.xso

from aioxmpp.utils import namespaces

from . import xso as my_xso
from . import caps115


logger = logging.getLogger("aioxmpp.entitycaps")


class Cache:
    """
    This provides a two-level cache for entity capabilities information. The
    idea is to have a trusted database, e.g. installed system-wide or shipped
    with :mod:`aioxmpp` and in addition a user-level database which is
    automatically filled with hashes which have been found by the
    :class:`Service`.

    The trusted database is taken as read-only and overrides the user-collected
    database. When a hash is in both databases, it is removed from the
    user-collected database (to save space).

    In addition to serving the databases, it provides deduplication for queries
    by holding a cache of futures looking up the same hash.

    Database management (user API):

    .. automethod:: set_system_db_path

    .. automethod:: set_user_db_path

    Queries (API intended for :class:`Service`):

    .. automethod:: create_query_future

    .. automethod:: lookup_in_database

    .. automethod:: lookup
    """

    def __init__(self):
        self._lookup_cache = {}
        self._memory_overlay = {}
        self._system_db_path = None
        self._user_db_path = None

    def _erase_future(self, key, fut):
        try:
            existing = self._lookup_cache[key]
        except KeyError:
            pass
        else:
            if existing is fut:
                del self._lookup_cache[key]

    def set_system_db_path(self, path):
        self._system_db_path = path

    def set_user_db_path(self, path):
        self._user_db_path = path

    def lookup_in_database(self, key):
        try:
            result = self._memory_overlay[key]
        except KeyError:
            pass
        else:
            logger.debug("memory cache hit: %s", key)
            return result

        key_path = key.path

        if self._system_db_path is not None:
            try:
                f = (
                    self._system_db_path / key_path
                ).open("rb")
            except OSError:
                pass
            else:
                logger.debug("system db hit: %s", key)
                with f:
                    return aioxmpp.xml.read_single_xso(f, disco.xso.InfoQuery)

        if self._user_db_path is not None:
            try:
                f = (
                    self._user_db_path / key_path
                ).open("rb")
            except OSError:
                pass
            else:
                logger.debug("user db hit: %s", key)
                with f:
                    return aioxmpp.xml.read_single_xso(f, disco.xso.InfoQuery)

        raise KeyError(key)

    @asyncio.coroutine
    def lookup(self, key):
        """
        Look up the given `node` URL using the given `hash_` first in the
        database and then by waiting on the futures created with
        :meth:`create_query_future` for that node URL and hash.

        If the hash is not in the database, :meth:`lookup` iterates as long as
        there are pending futures for the given `hash_` and `node`. If there
        are no pending futures, :class:`KeyError` is raised. If a future raises
        a :class:`ValueError`, it is ignored. If the future returns a value, it
        is used as the result.
        """
        try:
            result = self.lookup_in_database(key)
        except KeyError:
            pass
        else:
            return result

        while True:
            fut = self._lookup_cache[key]
            try:
                result = yield from fut
            except ValueError:
                continue
            else:
                return result

    def create_query_future(self, key):
        """
        Create and return a :class:`asyncio.Future` for the given `hash_`
        function and `node` URL. The future is referenced internally and used
        by any calls to :meth:`lookup` which are made while the future is
        pending. The future is removed from the internal storage automatically
        when a result or exception is set for it.

        This allows for deduplication of queries for the same hash.
        """
        fut = asyncio.Future()
        fut.add_done_callback(
            functools.partial(self._erase_future, key)
        )
        self._lookup_cache[key] = fut
        return fut

    def add_cache_entry(self, key, entry):
        """
        Add the given `entry` (which must be a :class:`~.disco.xso.InfoQuery`
        instance) to the user-level database keyed with the hash function type
        `hash_` and the `node` URL. The `entry` is **not** validated to
        actually map to `node` with the given `hash_` function, it is expected
        that the caller perfoms the validation.
        """
        copied_entry = copy.copy(entry)
        self._memory_overlay[key] = copied_entry
        if self._user_db_path is not None:
            asyncio.async(asyncio.get_event_loop().run_in_executor(
                None,
                writeback,
                self._user_db_path / key.path,
                entry.captured_events))


class EntityCapsService(aioxmpp.service.Service):
    """
    This service implements :xep:`0115`, transparently. Besides loading the
    service, no interaction is required to get some of the benefits of
    :xep:`0115`.

    Two additional things need to be done by users to get full support and
    performance:

    1. To make sure that peers are always up-to-date with the current
       capabilities, it is required that users listen on the
       :meth:`on_ver_changed` signal and re-emit their current presence when it
       fires.

       The service takes care of attaching capabilities information on the
       outgoing stanza, using a stanza filter.

    2. Users should use a process-wide :class:`Cache` instance and assign it to
       the :attr:`cache` of each :class:`.entitycaps.Service` they use. This
       improves performance by sharing (verified) hashes among :class:`Service`
       instances.

       In addition, the hashes should be saved and restored on shutdown/start
       of the process. See the :class:`Cache` for details.

    .. signal:: on_ver_changed

       The signal emits whenever the ``ver`` of the local client changes. This
       happens when the set of features or identities announced in the
       :class:`.DiscoServer` changes.

    .. autoattribute:: cache

    .. versionchanged:: 0.8

       This class was formerly known as :class:`aioxmpp.entitycaps.Service`. It
       is still available under that name, but the alias will be removed in
       1.0.

    """

    ORDER_AFTER = {
        disco.DiscoClient,
        disco.DiscoServer,
    }

    NODE = "http://aioxmpp.zombofant.net/"

    on_ver_changed = aioxmpp.callbacks.Signal()

    xep115_support = disco.register_feature(namespaces.xep0115_caps)

    def __init__(self, node, **kwargs):
        super().__init__(node, **kwargs)

        self.__current_keys = None
        self._cache = Cache()

        self.disco_server = self.dependencies[disco.DiscoServer]
        self.disco_client = self.dependencies[disco.DiscoClient]

        self.__115 = caps115.Implementation(self.NODE)

    @property
    def cache(self):
        """
        The :class:`Cache` instance used for this :class:`Service`. Deleting
        this attribute will automatically create a new :class:`Cache` instance.

        The attribute can be used to share a single :class:`Cache` among
        multiple :class:`Service` instances.
        """
        return self._cache

    @cache.setter
    def cache(self, v):
        self._cache = v

    @cache.deleter
    def cache(self):
        self._cache = Cache()

    @aioxmpp.service.depsignal(
        disco.DiscoServer,
        "on_info_changed")
    def _info_changed(self):
        self.logger.debug("info changed, scheduling re-calculation of version")
        asyncio.get_event_loop().call_soon(
            self.update_hash
        )

    @asyncio.coroutine
    def _shutdown(self):
        if self.__current_keys:
            for key in self.__current_keys:
                self.disco_server.unmount_node(key.node)

    @asyncio.coroutine
    def query_and_cache(self, jid, key, fut):
        data = yield from self.disco_client.query_info(
            jid,
            node=key.node,
            require_fresh=True)

        try:
            if key.verify(data):
                self.cache.add_cache_entry(key, data)
                fut.set_result(data)
            else:
                raise ValueError("hash mismatch")
        except ValueError as exc:
            fut.set_exception(exc)

        return data

    @asyncio.coroutine
    def lookup_info(self, jid, keys):
        for key in keys:
            try:
                info = yield from self.cache.lookup(key)
            except KeyError:
                continue

            self.logger.debug("found %s in cache", key)
            return info

        first_key = keys[0]
        self.logger.debug("using key %s to query peer", first_key)
        fut = self.cache.create_query_future(first_key)
        info = yield from self.query_and_cache(
            jid, first_key, fut
        )
        self.logger.debug("%s maps to %r", key, info)

        return info

    @aioxmpp.service.outbound_presence_filter
    def handle_outbound_presence(self, presence):
        if (presence.type_ == aioxmpp.structs.PresenceType.AVAILABLE and
                self.__current_keys):
            if self.xep115_support.enabled:
                self.logger.debug(
                    "injecting capabilities into outbound presence"
                )
                self.__115.put_keys(self.__current_keys, presence)

        return presence

    @aioxmpp.service.inbound_presence_filter
    def handle_inbound_presence(self, presence):
        if self.xep115_support.enabled:
            keys = list(self.__115.extract_keys(presence))
            lookup_task = asyncio.async(
                self.lookup_info(presence.from_, keys)
            )
            self.disco_client.set_info_future(
                presence.from_,
                None,
                lookup_task
            )

        return presence

    def update_hash(self):
        identities = []
        for category, type_, lang, name in self.disco_server.iter_identities():
            identity = disco.xso.Identity(category=category,
                                          type_=type_)
            if lang is not None:
                identity.lang = lang
            if name is not None:
                identity.name = name
            identities.append(identity)

        info = disco.xso.InfoQuery(
            identities=identities,
            features=self.disco_server.iter_features(),
        )

        current_keys = frozenset(self.__115.calculate_keys(info))

        self.logger.debug("new keys=%r", current_keys)

        if self.__current_keys != current_keys:
            if self.__current_keys:
                for key in self.__current_keys:
                    self.disco_server.unmount_node(key.node)
            self.__current_keys = current_keys
            for key in current_keys:
                self.disco_server.mount_node(key.node, self.disco_server)
            self.on_ver_changed()


def writeback(base_path, hash_, node, captured_events):
    quoted = urllib.parse.quote(node, safe="")
    dest_path = base_path / "{}_{}.xml".format(hash_, quoted)
    with tempfile.NamedTemporaryFile(dir=str(base_path), delete=False) as tmpf:
        try:
            generator = aioxmpp.xml.XMPPXMLGenerator(
                tmpf,
                short_empty_elements=True)
            generator.startDocument()
            aioxmpp.xso.events_to_sax(captured_events, generator)
            generator.endDocument()
        except:
            os.unlink(tmpf.name)
            raise
        os.replace(tmpf.name, str(dest_path))
