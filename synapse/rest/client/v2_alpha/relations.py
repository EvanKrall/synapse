# -*- coding: utf-8 -*-
# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This class implements the proposed relation APIs from MSC 1849.

Since the MSC has not been approved all APIs here are unstable and may change at
any time to reflect changes in the MSC.
"""

import logging

from twisted.internet import defer

from synapse.api.constants import EventTypes, RelationTypes
from synapse.api.errors import SynapseError
from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.rest.client.transactions import HttpTransactionCache
from synapse.storage.relations import AggregationPaginationToken, RelationPaginationToken

from ._base import client_patterns

logger = logging.getLogger(__name__)


class RelationSendServlet(RestServlet):
    """Helper API for sending events that have relation data.

    Example API shape to send a 👍 reaction to a room:

        POST /rooms/!foo/send_relation/$bar/m.annotation/m.reaction?key=%F0%9F%91%8D
        {}

        {
            "event_id": "$foobar"
        }
    """

    PATTERN = (
        "/rooms/(?P<room_id>[^/]*)/send_relation"
        "/(?P<parent_id>[^/]*)/(?P<relation_type>[^/]*)/(?P<event_type>[^/]*)"
    )

    def __init__(self, hs):
        super(RelationSendServlet, self).__init__()
        self.auth = hs.get_auth()
        self.event_creation_handler = hs.get_event_creation_handler()
        self.txns = HttpTransactionCache(hs)

    def register(self, http_server):
        http_server.register_paths(
            "POST",
            client_patterns(self.PATTERN + "$", releases=()),
            self.on_PUT_or_POST,
        )
        http_server.register_paths(
            "PUT",
            client_patterns(self.PATTERN + "/(?P<txn_id>[^/]*)$", releases=()),
            self.on_PUT,
        )

    def on_PUT(self, request, *args, **kwargs):
        return self.txns.fetch_or_execute_request(
            request, self.on_PUT_or_POST, request, *args, **kwargs
        )

    @defer.inlineCallbacks
    def on_PUT_or_POST(
        self, request, room_id, parent_id, relation_type, event_type, txn_id=None
    ):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)

        if event_type == EventTypes.Member:
            # Add relations to a membership is meaningless, so we just deny it
            # at the CS API rather than trying to handle it correctly.
            raise SynapseError(400, "Cannot send member events with relations")

        content = parse_json_object_from_request(request)

        aggregation_key = parse_string(request, "key", encoding="utf-8")

        content["m.relates_to"] = {
            "event_id": parent_id,
            "key": aggregation_key,
            "rel_type": relation_type,
        }

        event_dict = {
            "type": event_type,
            "content": content,
            "room_id": room_id,
            "sender": requester.user.to_string(),
        }

        event = yield self.event_creation_handler.create_and_send_nonmember_event(
            requester, event_dict=event_dict, txn_id=txn_id
        )

        defer.returnValue((200, {"event_id": event.event_id}))


class RelationPaginationServlet(RestServlet):
    """API to paginate relations on an event by topological ordering, optionally
    filtered by relation type and event type.
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/relations/(?P<parent_id>[^/]*)"
        "(/(?P<relation_type>[^/]*)(/(?P<event_type>[^/]*))?)?$",
        releases=(),
    )

    def __init__(self, hs):
        super(RelationPaginationServlet, self).__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        self._event_serializer = hs.get_event_client_serializer()
        self.event_handler = hs.get_event_handler()

    @defer.inlineCallbacks
    def on_GET(self, request, room_id, parent_id, relation_type=None, event_type=None):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)

        yield self.auth.check_in_room_or_world_readable(
            room_id, requester.user.to_string()
        )

        # This checks that a) the event exists and b) the user is allowed to
        # view it.
        yield self.event_handler.get_event(requester.user, room_id, parent_id)

        limit = parse_integer(request, "limit", default=5)
        from_token = parse_string(request, "from")
        to_token = parse_string(request, "to")

        if from_token:
            from_token = RelationPaginationToken.from_string(from_token)

        if to_token:
            to_token = RelationPaginationToken.from_string(to_token)

        result = yield self.store.get_relations_for_event(
            event_id=parent_id,
            relation_type=relation_type,
            event_type=event_type,
            limit=limit,
            from_token=from_token,
            to_token=to_token,
        )

        events = yield self.store.get_events_as_list(
            [c["event_id"] for c in result.chunk]
        )

        now = self.clock.time_msec()
        events = yield self._event_serializer.serialize_events(events, now)

        return_value = result.to_dict()
        return_value["chunk"] = events

        defer.returnValue((200, return_value))


