# DARE Backend — Administrator Guide

This guide covers operational tasks for administrators of a deployed DARE instance: user and role management, institutional configuration, and access to analytics. It assumes you have superuser access to the Django admin (`/admin/`) or shell access to the host running `manage.py`.

---

## 1. Accessing the admin

After running migrations and creating a superuser:

```bash
python manage.py createsuperuser
```

Visit `http://<host>:8000/admin/` and log in. From here you can manage users, access codes, model groups, billing, and more.

For programmatic operations, use the Django shell:

```bash
python manage.py shell
```

---

## 2. User management

### Roles

The platform uses Django's built-in user model extended with a few fields. Permission grants come from three sources:

1. **Django staff / superuser flags** — gate access to `/admin/`.
2. **Group membership** — Django auth groups; convenient for bundling permissions.
3. **Model groups** (DARE-specific) — control which LLMs a user can invoke.

### Creating users

Three options:

- **Self-service registration** — uses either a valid invitation code or administrator approval of a code-free account. Both paths require email verification (see below).
- **Admin-created** — via `/admin/users/user/add/`. Set a temporary password and ask the user to reset.
- **Bulk import** — via management command:

  ```bash
  python manage.py shell <<EOF
  from users.models import User
  User.objects.create_user(email="alice@example.com", password="...", is_active=True)
  EOF
  ```

### Access requests and automated invitations

1. Apply the users access-request migration before starting the updated backend.
2. A superuser opens **Access request settings** in Django admin. Choose reviewers
   who are active staff with the **Can change access request** permission (or superusers).
   Set the public administrator origin and full signup URL, select any model-group
   restriction, and enable requests. Production links require HTTPS by default. A
   VPN/firewall-restricted HTTP deployment must explicitly list its full application
   and admin origins, including ports, in `ACCESS_REQUEST_ALLOWED_HTTP_ORIGINS`.
   Other HTTP origins remain rejected; this setting does not add TLS. Localhost
   development with DEBUG enabled remains supported. On GT, the administrator origin
   must use the separately reachable admin service, not the application-only port.
3. Enable **Email approval enabled** if reviewers should receive approval links.
   Turning it off immediately blocks previously sent email links. Review through the
   normal admin list still works. Only superusers may modify these settings.
4. A visitor opens `/request-access`, supplies a name, email and optional reason.
   One persistent request is kept per normalized email. Duplicate submissions do not
   overwrite the request, reopen decisions or send more notifications. Existing-account
   addresses receive the same generic response. Contact the deployment administrator
   to revisit a declined request or an expired invitation.
5. Reviewers receive a notification. Email links require the addressed administrator
   to sign in, expire after 48 hours by default, and open a confirmation form. GET
   requests and mail scanners cannot approve access. Approve or decline with the form,
   or use **Access requests** in Django admin and the bulk actions.
6. Approval creates exactly one email-bound USER code with capacity one and a seven-day
   expiry by default, and automatically emails it to the applicant. Approval records
   the reviewer and time. It does not create an account, grant Django administrator
   privileges, or verify ownership of an email address. Declining creates no code.
7. The applicant registers with the code and follows the separate verification email.
   Generated invitations work only for the approved email. Their seven-day expiry
   applies before redemption; consuming one clears that generated group's expiry so
   it does not unexpectedly expire the activated account. Legacy code-group expiry,
   role, model-group and wallet behavior remain unchanged. Code consumption is atomic.
8. Alternatively, a visitor registers at `/register` without a code, choosing their
   password immediately. This path is available only while configured access requests
   are enabled. It creates an inactive USER account and a **Registered account approval**
   request, sends verification mail, and notifies reviewers. Supplied invalid codes
   are rejected; they never silently fall back to this path.
9. Administrator approval and email verification can occur in either order. Neither
   alone enables login. Approval before verification sends an approval notification
   explaining that the separate email-verification step is still required. When both complete, the account activates once and receives
   an account-ready notification. Approval attaches a consumed, nonexpiring USER group
   for existing model-access controls; no signup code is required or emailed for this
   path. Later verification does not reactivate an account subsequently disabled by
   an administrator. Declined accounts remain inactive. No staff or superuser privileges
   are assigned by either signup path. On GT, configure the designated GT-OSPO reviewers
   explicitly; membership of an organization is not inferred from an email domain.

For inactive code-free applicants who supply the correct password, login explains
whether verification or administrator approval is still pending. The unverified
message offers the existing verification-resend control; it does not activate the
account. Disabled/deleted accounts retain the administrator-assistance message.

Both signup paths fail closed with 503 if mandatory email verification is disabled.
Deployments intentionally using optional/no verification must review this compatibility
change before upgrading.

