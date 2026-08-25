/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { CalendarCheck, Check, CircleAlert, CircleX } from "lucide-react";
import useSWR, { useSWRConfig } from "swr";
// plane internal packages
import { API_BASE_URL } from "@plane/constants";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { InstanceService } from "@plane/services";
import type {
  IFormattedInstanceConfiguration,
  IGoogleCalendarReleaseReadiness,
  TInstanceGoogleCalendarConfigurationKeys,
} from "@plane/types";
import { Checkbox } from "@plane/ui";
// components
import { CodeBlock } from "@/components/common/code-block";
import type { TControllerInputFormField } from "@/components/common/controller-input";
import { ControllerInput } from "@/components/common/controller-input";
import { CopyField } from "@/components/common/copy-field";
// hooks
import { useInstance } from "@/hooks/store";

type Props = {
  config: IFormattedInstanceConfiguration;
};

type GoogleCalendarConfigFormValues = Record<TInstanceGoogleCalendarConfigurationKeys, string>;

type ConfigurationError = {
  error?: string;
};

const CALENDAR_CREDENTIAL_REPLACEMENT_ERRORS = new Set([
  "google_calendar_credentials_in_use",
  "google_calendar_credentials_locked",
]);
const GOOGLE_CALENDAR_RELEASE_READINESS_KEY = "GOOGLE_CALENDAR_RELEASE_READINESS";

const instanceService = new InstanceService();

const isCalendarCredentialReplacementError = (error: unknown): error is ConfigurationError =>
  typeof error === "object" &&
  error !== null &&
  "error" in error &&
  typeof (error as ConfigurationError).error === "string" &&
  CALENDAR_CREDENTIAL_REPLACEMENT_ERRORS.has((error as ConfigurationError).error as string);

type ReadinessCheckProps = {
  complete: boolean;
  label: string;
  description: string;
};

