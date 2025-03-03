# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from typing import Any, Dict, List, Optional, Set, Union, cast

import frozendict

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, HistoryVisibility, Membership
from synapse.api.room_versions import RoomVersions
from synapse.appservice import ApplicationService
from synapse.events import FrozenEvent, make_event_from_dict
from synapse.push.bulk_push_rule_evaluator import _flatten_dict
from synapse.push.httppusher import tweaks_for_actions
from synapse.rest import admin
from synapse.rest.client import login, register, room
from synapse.server import HomeServer
from synapse.storage.databases.main.appservice import _make_exclusive_regex
from synapse.synapse_rust.push import PushRuleEvaluator
from synapse.types import JsonDict, JsonMapping, UserID
from synapse.util import Clock
from synapse.util.frozenutils import freeze

from tests import unittest
from tests.test_utils.event_injection import create_event, inject_member_event


class FlattenDictTestCase(unittest.TestCase):
    def test_simple(self) -> None:
        """Test a dictionary that isn't modified."""
        input = {"foo": "abc"}
        self.assertEqual(input, _flatten_dict(input))

    def test_nested(self) -> None:
        """Nested dictionaries become dotted paths."""
        input = {"foo": {"bar": "abc"}}
        self.assertEqual({"foo.bar": "abc"}, _flatten_dict(input))

        # If a field has a dot in it, escape it.
        input = {"m.foo": {"b\\ar": "abc"}}
        self.assertEqual({"m.foo.b\\ar": "abc"}, _flatten_dict(input))
        self.assertEqual(
            {"m\\.foo.b\\\\ar": "abc"},
            _flatten_dict(input, msc3873_escape_event_match_key=True),
        )

    def test_non_string(self) -> None:
        """String, booleans, ints, nulls and list of those should be kept while other items are dropped."""
        input: Dict[str, Any] = {
            "woo": "woo",
            "foo": True,
            "bar": 1,
            "baz": None,
            "fuzz": ["woo", True, 1, None, [], {}],
            "boo": {},
        }
        self.assertEqual(
            {
                "woo": "woo",
                "foo": True,
                "bar": 1,
                "baz": None,
                "fuzz": ["woo", True, 1, None],
            },
            _flatten_dict(input),
        )

    def test_event(self) -> None:
        """Events can also be flattened."""
        event = make_event_from_dict(
            {
                "room_id": "!test:test",
                "type": "m.room.message",
                "sender": "@alice:test",
                "content": {
                    "msgtype": "m.text",
                    "body": "Hello world!",
                    "format": "org.matrix.custom.html",
                    "formatted_body": "<h1>Hello world!</h1>",
                },
            },
            room_version=RoomVersions.V8,
        )
        expected = {
            "content.msgtype": "m.text",
            "content.body": "Hello world!",
            "content.format": "org.matrix.custom.html",
            "content.formatted_body": "<h1>Hello world!</h1>",
            "room_id": "!test:test",
            "sender": "@alice:test",
            "type": "m.room.message",
        }
        self.assertEqual(expected, _flatten_dict(event))

    def test_extensible_events(self) -> None:
        """Extensible events has compatibility behaviour."""
        event_dict = {
            "room_id": "!test:test",
            "type": "m.room.message",
            "sender": "@alice:test",
            "content": {
                "org.matrix.msc1767.markup": [
                    {"mimetype": "text/plain", "body": "Hello world!"},
                    {"mimetype": "text/html", "body": "<h1>Hello world!</h1>"},
                ]
            },
        }

        # For a current room version, there's no special behavior.
        event = make_event_from_dict(event_dict, room_version=RoomVersions.V8)
        expected = {
            "room_id": "!test:test",
            "sender": "@alice:test",
            "type": "m.room.message",
            "content.org.matrix.msc1767.markup": [],
        }
        self.assertEqual(expected, _flatten_dict(event))

        # For a room version with extensible events, they parse out the text/plain
        # to a content.body property.
        event = make_event_from_dict(event_dict, room_version=RoomVersions.MSC1767v10)
        expected = {
            "content.body": "hello world!",
            "room_id": "!test:test",
            "sender": "@alice:test",
            "type": "m.room.message",
            "content.org.matrix.msc1767.markup": [],
        }
        self.assertEqual(expected, _flatten_dict(event))