**Delivery and retries.** Open a request to inspect each delivery's recipient, state,
number of attempts, and safe failure reason. The list highlights failures. Use
**Retry failed or interrupted emails** after fixing configuration. Retrying an invitation
reuses its code; delivered records are not resent. A delivery interrupted for over five
minutes is eligible for retry. The provider may have accepted a message before a
connection timeout, so a retry can deliver the same invitation twice; it cannot create
another code. Expired or consumed codes are not emailed again. For an expired unused
invitation, review its linked access-code group and extend its expiry before retrying.
The permanent request record prevents automatic reapplication after decline/approval.
An applicant with an already-approved invitation must enter the code sent to that
email address; submitting the same address without a code explains how to use the
existing invitation instead of creating another request.

**Mail configuration.** Optional `EMAIL_REPLY_TO` sets the default reply address for
SMTP and Resend notifications, including verification and password-reset email.
An explicit per-message reply address takes precedence. Blank retains existing
behavior. This setting changes where replies go, not the sending identity.

Set `EMAIL_BACKEND` explicitly to
`email_logs.backends.LoggingSMTPEmailBackend` or
`email_logs.backends.LoggingResendEmailBackend`, and configure the approved sender,
credentials, port, and TLS/SSL settings privately. An explicit backend now overrides
legacy inference, which otherwise selects Resend when both credential fields are set.
`EMAIL_TIMEOUT` defaults to 10 seconds. Console/dummy delivery is recorded as a failure
for access-request messages. A submitted message is not proof of inbox delivery. Verify
real review, invitation, verification, and password-reset emails before rollout.
Access-request email bodies and sensitive provider errors are redacted from Email Logs;
metadata and delivery states remain visible. Existing verification mail is unchanged.

**Setup check and diagnostic mail.** After migrations and admin settings are configured,
run `python manage.py check_signup_setup`. This read-only command checks the delivery
backend/sender, required URLs, reviewer permissions, mandatory verification, production
HTTPS/cookies, and Site branding. It does not activate requests or send mail. Use
`--require-enabled` for the final rollout check. Only an explicit
`--send-test-to recipient@example.org` sends one diagnostic; a successful submission
still needs inbox confirmation. Provider/domain authorization is an external prerequisite.

For an explicitly allowlisted HTTP admin origin, retain the existing network
restrictions and set `DJANGO_SECURE_COOKIES=False`; secure cookies cannot be sent
on HTTP. Keep `TRUST_PROXY_HTTPS=False` when the ingress does not terminate TLS.
Approval authentication, CSRF validation, mandatory email verification and all
role checks remain required in this mode.

For a sanitizing HTTPS proxy, set `TRUST_PROXY_HTTPS=True` to trust its overwritten
`X-Forwarded-Proto`, and `DJANGO_SECURE_COOKIES=True` to protect session/CSRF cookies.
Keep direct backend access blocked. For GT's single direct nginx proxy, set
`TRUSTED_PROXY_COUNT=1`; the default 0 ignores forwarded client addresses. Keep these
settings false/0 for direct local development. Verify the actual topology before
trusting headers. The GT deployment repository supplies mail and TLS templates plus
the verification-page routing correction; install them during the coordinated rollout.

**Rate limiting and ingress.** The new endpoint allows five attempts per hour per client
address, including duplicates. It uses Django's shared cache; use a shared cache in
multi-worker deployments. Forwarded headers are ignored unless the existing DRF
`NUM_PROXIES` setting explicitly identifies the trusted proxy count. At an ingress that
sanitizes/appends forwarded addresses and blocks direct backend access, configure that
count correctly; otherwise a reverse proxy can make all visitors share one limit.
The rate can be overridden with Django setting `ACCESS_REQUEST_THROTTLE_RATE`.

**Rollout and rollback.** Deploy backend and migration first, configure and test settings
with requests disabled, then enable the frontend link and public requests. Retain email
verification. Existing users' activation, passwords, roles and verification records are
not migrated. To disable the new workflow, turn off requests (and email approval), then
roll back application code. Retain the additive request tables for audit/recovery;
unapplying the migration deletes request and delivery records. Do not run migrations
or change hosted accounts as part of a local code review.

### Disabling / deleting users

- Set `is_active = False` to disable login (recommended over deletion — preserves their conversation history).
- Use `soft_delete()` on the model for a recoverable removal.
- Hard-delete only via admin if you need to scrub user data for compliance (note: this cascades).

### Promoting to staff / superuser

In the admin, edit the user and toggle `is_staff` (admin access) or `is_superuser` (all permissions). Or via shell:

```python
from users.models import User
u = User.objects.get(email="alice@example.com")
u.is_staff = True
u.save(update_fields=["is_staff"])
```

---

## 3. Access codes

DARE supports gated registration via access codes — useful for institutional deployments where you want to control who can sign up.

### Creating an access code group

In the admin, navigate to **Access Code Groups** and create a new group. Each group defines:

- A code (the value users enter at registration)
- An optional expiry date
- A finite maximum number of uses; capacity one makes a code single-use
- Default model group(s) to assign to users registered via this code

### Managing codes

- **Rotating a code:** create a new group, distribute the new code, then disable the old group.
- **Viewing usage:** the admin lists each registration tied to a code.
- **Per-institution provisioning:** create one access code group per institution; users registering with that code inherit the institution's model access.

---

## 4. Model groups (LLM access control)

