from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from files.api.serializers import FileReprocessingSerializer
from files.constants import (
    DocumentOcrStatus,
    DocumentProcessingMode,
    DocumentReprocessingAction,
    FileStatus,
)
from files.models import DocumentOcrRequest, File
from files.services.document_ocr_approval_service import (
    DocumentOcrApprovalCommand,
    DocumentOcrApprovalService,
    DocumentOcrInvalidState,
)
from files.services.document_reprocessing_service import (
    DocumentReprocessingCommand,
    DocumentReprocessingService,
    ReprocessingUnavailable,
)


class SpinReprocessingSafetyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="spin-owner@example.com", password="unused"
        )
        self.file = File.active_objects.create(
            user=self.user,
            name="scan.pdf",
            file="scan.pdf",
            file_type="application/pdf",
            status=FileStatus.PROCESSED,
            index_generation="previousgeneration",
            embedding_model="embeddinggemma-2",
            document_model={"original": "retained"},
            extracted_text="previous document",
        )

    def test_reprocessing_cannot_silently_change_embedding_space(self):
        for field, value in (
            ("embedding_model", "granite"),
            ("embedding_connection_id", "3ec99a80-3c4b-47f8-b7ca-49f48188a749"),
        ):
            serializer = FileReprocessingSerializer(
                data={
                    "action": DocumentReprocessingAction.REPARSE,
                    "processing_mode": DocumentProcessingMode.BASIC,
                    field: value,
                }
            )
            self.assertFalse(serializer.is_valid())
            self.assertIn("embedding_model", serializer.errors)

    @patch("files.services.document_reprocessing_service.enqueue")
    def test_spin_advanced_reprocessing_is_rejected_before_any_mutation(self, enqueue):
        with self.assertRaises(ReprocessingUnavailable):
            DocumentReprocessingService().start(
                DocumentReprocessingCommand(
                    self.file.pk,
                    self.user.pk,
                    DocumentReprocessingAction.REPARSE,
                    processing_mode=DocumentProcessingMode.ADVANCED,
                )
            )
        self.file.refresh_from_db()
        self.assertEqual(self.file.status, FileStatus.PROCESSED)
        self.assertEqual(self.file.index_generation, "previousgeneration")
        self.assertEqual(self.file.extracted_text, "previous document")
        self.assertEqual(self.file.document_model, {"original": "retained"})
        enqueue.assert_not_called()

    @patch("files.services.document_reprocessing_service.enqueue")
    def test_spin_basic_reprocessing_keeps_model_and_connection(self, enqueue):
        DocumentReprocessingService().start(
            DocumentReprocessingCommand(
                self.file.pk,
                self.user.pk,
                DocumentReprocessingAction.REPARSE,
                processing_mode=DocumentProcessingMode.BASIC,
            )
        )
        self.file.refresh_from_db()
        self.assertEqual(self.file.embedding_model, "embeddinggemma-2")
        self.assertEqual(self.file.index_generation, "previousgeneration")
        enqueue.assert_called_once()

    @patch("files.services.document_ocr_approval_service.enqueue")
    def test_continued_ocr_does_not_modify_existing_spin_index(self, enqueue):
        request = DocumentOcrRequest.objects.create(
            file=self.file,
            status=DocumentOcrStatus.PARTIAL,
            detected_pages=5,
            processed_pages=1,
            max_page_limit=5,
        )
        with self.assertRaises(DocumentOcrInvalidState):
            DocumentOcrApprovalService().start(
                DocumentOcrApprovalCommand(self.file.pk, self.user.pk, page_limit=1)
            )
        self.file.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(request.status, DocumentOcrStatus.PARTIAL)
        self.assertEqual(self.file.status, FileStatus.PROCESSED)
        self.assertEqual(self.file.index_generation, "previousgeneration")
        enqueue.assert_not_called()
