# DARE Backend — Administrator Guide

This guide covers operational tasks for administrators of a deployed DARE instance: user and role management, institutional configuration, and access to analytics. It assumes you have superuser access to the Django admin (`/admin/`) or shell access to the host running `manage.py`.

---

## 1. Accessing the admin

After running migrations and creating a superuser:

```bash
python manage.py createsuperuser
```

Visit `/admin/` on the configured administrator origin and log in (development default: `http://localhost:8000/admin/`). From here you can manage users, access codes, model groups, billing, and more.

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

### Signup and approval

There are two ordinary-user signup paths. Both require email verification and an
administrator's approval, either through an approved invitation or review of a
code-free registration. Applicants choose their own password on `/register`;
administrators do not create or distribute that password.

#### Setup and approver access

1. Apply the signup migrations before exposing the updated application. Configure
   the delivery backend, public application and admin URLs, and mandatory email
   verification. New signup paths fail closed if mandatory verification is disabled.
2. A superuser opens **Users → Access request settings**. Set **Admin base url** to
   the administrator origin, including its port, without `/admin/`. Set
   **Registration url** to the full public `/register` URL. Choose any model-group
   restriction for new approvals and enable **Enabled**.
3. Under **Request approvers**, select existing active staff accounts with **Can
   change access request** permission. Assign that permission through Users or a
   Django permission group if needed. Approval does not require superuser status.
   Adding someone here does not grant staff status, change their platform role,
   or give them permission to manage users, provider keys or approver settings.
4. Save the settings. Both list membership and the existing permission are required
   for admin decisions, decision changes, delivery retries and personal email review.
   Removing someone blocks these actions without changing their platform role.
   Superusers manage the selector and must also be selected to decide requests.
   Keep at least one eligible approver while requests are enabled.
5. Enable **Email approval enabled** to include personal review links. Turning it
   off invalidates those links immediately; selected approvers can still review
   through the normal admin list. Turning **Enabled** off also stops new access
   requests, code-free signup and request decisions; existing account login is separate.

Production URLs require HTTPS by default. An explicitly selected, network-restricted
HTTP deployment must allowlist its complete origins in
`ACCESS_REQUEST_ALLOWED_HTTP_ORIGINS`, including ports. This does not add TLS.
Keep DEBUG off and retain the deployment's network restrictions.

#### Path A: register without an invitation code

1. The applicant opens `/register`, enters their name and email, chooses and confirms
   a password, and leaves **Access Code (optional)** blank.
2. DARE creates an inactive ordinary-user account and a **Registered account approval**
   request. It emails the applicant a verification link and notifies configured approvers.
3. The applicant follows the verification link. The confirmation page may still show
   that administrator approval is pending; verification alone does not allow sign-in.
4. A selected approver approves or declines the request as described below. Approval
   and verification can occur in either order. Approval before verification sends an
   approval email explaining the remaining verification step. When both complete,
   DARE activates the account and sends an account-ready email.
5. The applicant signs in with the email and password chosen at registration. No
   code is issued to the applicant for this path. Approval does not grant admin rights.

For an ordinary code-free account:

| Request status | Email verified | Sign-in |
|---|---|---|
| Pending | No or yes | Blocked until approved and verified |
| Approved | No | Blocked until verified |
| Approved | Yes | Allowed once activated, unless separately disabled/deleted |
| Declined | No or yes | Blocked; later verification does not reverse the decline |

The signup-pending page has **Resend** for verification mail. Use the browser where
registration was submitted. A missing email should be investigated through delivery
configuration and logs, not routinely bypassed by marking an address verified.

#### Path B: request an invitation before registering

1. The visitor opens `/request-access` and submits their name, email and optional
   reason. This does not create a user account or ask for a password.
2. Approvers receive a request notification. Approval creates and emails a single-use
   invitation bound to that email address. Default expiry is seven days; the setting
   is configurable under Access request settings.
3. The applicant opens `/register`, uses that same email and the invitation code,
   and chooses their password. A supplied invalid, expired, disabled, used or
   wrong-email code is rejected; it does not silently become code-free registration.
4. The applicant follows the separate verification email and then signs in. An
   invitation already represents approval, so no second signup decision is needed.

