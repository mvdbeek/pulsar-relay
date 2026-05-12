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
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t", is_public=False))
        assert await storage.user_can_access(
            owner_id=OWNER_ID, topic_name="t", user_id=ADMIN_ID, permission_type="read", user_permissions=["admin"]
        )
        assert await storage.user_can_access(
            owner_id=OWNER_ID, topic_name="t", user_id=ADMIN_ID, permission_type="write", user_permissions=["admin"]
        )

    @pytest.mark.anyio
    async def test_owner_can_read_and_write(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t", is_public=False))
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
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t", is_public=False))
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
    async def test_granted_user_can_read_but_not_write(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t", is_public=False))
        await storage.grant_access(OWNER_ID, "t", GRANTED_ID)
        assert await storage.user_can_access(
            owner_id=OWNER_ID,
            topic_name="t",
            user_id=GRANTED_ID,
            permission_type="read",
            user_permissions=["read", "write"],
        )
        # Grants are read-only; writes remain owner-only.
        assert not await storage.user_can_access(
            owner_id=OWNER_ID,
            topic_name="t",
            user_id=GRANTED_ID,
            permission_type="write",
            user_permissions=["read", "write"],
        )

    @pytest.mark.anyio
    async def test_public_topic_allows_read_for_anyone(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t", is_public=True))
        assert await storage.user_can_access(
            owner_id=OWNER_ID, topic_name="t", user_id=OTHER_ID, permission_type="read", user_permissions=["read"]
        )

    @pytest.mark.anyio
    async def test_public_topic_denies_write_for_non_owner(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t", is_public=True))
        assert not await storage.user_can_access(
            owner_id=OWNER_ID,
            topic_name="t",
            user_id=OTHER_ID,
            permission_type="write",
            user_permissions=["read", "write"],
        )

    @pytest.mark.anyio
    async def test_revoked_user_loses_access(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="t", is_public=False))
        await storage.grant_access(OWNER_ID, "t", GRANTED_ID)
        assert await storage.user_can_access(
            owner_id=OWNER_ID, topic_name="t", user_id=GRANTED_ID, permission_type="read", user_permissions=["read"]
        )
        await storage.revoke_access(OWNER_ID, "t", GRANTED_ID)
        assert not await storage.user_can_access(
            owner_id=OWNER_ID, topic_name="t", user_id=GRANTED_ID, permission_type="read", user_permissions=["read"]
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
    """Verify list_user_topics filters by ownership and explicit grants."""

    @pytest.mark.anyio
    async def test_excludes_other_users_private_topic(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="theirs", is_public=False))
        await storage.create_topic(OTHER_ID, TopicCreate(topic_name="mine", is_public=False))
        topics = await storage.list_user_topics(OTHER_ID)
        names = {t.topic_name for t in topics}
        assert names == {"mine"}

    @pytest.mark.anyio
    async def test_excludes_other_users_public_topic(self, storage):
        """Public topics owned by others are NOT included in list_user_topics."""
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="public-other", is_public=True))
        await storage.create_topic(OTHER_ID, TopicCreate(topic_name="mine", is_public=False))
        topics = await storage.list_user_topics(OTHER_ID)
        names = {t.topic_name for t in topics}
        assert names == {"mine"}

    @pytest.mark.anyio
    async def test_includes_explicitly_granted_topics(self, storage):
        await storage.create_topic(OWNER_ID, TopicCreate(topic_name="shared", is_public=False))
        await storage.grant_access(OWNER_ID, "shared", OTHER_ID)
        topics = await storage.list_user_topics(OTHER_ID)
        names = {t.topic_name for t in topics}
        assert names == {"shared"}


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
