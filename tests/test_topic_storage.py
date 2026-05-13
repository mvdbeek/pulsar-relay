"""Unit tests for InMemoryTopicStorage access-control logic.

Topics are namespaced by ``(owner_id, topic_name)`` since Phase 3c
(closes API H#5). Every storage method that takes ``topic_name`` also
takes ``owner_id``; tests below pass both explicitly.
"""

import pytest

from pulsar_relay.auth.models import TopicCreate
from pulsar_relay.auth.topic_storage import InMemoryTopicStorage

OWNER_ID = "user-owner"
OTHER_ID = "user-other"
GRANTED_ID = "user-granted"
ADMIN_ID = "user-admin"


@pytest.fixture
def storage():
    return InMemoryTopicStorage()


class TestUserCanAccess:
    """Direct tests for InMemoryTopicStorage.user_can_access."""

    @pytest.mark.anyio
    async def test_admin_bypasses_all_checks(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t"))
        assert await storage.user_can_access(
            owner_id=OWNER_ID, topic_name="t", user_id=ADMIN_ID, permission_type="read", user_permissions=["admin"]
        )
        assert await storage.user_can_access(
            owner_id=OWNER_ID, topic_name="t", user_id=ADMIN_ID, permission_type="write", user_permissions=["admin"]
        )

    @pytest.mark.anyio
    async def test_owner_can_read_and_write(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t"))
        assert await storage.user_can_access(
            owner_id=OWNER_ID,
            topic_name="t",
            user_id=OWNER_ID,
            permission_type="read",
            user_permissions=["read", "write"],
        )
        assert await storage.user_can_access(
            owner_id=OWNER_ID,
            topic_name="t",
            user_id=OWNER_ID,
            permission_type="write",
            user_permissions=["read", "write"],
        )

    @pytest.mark.anyio
    async def test_other_user_denied_on_private_topic(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t"))
        assert not await storage.user_can_access(
            owner_id=OWNER_ID,
            topic_name="t",
            user_id=OTHER_ID,
            permission_type="read",
            user_permissions=["read", "write"],
        )
        assert not await storage.user_can_access(
            owner_id=OWNER_ID,
            topic_name="t",
            user_id=OTHER_ID,
            permission_type="write",
            user_permissions=["read", "write"],
        )

    @pytest.mark.anyio
    async def test_non_owner_always_denied(self, storage):
        """Phase 4 dropped the shared-access feature. Non-owners are
        always denied regardless of permission type; admin override
        still works (covered by ``test_admin_bypasses_all_checks``)."""
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t"))
        for perm in ("read", "write"):
            assert not await storage.user_can_access(
                owner_id=OWNER_ID,
                topic_name="t",
                user_id=OTHER_ID,
                permission_type=perm,
                user_permissions=["read", "write"],
            )

    @pytest.mark.anyio
    async def test_nonexistent_topic_allows_access(self, storage):
        """Non-existent ``(owner, name)`` returns True so it can be
        auto-created on first write by the bearer."""
        assert await storage.user_can_access(
            owner_id=OTHER_ID,
            topic_name="missing",
            user_id=OTHER_ID,
            permission_type="write",
            user_permissions=["write"],
        )
        assert await storage.user_can_access(
            owner_id=OTHER_ID, topic_name="missing", user_id=OTHER_ID, permission_type="read", user_permissions=["read"]
        )


class TestListUserTopicsFiltering:
    """Verify list_user_topics returns only the bearer's owned topics.

    After Phase 4 dropped cross-user shared access, list_user_topics
    is equivalent to list_owned_topics — kept as a method on the
    interface for back-compat with callers that haven't migrated.
    """

    @pytest.mark.anyio
    async def test_excludes_other_users_topic(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="theirs"))
        await storage.create_topic(OTHER_ID, TopicCreate(topic_name="mine"))
        topics = await storage.list_user_topics(OTHER_ID)
        names = {t.topic_name for t in topics}
        assert names == {"mine"}


class TestNamespacingClosesSquat:
    """Direct verification that two users can have the same topic name
    without colliding — the core API H#5 invariant."""

    @pytest.mark.anyio
    async def test_two_users_can_create_same_topic_name(self, storage):
        alice = await storage.create_topic("alice", TopicCreate(topic_name="jobs"))
        bob = await storage.create_topic("bob", TopicCreate(topic_name="jobs"))
        assert alice.owner_id == "alice"
        assert bob.owner_id == "bob"
        assert alice.topic_id != bob.topic_id

    @pytest.mark.anyio
    async def test_creating_same_name_for_same_owner_raises(self, storage):
        await storage.create_topic("alice", TopicCreate(topic_name="jobs"))
        with pytest.raises(ValueError, match="already exists"):
            await storage.create_topic("alice", TopicCreate(topic_name="jobs"))

    @pytest.mark.anyio
    async def test_get_topic_is_owner_scoped(self, storage):
        await storage.create_topic("alice", TopicCreate(topic_name="jobs"))
        # Bob looking up "jobs" sees nothing — Alice's jobs is not his.
        assert await storage.get_topic("bob", "jobs") is None
        assert (await storage.get_topic("alice", "jobs")).owner_id == "alice"