class PushRuleEvaluatorTestCase(unittest.TestCase):
    def _get_evaluator(
        self,
        content: JsonMapping,
        *,
        has_mentions: bool = False,
        user_mentions: Optional[Set[str]] = None,
        related_events: Optional[JsonDict] = None,
    ) -> PushRuleEvaluator:
        event = FrozenEvent(
            {
                "event_id": "$event_id",
                "type": "m.room.history_visibility",
                "sender": "@user:test",
                "state_key": "",
                "room_id": "#room:test",
                "content": content,
            },
            RoomVersions.V1,
        )
        room_member_count = 0
        sender_power_level = 0
        power_levels: Dict[str, Union[int, Dict[str, int]]] = {}
        return PushRuleEvaluator(
            _flatten_dict(event),
            has_mentions,
            user_mentions or set(),
            room_member_count,
            sender_power_level,
            cast(Dict[str, int], power_levels.get("notifications", {})),
            {} if related_events is None else related_events,
            related_event_match_enabled=True,
            room_version_feature_flags=event.room_version.msc3931_push_features,
            msc3931_enabled=True,
            msc3758_exact_event_match=True,
            msc3966_exact_event_property_contains=True,
        )

    def test_display_name(self) -> None:
        """Check for a matching display name in the body of the event."""
        evaluator = self._get_evaluator({"body": "foo bar baz"})

        condition = {"kind": "contains_display_name"}

        # Blank names are skipped.
        self.assertFalse(evaluator.matches(condition, "@user:test", ""))

        # Check a display name that doesn't match.
        self.assertFalse(evaluator.matches(condition, "@user:test", "not found"))

        # Check a display name which matches.
        self.assertTrue(evaluator.matches(condition, "@user:test", "foo"))

        # A display name that matches, but not a full word does not result in a match.
        self.assertFalse(evaluator.matches(condition, "@user:test", "ba"))

        # A display name should not be interpreted as a regular expression.
        self.assertFalse(evaluator.matches(condition, "@user:test", "ba[rz]"))

        # A display name with spaces should work fine.
        self.assertTrue(evaluator.matches(condition, "@user:test", "foo bar"))

    def test_user_mentions(self) -> None:
        """Check for user mentions."""
        condition = {"kind": "org.matrix.msc3952.is_user_mention"}

        # No mentions shouldn't match.
        evaluator = self._get_evaluator({}, has_mentions=True)
        self.assertFalse(evaluator.matches(condition, "@user:test", None))

        # An empty set shouldn't match
        evaluator = self._get_evaluator({}, has_mentions=True, user_mentions=set())
        self.assertFalse(evaluator.matches(condition, "@user:test", None))

        # The Matrix ID appearing anywhere in the mentions list should match
        evaluator = self._get_evaluator(
            {}, has_mentions=True, user_mentions={"@user:test"}
        )
        self.assertTrue(evaluator.matches(condition, "@user:test", None))

        evaluator = self._get_evaluator(
            {}, has_mentions=True, user_mentions={"@another:test", "@user:test"}
        )
        self.assertTrue(evaluator.matches(condition, "@user:test", None))

        # Note that invalid data is tested at tests.push.test_bulk_push_rule_evaluator.TestBulkPushRuleEvaluator.test_mentions
        # since the BulkPushRuleEvaluator is what handles data sanitisation.

    def _assert_matches(
        self, condition: JsonDict, content: JsonMapping, msg: Optional[str] = None
    ) -> None:
        evaluator = self._get_evaluator(content)
        self.assertTrue(evaluator.matches(condition, "@user:test", "display_name"), msg)

    def _assert_not_matches(
        self, condition: JsonDict, content: JsonDict, msg: Optional[str] = None
    ) -> None:
        evaluator = self._get_evaluator(content)
        self.assertFalse(
            evaluator.matches(condition, "@user:test", "display_name"), msg
        )

    def test_event_match_body(self) -> None:
        """Check that event_match conditions on content.body work as expected"""

        # if the key is `content.body`, the pattern matches substrings.

        # non-wildcards should match
        condition = {
            "kind": "event_match",
            "key": "content.body",
            "pattern": "foobaz",
        }
        self._assert_matches(
            condition,
            {"body": "aaa FoobaZ zzz"},
            "patterns should match and be case-insensitive",
        )
        self._assert_not_matches(
            condition,
            {"body": "aa xFoobaZ yy"},
            "pattern should only match at word boundaries",
        )
        self._assert_not_matches(
            condition,
            {"body": "aa foobazx yy"},
            "pattern should only match at word boundaries",
        )

        # wildcards should match
        condition = {
            "kind": "event_match",
            "key": "content.body",
            "pattern": "f?o*baz",
        }

        self._assert_matches(
            condition,
            {"body": "aaa FoobarbaZ zzz"},
            "* should match string and pattern should be case-insensitive",
        )
        self._assert_matches(
            condition, {"body": "aa foobaz yy"}, "* should match 0 characters"
        )
        self._assert_not_matches(
            condition, {"body": "aa fobbaz yy"}, "? should not match 0 characters"
        )
        self._assert_not_matches(
            condition, {"body": "aa fiiobaz yy"}, "? should not match 2 characters"
        )
        self._assert_not_matches(
            condition,
            {"body": "aa xfooxbaz yy"},
            "pattern should only match at word boundaries",
        )
        self._assert_not_matches(
            condition,
            {"body": "aa fooxbazx yy"},
            "pattern should only match at word boundaries",
        )

        # test backslashes
        condition = {
            "kind": "event_match",
            "key": "content.body",
            "pattern": r"f\oobaz",
        }
        self._assert_matches(
            condition,
            {"body": r"F\oobaz"},
            "backslash should match itself",
        )
        condition = {
            "kind": "event_match",
            "key": "content.body",
            "pattern": r"f\?obaz",
        }
        self._assert_matches(
            condition,
            {"body": r"F\oobaz"},
            r"? after \ should match any character",
        )

    def test_event_match_non_body(self) -> None:
        """Check that event_match conditions on other keys work as expected"""

        # if the key is anything other than 'content.body', the pattern must match the
        # whole value.

        # non-wildcards should match
        condition = {
            "kind": "event_match",
            "key": "content.value",
            "pattern": "foobaz",
        }
        self._assert_matches(
            condition,
            {"value": "FoobaZ"},
            "patterns should match and be case-insensitive",
        )
        self._assert_not_matches(
            condition,
            {"value": "xFoobaZ"},
            "pattern should only match at the start/end of the value",
        )
        self._assert_not_matches(
            condition,
            {"value": "FoobaZz"},
            "pattern should only match at the start/end of the value",
        )

        # it should work on frozendicts too
        self._assert_matches(
            condition,
            frozendict.frozendict({"value": "FoobaZ"}),
            "patterns should match on frozendicts",
        )

        # wildcards should match
        condition = {
            "kind": "event_match",
            "key": "content.value",
            "pattern": "f?o*baz",
        }
        self._assert_matches(
            condition,
            {"value": "FoobarbaZ"},
            "* should match string and pattern should be case-insensitive",
        )
        self._assert_matches(
            condition, {"value": "foobaz"}, "* should match 0 characters"
        )
        self._assert_not_matches(
            condition, {"value": "fobbaz"}, "? should not match 0 characters"
        )
        self._assert_not_matches(
            condition, {"value": "fiiobaz"}, "? should not match 2 characters"
        )
        self._assert_not_matches(
            condition,
            {"value": "xfooxbaz"},
            "pattern should only match at the start/end of the value",
        )
        self._assert_not_matches(
            condition,
            {"value": "fooxbazx"},
            "pattern should only match at the start/end of the value",
        )
        self._assert_not_matches(
            condition,
            {"value": "x\nfooxbaz"},
            "pattern should not match after a newline",
        )
        self._assert_not_matches(
            condition,
            {"value": "fooxbaz\nx"},
            "pattern should not match before a newline",
        )

    def test_exact_event_match_string(self) -> None:
        """Check that exact_event_match conditions work as expected for strings."""

        # Test against a string value.
        condition = {
            "kind": "com.beeper.msc3758.exact_event_match",
            "key": "content.value",
            "value": "foobaz",
        }
        self._assert_matches(
            condition,
            {"value": "foobaz"},
            "exact value should match",
        )
        self._assert_not_matches(
            condition,
            {"value": "FoobaZ"},
            "values should match and be case-sensitive",
        )
        self._assert_not_matches(
            condition,
            {"value": "test foobaz test"},
            "values must exactly match",
        )
        value: Any
        for value in (True, False, 1, 1.1, None, [], {}):
            self._assert_not_matches(
                condition,
                {"value": value},
                "incorrect types should not match",
            )

        # it should work on frozendicts too
        self._assert_matches(
            condition,
            frozendict.frozendict({"value": "foobaz"}),
            "values should match on frozendicts",
        )

    def test_exact_event_match_boolean(self) -> None:
        """Check that exact_event_match conditions work as expected for booleans."""

        # Test against a True boolean value.
        condition = {
            "kind": "com.beeper.msc3758.exact_event_match",
            "key": "content.value",
            "value": True,
        }
        self._assert_matches(
            condition,
            {"value": True},
            "exact value should match",
        )
        self._assert_not_matches(
            condition,
            {"value": False},
            "incorrect values should not match",
        )
        for value in ("foobaz", 1, 1.1, None, [], {}):
            self._assert_not_matches(
                condition,
                {"value": value},
                "incorrect types should not match",
            )

        # Test against a False boolean value.
        condition = {
            "kind": "com.beeper.msc3758.exact_event_match",
            "key": "content.value",
            "value": False,
        }
        self._assert_matches(
            condition,
            {"value": False},
            "exact value should match",
        )
        self._assert_not_matches(
            condition,
            {"value": True},
            "incorrect values should not match",
        )
        # Choose false-y values to ensure there's no type coercion.
        for value in ("", 0, 1.1, None, [], {}):
            self._assert_not_matches(
                condition,
                {"value": value},
                "incorrect types should not match",
            )

    def test_exact_event_match_null(self) -> None:
        """Check that exact_event_match conditions work as expected for null."""

        condition = {
            "kind": "com.beeper.msc3758.exact_event_match",
            "key": "content.value",
            "value": None,
        }
        self._assert_matches(
            condition,
            {"value": None},
            "exact value should match",
        )
        for value in ("foobaz", True, False, 1, 1.1, [], {}):
            self._assert_not_matches(
                condition,
                {"value": value},
                "incorrect types should not match",
            )

    def test_exact_event_match_integer(self) -> None:
        """Check that exact_event_match conditions work as expected for integers."""

        condition = {
            "kind": "com.beeper.msc3758.exact_event_match",
            "key": "content.value",
            "value": 1,
        }
        self._assert_matches(
            condition,
            {"value": 1},
            "exact value should match",
        )
        value: Any
        for value in (1.1, -1, 0):
            self._assert_not_matches(
                condition,
                {"value": value},
                "incorrect values should not match",
            )
        for value in ("1", True, False, None, [], {}):
            self._assert_not_matches(
                condition,
                {"value": value},
                "incorrect types should not match",
            )

    def test_exact_event_property_contains(self) -> None:
        """Check that exact_event_property_contains conditions work as expected."""

        condition = {
            "kind": "org.matrix.msc3966.exact_event_property_contains",
            "key": "content.value",
            "value": "foobaz",
        }
        self._assert_matches(
            condition,
            {"value": ["foobaz"]},
            "exact value should match",
        )
        self._assert_matches(
            condition,
            {"value": ["foobaz", "bugz"]},
            "extra values should match",
        )
        self._assert_not_matches(
            condition,
            {"value": ["FoobaZ"]},
            "values should match and be case-sensitive",
        )
        self._assert_not_matches(
            condition,
            {"value": "foobaz"},
            "does not search in a string",
        )

        # it should work on frozendicts too
        self._assert_matches(
            condition,
            freeze({"value": ["foobaz"]}),
            "values should match on frozendicts",
        )

    def test_no_body(self) -> None:
        """Not having a body shouldn't break the evaluator."""
        evaluator = self._get_evaluator({})

        condition = {
            "kind": "contains_display_name",
        }
        self.assertFalse(evaluator.matches(condition, "@user:test", "foo"))

    def test_invalid_body(self) -> None:
        """A non-string body should not break the evaluator."""
        condition = {
            "kind": "contains_display_name",
        }

        for body in (1, True, {"foo": "bar"}):
            evaluator = self._get_evaluator({"body": body})
            self.assertFalse(evaluator.matches(condition, "@user:test", "foo"))

    def test_tweaks_for_actions(self) -> None:
        """
        This tests the behaviour of tweaks_for_actions.
        """

        actions: List[Union[Dict[str, str], str]] = [
            {"set_tweak": "sound", "value": "default"},
            {"set_tweak": "highlight"},
            "notify",
        ]

        self.assertEqual(
            tweaks_for_actions(actions),
            {"sound": "default", "highlight": True},
        )

    def test_related_event_match(self) -> None:
        evaluator = self._get_evaluator(
            {
                "m.relates_to": {
                    "event_id": "$parent_event_id",
                    "key": "😀",
                    "rel_type": "m.annotation",
                    "m.in_reply_to": {
                        "event_id": "$parent_event_id",
                    },
                }
            },
            related_events={
                "m.in_reply_to": {
                    "event_id": "$parent_event_id",
                    "type": "m.room.message",
                    "sender": "@other_user:test",
                    "room_id": "!room:test",
                    "content.msgtype": "m.text",
                    "content.body": "Original message",
                },
                "m.annotation": {
                    "event_id": "$parent_event_id",
                    "type": "m.room.message",
                    "sender": "@other_user:test",
                    "room_id": "!room:test",
                    "content.msgtype": "m.text",
                    "content.body": "Original message",
                },
            },
        )
        self.assertTrue(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                    "pattern": "@other_user:test",
                },
                "@user:test",
                "display_name",
            )
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                    "pattern": "@user:test",
                },
                "@other_user:test",
                "display_name",
            )
        )
        self.assertTrue(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.annotation",
                    "pattern": "@other_user:test",
                },
                "@other_user:test",
                "display_name",
            )
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                },
                "@user:test",
                "display_name",
            )
        )
        self.assertTrue(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "rel_type": "m.in_reply_to",
                },
                "@user:test",
                "display_name",
            )
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "rel_type": "m.replace",
                },
                "@other_user:test",
                "display_name",
            )
        )

    def test_related_event_match_with_fallback(self) -> None:
        evaluator = self._get_evaluator(
            {
                "m.relates_to": {
                    "event_id": "$parent_event_id",
                    "key": "😀",
                    "rel_type": "m.thread",
                    "is_falling_back": True,
                    "m.in_reply_to": {
                        "event_id": "$parent_event_id",
                    },
                }
            },
            related_events={
                "m.in_reply_to": {
                    "event_id": "$parent_event_id",
                    "type": "m.room.message",
                    "sender": "@other_user:test",
                    "room_id": "!room:test",
                    "content.msgtype": "m.text",
                    "content.body": "Original message",
                    "im.vector.is_falling_back": "",
                },
                "m.thread": {
                    "event_id": "$parent_event_id",
                    "type": "m.room.message",
                    "sender": "@other_user:test",
                    "room_id": "!room:test",
                    "content.msgtype": "m.text",
                    "content.body": "Original message",
                },
            },
        )
        self.assertTrue(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                    "pattern": "@other_user:test",
                    "include_fallbacks": True,
                },
                "@user:test",
                "display_name",
            )
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                    "pattern": "@other_user:test",
                    "include_fallbacks": False,
                },
                "@user:test",
                "display_name",
            )
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                    "pattern": "@other_user:test",
                },
                "@user:test",
                "display_name",
            )
        )

    def test_related_event_match_no_related_event(self) -> None:
        evaluator = self._get_evaluator(
            {"msgtype": "m.text", "body": "Message without related event"}
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                    "pattern": "@other_user:test",
                },
                "@user:test",
                "display_name",
            )
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "key": "sender",
                    "rel_type": "m.in_reply_to",
                },
                "@user:test",
                "display_name",
            )
        )
        self.assertFalse(
            evaluator.matches(
                {
                    "kind": "im.nheko.msc3664.related_event_match",
                    "rel_type": "m.in_reply_to",
                },
                "@user:test",
                "display_name",
            )
        )


