from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django_rq.workers import get_worker_class
from django_rq import get_queue
from rq import Worker

from core.workers import InspectableWorker


class WorkerRetentionTests(SimpleTestCase):
    def test_all_queues_use_retaining_worker(self):
        self.assertIs(get_worker_class(), InspectableWorker)
        for queue in ("default", "memory", "scheduler"):
            with self.subTest(queue=queue), patch.object(
                Worker, "__init__", return_value=None
            ) as init:
                InspectableWorker([queue])
                self.assertEqual(init.call_args.kwargs["default_result_ttl"], 604800)

    def test_explicit_worker_override_is_preserved(self):
        with patch.object(Worker, "__init__", return_value=None) as init:
            InspectableWorker([], default_result_ttl=120)
        self.assertEqual(init.call_args.kwargs["default_result_ttl"], 120)


class WorkerConnectionTests(SimpleTestCase):
    def test_startup_closes_connections_before_work_even_when_sweep_fails(self):
        for error in (None, RuntimeError("sweep failed")):
            events = []
            worker = object.__new__(InspectableWorker)
            with self.subTest(error=error), patch(
                "core.workers.InspectableWorker.queue_names", return_value=["default"]
            ), patch(
                "core.workers.reconcile_interrupted_ingestions",
                return_value=SimpleNamespace(interrupted=[], retained=[]),
                side_effect=error,
            ), patch("core.workers.IngestionReconciliationScheduler"), patch(
                "core.workers.connections.close_all",
                side_effect=lambda: events.append("close"),
            ), patch.object(
                Worker, "work", side_effect=lambda: events.append("work")
            ):
                worker.work()
            self.assertEqual(events, ["close", "work"])

    def test_memory_only_worker_skips_document_startup(self):
        worker = object.__new__(InspectableWorker)
        events = []
        with patch.object(worker, "queue_names", return_value=["memory"]), patch(
            "core.workers.reconcile_interrupted_ingestions"
        ) as sweep, patch("core.workers.IngestionReconciliationScheduler") as schedule, patch(
            "core.workers.connections.close_all", side_effect=lambda: events.append("close")
        ), patch.object(Worker, "work", side_effect=lambda *a, **k: events.append("work") or "done") as work:
            self.assertEqual(worker.work(burst=True), "done")
        sweep.assert_not_called()
        schedule.assert_not_called()
        work.assert_called_once_with(burst=True)
        self.assertEqual(events, ["close", "work"])

    def test_non_memory_only_workers_preserve_document_startup(self):
        for queues in ([], ["default"], ["scheduler"], ["unknown"], ["default", "memory"]):
            worker = object.__new__(InspectableWorker)
            with self.subTest(queues=queues), patch.object(worker, "queue_names", return_value=queues), patch(
                "core.workers.reconcile_interrupted_ingestions",
                return_value=SimpleNamespace(interrupted=[], retained=[]),
            ) as sweep, patch("core.workers.IngestionReconciliationScheduler") as schedule, patch(
                "core.workers.connections.close_all"
            ), patch.object(Worker, "work", return_value=True):
                self.assertTrue(worker.work())
            sweep.assert_called_once_with()
            schedule.return_value.start.assert_called_once_with()

    def test_real_rq_queue_object_is_identified_as_memory_only(self):
        worker = InspectableWorker([get_queue("memory")])
        self.assertEqual(worker.queue_names(), ["memory"])
        with patch("core.workers.reconcile_interrupted_ingestions") as sweep, patch(
            "core.workers.IngestionReconciliationScheduler"
        ) as schedule, patch("core.workers.connections.close_all"), patch.object(
            Worker, "work", return_value=True
        ):
            self.assertTrue(worker.work(burst=True))
        sweep.assert_not_called()
        schedule.assert_not_called()

    def test_every_fork_closes_parent_connections(self):
        worker = object.__new__(InspectableWorker)
        events = []
        with patch(
            "core.workers.connections.close_all",
            side_effect=lambda: events.append("close"),
        ), patch.object(
            Worker, "fork_work_horse", side_effect=lambda *args: events.append("fork")
        ):
            for _ in range(20):
                worker.fork_work_horse(None, None)
        self.assertEqual(events, ["close", "fork"] * 20)