class RelationAggregationPaginationServlet(RestServlet):
    """API to paginate aggregation groups of relations, e.g. paginate the
    types and counts of the reactions on the events.

    Example request and response:

        GET /rooms/{room_id}/aggregations/{parent_id}

        {
            chunk: [
                {
                    "type": "m.reaction",
                    "key": "👍",
                    "count": 3
                }
            ]
        }
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/aggregations/(?P<parent_id>[^/]*)"
        "(/(?P<relation_type>[^/]*)(/(?P<event_type>[^/]*))?)?$",
        releases=(),
    )

    def __init__(self, hs):
        super(RelationAggregationPaginationServlet, self).__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()
        self.event_handler = hs.get_event_handler()

    @defer.inlineCallbacks
    def on_GET(self, request, room_id, parent_id, relation_type=None, event_type=None):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)

        yield self.auth.check_in_room_or_world_readable(
            room_id, requester.user.to_string()
        )

        # This checks that a) the event exists and b) the user is allowed to
        # view it.
        yield self.event_handler.get_event(requester.user, room_id, parent_id)

        if relation_type not in (RelationTypes.ANNOTATION, None):
            raise SynapseError(400, "Relation type must be 'annotation'")

        limit = parse_integer(request, "limit", default=5)
        from_token = parse_string(request, "from")
        to_token = parse_string(request, "to")

        if from_token:
            from_token = AggregationPaginationToken.from_string(from_token)

        if to_token:
            to_token = AggregationPaginationToken.from_string(to_token)

        res = yield self.store.get_aggregation_groups_for_event(
            event_id=parent_id,
            event_type=event_type,
            limit=limit,
            from_token=from_token,
            to_token=to_token,
        )

        defer.returnValue((200, res.to_dict()))


class RelationAggregationGroupPaginationServlet(RestServlet):
    """API to paginate within an aggregation group of relations, e.g. paginate
    all the 👍 reactions on an event.

    Example request and response:

        GET /rooms/{room_id}/aggregations/{parent_id}/m.annotation/m.reaction/👍

        {
            chunk: [
                {
                    "type": "m.reaction",
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.annotation",
                            "key": "👍"
                        }
                    }
                },
                ...
            ]
        }
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/aggregations/(?P<parent_id>[^/]*)"
        "/(?P<relation_type>[^/]*)/(?P<event_type>[^/]*)/(?P<key>[^/]*)$",
        releases=(),
    )

    def __init__(self, hs):
        super(RelationAggregationGroupPaginationServlet, self).__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        self._event_serializer = hs.get_event_client_serializer()
        self.event_handler = hs.get_event_handler()

    @defer.inlineCallbacks
    def on_GET(self, request, room_id, parent_id, relation_type, event_type, key):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)

        yield self.auth.check_in_room_or_world_readable(
            room_id, requester.user.to_string()
        )

        # This checks that a) the event exists and b) the user is allowed to
        # view it.
        yield self.event_handler.get_event(requester.user, room_id, parent_id)

        if relation_type != RelationTypes.ANNOTATION:
            raise SynapseError(400, "Relation type must be 'annotation'")

        limit = parse_integer(request, "limit", default=5)
        from_token = parse_string(request, "from")
        to_token = parse_string(request, "to")

        if from_token:
            from_token = RelationPaginationToken.from_string(from_token)

        if to_token:
            to_token = RelationPaginationToken.from_string(to_token)

        result = yield self.store.get_relations_for_event(
            event_id=parent_id,
            relation_type=relation_type,
            event_type=event_type,
            aggregation_key=key,
            limit=limit,
            from_token=from_token,
            to_token=to_token,
        )

        events = yield self.store.get_events_as_list(
            [c["event_id"] for c in result.chunk]
        )

        now = self.clock.time_msec()
        events = yield self._event_serializer.serialize_events(events, now)

        return_value = result.to_dict()
        return_value["chunk"] = events

        defer.returnValue((200, return_value))


def register_servlets(hs, http_server):
    RelationSendServlet(hs).register(http_server)
    RelationPaginationServlet(hs).register(http_server)
    RelationAggregationPaginationServlet(hs).register(http_server)
    RelationAggregationGroupPaginationServlet(hs).register(http_server)
