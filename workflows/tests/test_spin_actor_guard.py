from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from workflows.handlers.step_handler import StepNodeHandler


class SpinWorkflowActorTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {
            "user": SimpleNamespace(id=1),
            "embedding_file_ids": [7],
            "tag_ids": [8],
        }
        self.context = SimpleNamespace(
            turn=None, workflow_run=SimpleNamespace(user_id=2)
        )

    @staticmethod
    def immediate_sync_to_async(function):
        async def run():
            return function()
        return run

    @patch("workflows.handlers.step_handler.database_sync_to_async", immediate_sync_to_async)
    @patch("workflows.handlers.step_handler.File")
    @patch("workflows.handlers.step_handler.collect_embedding_file_ids", new_callable=AsyncMock)
    async def test_shared_workflow_refuses_owners_spin_key(self, collect, file_model):
        collect.return_value = {7, 9}
        file_model.active_objects.filter.return_value.exclude.return_value.exists.return_value = True
        with self.assertRaisesRegex(ValueError, "personal SPIN connection"):
            await StepNodeHandler._check_spin_source_access(self.config, self.context)
        file_model.active_objects.filter.assert_called_once_with(
            user_id=1, pk__in={7, 9}
        )

    @patch("workflows.handlers.step_handler.database_sync_to_async", immediate_sync_to_async)
    @patch("workflows.handlers.step_handler.File")
    @patch("workflows.handlers.step_handler.collect_embedding_file_ids", new_callable=AsyncMock)
    async def test_shared_granite_workflow_is_unchanged(self, collect, file_model):
        collect.return_value = {7}
        file_model.active_objects.filter.return_value.exclude.return_value.exists.return_value = False
        await StepNodeHandler._check_spin_source_access(self.config, self.context)

    @patch("workflows.handlers.step_handler.collect_embedding_file_ids", new_callable=AsyncMock)
    async def test_owner_run_needs_no_extra_guard(self, collect):
        self.context.workflow_run.user_id = 1
        await StepNodeHandler._check_spin_source_access(self.config, self.context)
        collect.assert_not_called()

    @patch("workflows.handlers.step_handler.collect_embedding_file_ids", new_callable=AsyncMock)
    async def test_chat_driven_run_uses_request_actor(self, collect):
        self.context.turn = object()
        await StepNodeHandler._check_spin_source_access(self.config, self.context)
        collect.assert_not_called()