Code consumption is transactional. Generated invitation expiry applies before
redemption; redemption clears that group's expiry so the account is not later
expired by the invitation deadline. Legacy/manual code-group expiry, model access,
role and wallet behavior remain separate. New workflow grants use ordinary USER access.

#### Approve or decline in Django admin

1. Sign in to the configured administrator origin with your own selected approver
   account. An application login or receipt of a mailbox copy does not grant admin access.
2. Open **Users → Access requests** and find the applicant by email. Check **Kind**,
   **Status**, the linked account, and the request details.
3. For a pending ordinary request, select **Approve or decline** in the **Review
   request** column, inspect the confirmation page, and submit the decision.
   Alternatively, select pending rows and use **Approve selected pending requests**
   or **Decline selected pending requests**, then **Go**.
4. Open the request to inspect its read-only decision history and delivery records.
   Saving a decision records the actor and time; it does not prove inbox delivery.

Declining sends the applicant a standard explanation: **We are unable to grant
access at this time.** It grants no access and creates no invitation. For code-free
registration the account remains inactive. Repeating the same decision does not
queue another notification for that decision version.

#### Review from an email or shared-mailbox copy

A personal email-review link belongs to the addressed reviewer. It requires that
reviewer's authenticated, eligible account, expires after 48 hours by default,
and opens a confirmation form. Opening a link or a mail scanner's GET request
never saves a decision.

The email also provides a normal Django admin link for other configured approvers.
If you receive a shared-mailbox copy, use that link and your own approver account;
do not use another person's signed link. Copies are informational and grant no
permissions. `ACCESS_REQUEST_REVIEW_CC` copies configured mailboxes once per
code-free registration review, not once per reviewer. Invitation requests from
`/request-access` have no review CC from this setting.

#### Change a reviewed decision

1. In **Access requests**, choose **Change decision** in the reviewed row, or open
   the request and use the same link. Pending-only bulk actions intentionally skip
   already-reviewed requests and display a link to this confirmation form.
2. Confirm the current status and the proposed change. A stale form is rejected;
   reload rather than resubmitting an old confirmation or email link.
3. **Approved → Declined** disables the linked ordinary account and invalidates its
   invitation. New authenticated API requests and socket actions are rejected for
   the inactive account. Existing history and credits are retained.
4. **Declined → Approved** still requires email verification. Reapproval preserves
   existing account roles, access group and credits. An unused invitation is replaced
   with a new code and expiry; the previous code remains invalid. A redeemed
   invitation is not consumed again and does not create a second account.
5. Inspect the new history entry and delivery record. The applicant receives an
   updated-decision notification. Notifications for superseded decisions cannot be
   retried as if they described the current decision.

Duplicates do not automatically reopen a reviewed request. A deleted account, or
an account whose access group has since changed separately, needs user-administration
review. Do not delete and recreate an account just to reverse an ordinary decision.

#### Existing administrator accounts

An account may already have been provisioned by an administrator outside signup.
For an already-active, email-verified staff/superuser account, a selected **superuser**
can choose **Record existing admin approval** and confirm **Record existing approval**.
This records approval and notifies the account holder; it does not create an invitation
or replace the existing password, role, staff/superuser flags, access group or credits.

The request is marked **Access managed in Users** and subsequently links to **Manage
in Users**. Administrative account activation/deactivation stays in Users, not signup
reversal. An inactive or unverified administrator is not activated by this record-only
path. A non-superuser approver cannot use it to grant or acknowledge admin access.

#### Delivery, retries and common issues

Each delivery shows its kind, recipient, decision version, state, attempts, timestamps
and safe failure reason. **Submitted to mail backend** means the provider accepted
submission; confirm inbox receipt separately. Access-request bodies are redacted in
Email Logs. Do not copy live verification, invitation or review tokens into documentation.

Use **Retry failed or interrupted emails** on selected requests after fixing the
cause. Sent records are skipped. An interrupted delivery becomes retryable after
five minutes. An SMTP timeout can mean the provider accepted a message before the
failure was recorded, so a retry can duplicate an email; it does not create another
invitation or decision. Expired/consumed invitations and superseded decisions are
not resent. For an expired unused invitation, an approver can decline and reapprove
the request to issue a replacement through the confirmed decision-change workflow.

