# Helm Chart: Plane Community

The official Plane Community Edition Helm chart is maintained and versioned separately from this repository. See its installation instructions on [Artifact Hub](https://artifacthub.io/packages/helm/makeplane/plane-ce).

[![Artifact Hub](https://img.shields.io/endpoint?url=https://artifacthub.io/badge/repository/makeplane)](https://artifacthub.io/packages/helm/makeplane/plane-ce)

## Google Calendar deployment contract

No chart version is declared compatible with Google Calendar by this pointer. Before marking a separately versioned chart compatible, render that exact chart and values and inspect the resulting manifests. The API, worker, beat, and migrator workloads must each receive the same four backend values:

- `GOOGLE_CALENDAR_CLIENT_ID`: the Web application OAuth client ID from a dedicated Google Cloud project.
- `GOOGLE_CALENDAR_CLIENT_SECRET`: the matching OAuth client secret, delivered through the chart's secret mechanism and not printed during verification.
- `GOOGLE_CALENDAR_IS_PROJECT_DEDICATED`: `1` only after confirming that the Google Cloud project is dedicated to Plane's Google Calendar integration.
- `GOOGLE_CALENDAR_RELEASED`: the instance-wide release gate, which must render as `0` until Phase 7 is deployed and its release-readiness checks are complete.

Trace any rendered `Secret` and `ConfigMap` references through to the environment of all four workloads; checking values declarations alone is insufficient. Reject the chart version if a key is absent, differs between workloads, or is not wired into a workload. Perform the check once with shipped defaults and once with distinct non-secret sentinels. Confirm the secret's presence and reference without emitting its decoded value.

In Google Cloud, enable the Google Calendar API in a project used only for this integration. Configure the OAuth consent screen with the app name, support and developer contact details, and the Calendar scopes Plane requests, then publish it when ready. Google may require verification before external users can authorize the sensitive Calendar scopes. Until Google verifies the consent screen, self-hosted users can see Google's **unverified app** interstitial; Google controls that warning, not Plane. Plane requests offline consent so it can refresh access while members are away.

Create a **Web application** OAuth client and register the public Plane API origin followed by `/auth/google-calendar/callback/` as an **Authorized redirect URI**, including the trailing slash. God Mode shows the exact callback under **Integrations > Google Calendar**.

Render and deploy all four workloads with `GOOGLE_CALENDAR_RELEASED=0` first. Configure and validate the dedicated credentials, callback, consent screen, and verification status, then complete the Phase 7 deployment and release-readiness checks. Only afterward render the gate as `1`, recheck all four manifests, and roll them out together.

Live credential rotation is unsupported. Workspace disable retains member grants and is not sufficient preparation for replacing the OAuth client. Start Plane's project-wide revocation flow, wait for terminal cleanup to finish with every grant in a terminal state, and only then roll out the disabled release gate to all four workloads and replace the credentials. Revoking or deleting the dedicated Google OAuth client disconnects Calendar for every member, so coordinate it with that cleanup. Repeat the rendered-manifest verification before releasing replacement credentials.
