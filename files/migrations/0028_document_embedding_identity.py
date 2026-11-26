import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0027_vectorindexattempt_finished_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = (
        [
            migrations.AddField(
                model_name=model,
                name="embedding_model",
                field=models.CharField(
                    default="granite",
                    db_default="granite",
                    help_text=help_text,
                    max_length=255,
                ),
            )
            for model, help_text in (
                ("file", "Embedding space of the active document index."),
                ("vectorindexattempt", "Embedding space used by this generation."),
            )
        ]
        + [
            migrations.AddField(
                model_name=model,
                name="embedding_connection_id",
                field=models.UUIDField(blank=True, null=True, help_text=help_text),
            )
            for model, help_text in (
                (
                    "file",
                    "Personal connection used to create and search the active index.",
                ),
                ("vectorindexattempt", "Personal connection used by this generation."),
            )
        ]
        + [
            migrations.AddField(
                model_name="file",
                name="spin_index_unavailable",
                field=models.BooleanField(
                    default=False,
                    db_default=False,
                    help_text="The SPIN index may have been partially deleted and requires reprocessing.",
                ),
            ),
            migrations.CreateModel(
                name="SpinEmbeddingCall",
                fields=[
                    (
                        "id",
                        models.BigAutoField(
                            auto_created=True,
                            primary_key=True,
                            serialize=False,
                            verbose_name="ID",
                        ),
                    ),
                    ("created_at", models.DateTimeField(auto_now_add=True)),
                    ("updated_at", models.DateTimeField(auto_now=True)),
                    ("operation_id", models.CharField(max_length=255)),
                    ("batch_number", models.PositiveIntegerField()),
                    ("model", models.CharField(max_length=255)),
                    ("connection_id", models.UUIDField()),
                    ("state", models.CharField(default="started", max_length=16)),
                    (
                        "total_tokens",
                        models.PositiveIntegerField(blank=True, null=True),
                    ),
                    (
                        "file",
                        models.ForeignKey(
                            blank=True,
                            null=True,
                            on_delete=django.db.models.deletion.SET_NULL,
                            to="files.file",
                        ),
                    ),
                    (
                        "user",
                        models.ForeignKey(
                            on_delete=django.db.models.deletion.CASCADE,
                            to=settings.AUTH_USER_MODEL,
                        ),
                    ),
                ],
                options={
                    "constraints": [
                        models.UniqueConstraint(
                            fields=("user", "operation_id", "batch_number"),
                            name="unique_spin_embedding_operation_batch",
                        )
                    ]
                },
            )
        ]
    )