class TestBulkPushRuleEvaluator(unittest.HomeserverTestCase):
    """Tests for the bulk push rule evaluator"""

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        register.register_servlets,
        room.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Define an application service so that we can register appservice users
        self._service_token = "some_token"
        self._service = ApplicationService(
            self._service_token,
            "as1",
            "@as.sender:test",
            namespaces={
                "users": [
                    {"regex": "@_as_.*:test", "exclusive": True},
                    {"regex": "@as.sender:test", "exclusive": True},
                ]
            },
            msc3202_transaction_extensions=True,
        )
        self.hs.get_datastores().main.services_cache = [self._service]
        self.hs.get_datastores().main.exclusive_user_regex = _make_exclusive_regex(
            [self._service]
        )

        self._as_user, _ = self.register_appservice_user(
            "_as_user", self._service_token
        )

        self.evaluator = self.hs.get_bulk_push_rule_evaluator()

    def test_ignore_appservice_users(self) -> None:
        "Test that we don't generate push for appservice users"

        user_id = self.register_user("user", "pass")
        token = self.login("user", "pass")

        room_id = self.helper.create_room_as(user_id, tok=token)
        self.get_success(
            inject_member_event(self.hs, room_id, self._as_user, Membership.JOIN)
        )

        event, context = self.get_success(
            create_event(
                self.hs,
                type=EventTypes.Message,
                room_id=room_id,
                sender=user_id,
                content={"body": "test", "msgtype": "m.text"},
            )
        )

        # Assert the returned push rules do not contain the app service user
        rules = self.get_success(self.evaluator._get_rules_for_event(event))
        self.assertTrue(self._as_user not in rules)

        # Assert that no push actions have been added to the staging table (the
        # sender should not be pushed for the event)
        users_with_push_actions = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_onecol(
                table="event_push_actions_staging",
                keyvalues={"event_id": event.event_id},
                retcol="user_id",
                desc="test_ignore_appservice_users",
            )
        )

        self.assertEqual(len(users_with_push_actions), 0)


class BulkPushRuleEvaluatorTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.main_store = homeserver.get_datastores().main

        self.user_id1 = self.register_user("user1", "password")
        self.tok1 = self.login(self.user_id1, "password")
        self.user_id2 = self.register_user("user2", "password")
        self.tok2 = self.login(self.user_id2, "password")

        self.room_id = self.helper.create_room_as(tok=self.tok1)

        # We want to test history visibility works correctly.
        self.helper.send_state(
            self.room_id,
            EventTypes.RoomHistoryVisibility,
            {"history_visibility": HistoryVisibility.JOINED},
            tok=self.tok1,
        )

    def get_notif_count(self, user_id: str) -> int:
        return self.get_success(
            self.main_store.db_pool.simple_select_one_onecol(
                table="event_push_actions",
                keyvalues={"user_id": user_id},
                retcol="COALESCE(SUM(notif), 0)",
                desc="get_staging_notif_count",
            )
        )

    def test_plain_message(self) -> None:
        """Test that sending a normal message in a room will trigger a
        notification
        """

        # Have user2 join the room and cle
        self.helper.join(self.room_id, self.user_id2, tok=self.tok2)

        # They start off with no notifications, but get them when messages are
        # sent.
        self.assertEqual(self.get_notif_count(self.user_id2), 0)

        user1 = UserID.from_string(self.user_id1)
        self.create_and_send_event(self.room_id, user1)

        self.assertEqual(self.get_notif_count(self.user_id2), 1)

    def test_delayed_message(self) -> None:
        """Test that a delayed message that was from before a user joined
        doesn't cause a notification for the joined user.
        """
        user1 = UserID.from_string(self.user_id1)

        # Send a message before user2 joins
        event_id1 = self.create_and_send_event(self.room_id, user1)

        # Have user2 join the room
        self.helper.join(self.room_id, self.user_id2, tok=self.tok2)

        # They start off with no notifications
        self.assertEqual(self.get_notif_count(self.user_id2), 0)

        # Send another message that references the event before the join to
        # simulate a "delayed" event
        self.create_and_send_event(self.room_id, user1, prev_event_ids=[event_id1])

        # user2 should not be notified about it, because they can't see it.
        self.assertEqual(self.get_notif_count(self.user_id2), 0)