function ReadinessCheck(props: ReadinessCheckProps) {
  const { complete, label, description } = props;

  return (
    <li className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
      <span
        className={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full ${
          complete ? "bg-success-subtle text-success-primary" : "bg-warning-subtle text-warning-primary"
        }`}
      >
        {complete ? <Check className="size-3.5" /> : <CircleX className="size-3.5" />}
      </span>
      <div>
        <p className="text-13 font-medium text-primary">{label}</p>
        <p className="mt-0.5 text-12 text-tertiary">{description}</p>
      </div>
    </li>
  );
}

function readinessSummary(readiness: IGoogleCalendarReleaseReadiness) {
  if (readiness.ready) {
    return {
      title: "Calendar backend is ready",
      description: "Credentials, lifecycle recovery, and required reconciliation checks are complete.",
    };
  }
  if (!readiness.configuration_complete) {
    return {
      title: "Calendar backend setup is incomplete",
      description: "Save a dedicated Google OAuth client before preparing this integration for release.",
    };
  }
  if (!readiness.credential_binding_complete) {
    return {
      title: "Calendar credential mismatch detected",
      description: "One or more connections are bound to a different OAuth client configuration.",
    };
  }
  if (!readiness.lifecycle_recovery_complete) {
    return {
      title: "Calendar lifecycle recovery is incomplete",
      description: "Wait for pending connection and cleanup operations to reach a terminal state.",
    };
  }
  return {
    title: "Calendar backend verification is incomplete",
    description: "Required reconciliation checks must complete before the backend is ready for release.",
  };
}

function InstanceGoogleCalendarReadiness() {
  const {
    data: readiness,
    error,
    isLoading,
  } = useSWR(GOOGLE_CALENDAR_RELEASE_READINESS_KEY, () => instanceService.googleCalendarReleaseReadiness());

  if (isLoading) {
    return (
      <section className="rounded-lg border border-subtle bg-layer-1 p-5">
        <p className="text-13 text-tertiary">Checking Calendar backend readiness…</p>
      </section>
    );
  }

  if (error || !readiness) {
    return (
      <section className="flex gap-3 rounded-lg border border-danger-subtle bg-danger-subtle p-5">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger-primary" />
        <div>
          <h2 className="text-14 font-medium text-primary">Calendar readiness is unavailable</h2>
          <p className="mt-1 text-13 text-secondary">
            Plane could not verify the backend state. Refresh this page before making a release decision.
          </p>
        </div>
      </section>
    );
  }

  const summary = readinessSummary(readiness);
  const verificationDescription = readiness.backend_verification_complete
    ? `${readiness.completed_verification_count} of ${readiness.required_verification_count} required connections verified.`
    : readiness.reconciliation_overdue
      ? `${readiness.overdue_reconciliation_count} required reconciliation checks are overdue.`
      : `${readiness.completed_verification_count} of ${readiness.required_verification_count} required connections verified.`;

  return (
    <section className="rounded-lg border border-subtle bg-layer-1 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-subtle pb-4">
        <div className="flex gap-3">
          {readiness.ready ? (
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-success-subtle text-success-primary">
              <Check className="size-5" />
            </span>
          ) : (
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-warning-subtle text-warning-primary">
              <CircleAlert className="size-5" />
            </span>
          )}
          <div>
            <h2 className="text-16 font-medium text-primary">{summary.title}</h2>
            <p className="mt-1 max-w-2xl text-13 text-secondary">{summary.description}</p>
          </div>
        </div>
        <span className="rounded-sm border border-subtle bg-surface-1 px-2 py-1 text-11 font-medium text-secondary">
          {readiness.released ? "Released by server environment" : "Release disabled by server environment"}
        </span>
      </div>

      <ul className="divide-y divide-subtle pt-4">
        <ReadinessCheck
          complete={readiness.configuration_complete}
          label="OAuth configuration"
          description={
            readiness.configuration_complete
              ? "The dedicated OAuth client configuration is complete."
              : "The OAuth client ID, encrypted secret, and dedicated-project acknowledgement are required."
          }
        />
        <ReadinessCheck
          complete={readiness.credential_binding_complete}
          label="Credential binding"
          description={
            !readiness.configuration_complete
              ? "Credential binding cannot be verified until the OAuth configuration is complete."
              : readiness.credential_binding_complete
                ? `${readiness.provider_connection_count} provider connections match the configured OAuth client.`
                : `${readiness.credential_mismatch_count} provider connections do not match. Finish terminal disconnect cleanup before replacing credentials.`
          }
        />
        <ReadinessCheck
          complete={readiness.lifecycle_recovery_complete}
          label="Lifecycle recovery"
          description={
            readiness.lifecycle_recovery_complete
              ? "No connection or cleanup operations require recovery."
              : `${readiness.incomplete_lifecycle_count} connection or cleanup operations have not reached a terminal state.`
          }
        />
        <ReadinessCheck
          complete={readiness.backend_verification_complete}
          label="Backend verification"
          description={verificationDescription}
        />
      </ul>

      <p className="mt-4 border-t border-subtle pt-4 text-12 text-tertiary">
        The release setting is controlled only by the server environment and cannot be changed in God Mode.
      </p>
    </section>
  );
}

export function InstanceGoogleCalendarConfigForm(props: Props) {
  const { config } = props;
  const { updateInstanceConfigurations } = useInstance();
  const { mutate } = useSWRConfig();
  const [credentialReplacementBlocked, setCredentialReplacementBlocked] = useState(false);
  const {
    control,
    handleSubmit,
    reset,
    formState: { errors, isDirty, isSubmitting },
  } = useForm<GoogleCalendarConfigFormValues>({
    defaultValues: {
      GOOGLE_CALENDAR_CLIENT_ID: config.GOOGLE_CALENDAR_CLIENT_ID ?? "",
      GOOGLE_CALENDAR_CLIENT_SECRET: config.GOOGLE_CALENDAR_CLIENT_SECRET ?? "",
      GOOGLE_CALENDAR_IS_PROJECT_DEDICATED: config.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED ?? "0",
    },
  });

  const configuredOrigin = API_BASE_URL || (typeof window !== "undefined" ? window.location.origin : "");
  const callbackUri = `${configuredOrigin.replace(/\/$/, "")}/auth/google-calendar/callback/`;

  const formFields: TControllerInputFormField[] = [
    {
      key: "GOOGLE_CALENDAR_CLIENT_ID",
      type: "text",
      label: "OAuth client ID",
      description: "The client ID from the dedicated Google Cloud project's Web application OAuth client.",
      placeholder: "840195096245-example.apps.googleusercontent.com",
      error: Boolean(errors.GOOGLE_CALENDAR_CLIENT_ID),
      required: true,
    },
    {
      key: "GOOGLE_CALENDAR_CLIENT_SECRET",
      type: "password",
      label: "OAuth client secret",
      description: "The client secret is sent to Plane's encrypted instance-configuration store.",
      placeholder: "GOCSPX-example",
      error: Boolean(errors.GOOGLE_CALENDAR_CLIENT_SECRET),
      required: true,
    },
  ];

  const onSubmit = async (formData: GoogleCalendarConfigFormValues) => {
    setCredentialReplacementBlocked(false);
    try {
      const response = await updateInstanceConfigurations(formData);
      const savedValues = Object.fromEntries(response.map(({ key, value }) => [key, value]));

      reset({
        GOOGLE_CALENDAR_CLIENT_ID: savedValues.GOOGLE_CALENDAR_CLIENT_ID ?? formData.GOOGLE_CALENDAR_CLIENT_ID,
        GOOGLE_CALENDAR_CLIENT_SECRET:
          savedValues.GOOGLE_CALENDAR_CLIENT_SECRET ?? formData.GOOGLE_CALENDAR_CLIENT_SECRET,
        GOOGLE_CALENDAR_IS_PROJECT_DEDICATED:
          savedValues.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED ?? formData.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED,
      });
      void mutate(GOOGLE_CALENDAR_RELEASE_READINESS_KEY);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Configuration saved",
        message: "Google Calendar OAuth credentials were saved successfully.",
      });
    } catch (error) {
      if (isCalendarCredentialReplacementError(error)) {
        setCredentialReplacementBlocked(true);
        setToast({
          type: TOAST_TYPE.ERROR,
          title: "Calendar credentials cannot be replaced",
          message:
            "Confirm release is disabled in the server environment and finish terminal disconnect cleanup for every Calendar connection.",
        });
        return;
      }

      console.error(error);
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Could not save configuration",
        message: "Check the credentials and try again.",
      });
    }
  };

  return (
    <form className="space-y-8" onSubmit={handleSubmit(onSubmit)}>
      <InstanceGoogleCalendarReadiness />

      {credentialReplacementBlocked && (
        <div className="flex gap-3 rounded-lg border border-danger-subtle bg-danger-subtle p-4 text-13 text-secondary">
          <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger-primary" />
          <div className="space-y-1">
            <p className="font-medium text-primary">Credential replacement requires terminal cleanup</p>
            <p>
              Live rotation is not supported. An operator must disable the environment-managed release gate, and every
              member connection—including soft-deleted records—must finish disconnecting and remove its provider state
              before these credentials can be replaced. There is no force-rotation path.
            </p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-2 lg:gap-12">
        <section className="space-y-5">
          <div>
            <h2 className="text-18 font-medium text-primary">Google-provided details for Plane</h2>
            <p className="mt-1 text-13 text-tertiary">
              Create a Web application OAuth client in a Google Cloud project used only for this Calendar integration.
            </p>
          </div>

          {formFields.map((field) => (
            <ControllerInput
              key={field.key}
              control={control}
              type={field.type}
              name={field.key}
              label={field.label}
              description={field.description}
              placeholder={field.placeholder}
              error={field.error}
              required={field.required}
            />
          ))}

          <Controller
            control={control}
            name="GOOGLE_CALENDAR_IS_PROJECT_DEDICATED"
            rules={{ validate: (value) => value === "1" || "A dedicated Google Cloud project is required." }}
            render={({ field: { onChange, ref, value } }) => (
              <div className="space-y-1">
                <div className="flex items-start gap-2">
                  <Checkbox
                    id="GOOGLE_CALENDAR_IS_PROJECT_DEDICATED"
                    ref={ref}
                    checked={value === "1"}
                    onChange={(event) => onChange(event.target.checked ? "1" : "0")}
                    className="mt-0.5"
                  />
                  <label
                    htmlFor="GOOGLE_CALENDAR_IS_PROJECT_DEDICATED"
                    className="cursor-pointer text-13 font-medium text-secondary"
                  >
                    I confirm this Google Cloud project is dedicated to Plane's Google Calendar integration.
                  </label>
                </div>
                {errors.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED?.message && (
                  <p className="text-11 text-danger-primary">{errors.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED.message}</p>
                )}
              </div>
            )}
          />
        </section>

        <section className="space-y-5">
          <div>
            <h2 className="text-18 font-medium text-primary">Plane-provided details for Google</h2>
            <p className="mt-1 text-13 text-tertiary">
              Add this exact value to the OAuth client's <CodeBlock darkerShade>Authorized redirect URIs</CodeBlock>.
            </p>
          </div>
          <div className="rounded-lg bg-layer-1 px-6 py-4">
            <CopyField
              label="Callback URI"
              url={callbackUri}
              description="Plane uses this callback to complete Google Calendar authorization."
            />
          </div>

          <div className="rounded-lg border border-subtle bg-layer-1 p-5">
            <div className="mb-3 flex items-center gap-2">
              <CalendarCheck className="size-4 text-accent-primary" />
              <h3 className="text-14 font-medium text-primary">Google Cloud setup</h3>
            </div>
            <ol className="list-decimal space-y-2 pl-5 text-13 text-secondary">
              <li>Enable the Google Calendar API for the dedicated project.</li>
              <li>
                Configure the OAuth consent screen with the app name, support and developer contact details, and the
                Calendar scopes Plane requests.
              </li>
              <li>
                Publish the consent screen when ready. Google may require verification before external users can
                authorize sensitive Calendar scopes.
              </li>
              <li>
                Plane requests offline consent so it can refresh access and keep calendars synchronized when members are
                away.
              </li>
            </ol>
          </div>
        </section>
      </div>

      <div className="flex gap-3 rounded-lg border border-warning-subtle bg-warning-subtle p-4 text-13 text-secondary">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning-primary" />
        <div className="space-y-2">
          <p>
            Self-hosted instances can show Google's <span className="font-medium text-primary">unverified app</span>{" "}
            interstitial until the OAuth consent screen is verified. This warning is controlled by Google, not Plane.
          </p>
          <p>
            Live credential rotation is unsupported. Revoking or deleting this dedicated OAuth client is a project-wide
            action that disconnects Google Calendar for every member, so coordinate revocation before replacing
            credentials.
          </p>
        </div>
      </div>

      <Button type="submit" variant="primary" size="lg" loading={isSubmitting} disabled={!isDirty}>
        {isSubmitting ? "Saving" : "Save changes"}
      </Button>
    </form>
  );
}