| Symptom | Action |
|---|---|
| Verified but awaiting approval | Approve the pending request; verification alone is insufficient. |
| Approved but still inactive | Check email verification and any separate disable/delete action. Use Resend for missing verification mail. |
| Bulk action skips a declined/approved row | Use its Change decision link; bulk decisions are pending-only. |
| Personal review link rejects your login | Check the addressed reviewer, expiry, membership and email-approval switch; use the normal admin link with your own authorized account. |
| Duplicate request or existing account | Review the persistent request/account; do not submit repeated registrations to reset it. |
| HTTP 429 | Wait for the limit to reset. Request-access and registration attempts share the configured per-client limit, including invalid/duplicate attempts. |
| Email contains `example.com` | Set both Site domain and Site name; the name supplies email branding and subjects. |
| Reset link becomes invalid | Request a fresh link and use it before another sign-in/password change; those events can invalidate an earlier token. |
| User is already inactive | Disabling again makes no change. Use the request decision workflow to approve/reapprove ordinary signup access. |

#### Mail, public URLs and setup checks

Configure `EMAIL_BACKEND` explicitly as
`email_logs.backends.LoggingSMTPEmailBackend` or
`email_logs.backends.LoggingResendEmailBackend`. Set the approved sender, delivery
service, credentials if required, port and TLS/SSL settings privately.
`EMAIL_REPLY_TO` controls replies, not the sending identity; an explicit per-message
reply address takes precedence. `EMAIL_TIMEOUT` defaults to ten seconds.
Console/dummy delivery is a failure for request notifications.

Configure `DARE_FRONTEND_URL` and `DARE_BACKEND_URL` for the public deployment, plus
`FRONTEND_CONFIRM_EMAIL_URL` for the SPA `/users/confirm-email` page and
`FRONTEND_PASSWORD_RESET_URL` for `/password-reset/confirm` (the adapter appends
user/token path segments). Do not leave callback origins unset or pointing at localhost.
The frontend proxy must route verification and reset pages to the SPA while keeping
`/users/api/...` proxied to Django.

Under **Sites**, set the public domain (including its port if used) and the deployment
name. The name appears in email subjects, body and signature. Verification/reset
emails greet the registered name, with the sign-in email as fallback. The setup
command rejects placeholder domains; independently verify the display name as well.

Run `python manage.py check_signup_setup --require-enabled` after migrations and
configuration. It checks the mail backend/sender, URL validity, eligible approvers,
mandatory verification, Site domain and cookie/proxy settings. It sends no mail.
Only `--send-test-to recipient@example.org` explicitly sends a diagnostic, whose
inbox receipt must still be confirmed.

For allowlisted HTTP, keep network restrictions, `DJANGO_DEBUG=False`,
`DJANGO_SECURE_COOKIES=False` and `TRUST_PROXY_HTTPS=False`. For HTTPS termination,
coordinate trusted certificates, public URLs, secure cookies and sanitized proxy
headers together. `TRUSTED_PROXY_COUNT` must match the real trusted ingress and
backends must not be directly reachable; a wrong count can make all users share
one rate limit. The default request/registration limit is five attempts per hour
per client, using Django's cache; multi-worker deployments need a shared cache.

Deploy the backend and migrations before exposing new UI. Retain the additive
request, decision and delivery tables during an application rollback; reversing
migrations destroys audit data and may conflict with newer account states. Use
the deployment-specific backup/rollback runbook. Existing user passwords and roles
are not a migration target.

### Disabling / deleting users

On the Users list, **Amount** and **Note** apply to wallet credit actions; leave them
blank for account actions. **Disable selected accounts** reports already-inactive
accounts as unchanged. Deletion has a separate confirmation. These maintenance
actions do not replace signup decisions or send decision-change notifications.

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
| User can't log in | Check approval, verified email and `is_active`; ordinary application login does not require staff status |
| Model not appearing in chooser | Verify model record exists, is in the user's model group |
| Stuck file processing | `python manage.py rqstats`; check worker logs |
| Token cost off | Verify `input_token_rate_per_million` / `output_token_rate_per_million` on the model record |
| Access code rejected | Admin → access code group → verify expiry and remaining uses |

For deeper issues, see [DEPLOYMENT.md § Troubleshooting](../DEPLOYMENT.md#troubleshooting).
