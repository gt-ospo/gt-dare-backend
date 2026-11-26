import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0027_unify_litellm_background_model"), ("conversations", "0101_ensemble_preset")]

    operations = [
        migrations.CreateModel(
            name="MessageUsageAttempt",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="usage_attempts", to="conversations.message")),
            ],
        ),
        migrations.AddField(
            model_name="transaction",
            name="usage_attempt_id",
            field=models.UUIDField(
                blank=True,
                help_text="Generation attempt identifier for idempotent usage recording.",
                null=True,
                unique=True,
            ),
        ),
    ]