Each user belongs to one or more **model groups**. Each model group has a set of LLM models (e.g., `gpt-4o`, `claude-3.5-sonnet`) that members can invoke.

### Creating a model group

In `/admin/`:

1. Go to **Model Groups** → Add.
2. Name it (e.g., `Premium`, `Researchers`, `Free Tier`).
3. Select the models that group can use.
4. Save.

### Assigning users to model groups

- Edit a user → set their `model_groups`.
- Or assign automatically via access code (configure on the access code group).

### Adding a new model

When a new LLM is released:

1. Add a record in **AI Models** with:
   - Provider (`openai` / `anthropic` / `google` / `ollama`)
   - Model identifier (e.g., `gpt-5`)
   - Pricing (`input_token_rate_per_million`, `output_token_rate_per_million`)
   - Display name and description
2. Add it to the appropriate model groups.
3. Verify it appears in the user's model picker on the frontend.

---

## 5. Institutional configuration

For deployments scoped to a single institution or organization:

### Branding

The frontend reads its branding from `dare-frontend/.env`:

- `VITE_APP_NAME` — application name shown in the header
- (Logos are bundled in `src/assets/` — replace and rebuild)

### Authentication scope

The backend supports per-platform JWT scopes via the `auth_source` field on the user. Partner platforms (e.g., embedded use) can register users with a specific source, and tokens issued to that source carry that scope.

### Email templates

Outbound transactional emails (registration, password reset, confirmations) live in `users/templates/`. Override per institution by:

1. Copying the template.
2. Customizing branding and copy.
3. Setting `TEMPLATES["DIRS"]` in your settings to a custom directory that takes precedence.

### Storage

By default, file uploads land in Django's `MEDIA_ROOT`. For multi-instance deployments, mount a shared filesystem or configure a remote backend (S3, GCS) by overriding `DEFAULT_FILE_STORAGE` in production settings.

---

## 6. Analytics & reporting

### Built-in dashboards

The admin includes basic dashboards under:

- **Conversations** → message volume per user / per model
- **Billing** → token usage, cost accumulation, per-user totals
- **Files** → upload count, processing status distribution

### Token usage queries

Common query — aggregate token usage by user over a time range:

```python
from django.db.models import Sum
from billing.models import TokenUsage  # adapt to your model name

TokenUsage.objects.filter(
    created_at__gte="2026-01-01"
).values("user__email").annotate(
    total_input=Sum("input_tokens"),
    total_output=Sum("output_tokens"),
).order_by("-total_input")[:20]
```

### Exporting data

For ad-hoc analysis, export query results to CSV:

```bash
python manage.py shell <<EOF
import csv
from billing.models import TokenUsage
with open("/tmp/usage.csv", "w") as f:
    w = csv.writer(f)
    w.writerow(["user", "input_tokens", "output_tokens", "cost"])
    for u in TokenUsage.objects.all().select_related("user"):
        w.writerow([u.user.email, u.input_tokens, u.output_tokens, u.cost])
EOF
```

### Sentry

If `SENTRY_DSN` is configured, errors and performance traces flow to your Sentry project. Use Sentry's dashboards for error rates, slow endpoints, and release tracking.

---

## 7. Operational tasks

### Backups

- **Postgres:** schedule `pg_dump` via cron. Retain at least 7 days.
- **Vector store (Weaviate):** use Weaviate's snapshot API. Pinecone is managed.
- **Uploaded files:** snapshot the storage volume or, if using S3, enable versioning.

### Monitoring

Suggested signals to alert on:

- API 5xx rate (via Sentry or your APM)
- RQ queue depth (`python manage.py rqstats`)
- Redis and Postgres connection counts
- LLM provider error rate (logged at WARNING / ERROR)

### Rotating secrets

When rotating any secret in `.env`:

1. Update the value in `.env`.
2. Restart `dare-api` and `dare-worker`.
3. For `DJANGO_SECRET_KEY`: rotating invalidates existing sessions and password reset tokens — communicate before rotating.
4. For `DARE_INTERNAL_KEY`: update the value on partner backends simultaneously.

### Common shell commands

```bash
# Show queued and failed jobs
python manage.py rqstats

# Requeue all failed jobs
python manage.py rqrequeue --all-failed

# Reset a user's password
python manage.py changepassword <email>

# Show migration status
python manage.py showmigrations

# Open an interactive Python shell with Django loaded
python manage.py shell
```

---

## 8. Troubleshooting

| Issue | Where to look |
|---|---|
| User can't log in | Admin → user → check `is_active`, `is_staff`; verify email confirmed |
| Model not appearing in chooser | Verify model record exists, is in the user's model group |
| Stuck file processing | `python manage.py rqstats`; check worker logs |
| Token cost off | Verify `input_token_rate_per_million` / `output_token_rate_per_million` on the model record |
| Access code rejected | Admin → access code group → verify expiry and remaining uses |

For deeper issues, see [DEPLOYMENT.md § Troubleshooting](../DEPLOYMENT.md#troubleshooting).
